# Jev paper trader

This program asks TypeSafe AI's Jev model whether to **buy**, **hold**, or **sell** each member of a stock index, sizes a **paper** position for every buy, and writes the result to Google Sheets.

It does not place orders. It does not read account balances, positions, or executions. The Longbridge client it opens is limited to market, fundamental, and quote data.

Jev is a decision model. You send it a JSON `state` and a choice question. It returns the chosen label, a probability for buy, hold, and sell, and a confidence score. This repo builds that state from Longbridge, checks that it fits Jev's context limit, and records the answer. A person still decides whether the paper log means anything.

## What a run does

1. Load `config.yaml` and `.env`.
2. Ask Longbridge for every constituent of `universe.index_symbol` (default `HSI.HK`).
3. For each ticker, pull the datasets that are enabled in config.
4. Compact that payload so it can fit in Jev's limit of **32,000 tokens for the state plus the choice question**.
5. `estimate` stops here and prints token counts and a dollar estimate. It does **not** call TypeSafe and it does **not** write the sheet.
6. `run` sends one `POST https://api.typesafe.ai/v1/systemone` per ticker that fits the limit (`model: jev-latest`), then upserts Google Sheets.

`estimate` does call Longbridge, because the token count has to be the state `run` would send. Pass `--states-dir` to count JSON files already on disk and make no network calls at all.

## Prerequisites

- Python 3.11 or newer
- A Longbridge account with OpenAPI market-data credentials (app key, app secret, access token)
- A TypeSafe API key (only for `run`)
- A Google Cloud service account and a spreadsheet you have shared with it (only for `run` when `sheets.enabled` is true)
- The machine that runs the daily job needs outbound HTTPS to `openapi.longbridge.com`, `api.typesafe.ai`, and `sheets.googleapis.com`

## Install

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and, if you want a different universe or budget, `config.yaml`. Commands below assume the virtualenv is active and the working directory is the repo root, so `python -m jev_trader` can see the package and `config.yaml`.

## Longbridge read-only keys

1. Log in at [https://open.longbridge.com/](https://open.longbridge.com/) and open **User Center**.
2. Create an application if you do not already have one. The page shows three legacy API-key values: **App Key**, **App Secret**, and **Access Token**.
3. Put them in `.env` as `LONGBRIDGE_APP_KEY`, `LONGBRIDGE_APP_SECRET`, and `LONGBRIDGE_ACCESS_TOKEN`.
4. Leave `LONGBRIDGE_LANGUAGE=en`. Where Longbridge can return English names, it will. Any Chinese text that is still in the payload is translated with a small financial glossary or dropped (see [State compaction](#state-compaction)).
5. The access token expires after 90 days by default. When calls start failing with an authentication error, issue a new token from User Center and update `.env`. The Longbridge SDK can also rotate it with `Config.refresh_access_token`; this program does not do that for you.

These three variables are the only Longbridge credentials this program reads. It builds `Config.from_apikey_env()` and then only `MarketContext`, `FundamentalContext`, `QuoteContext`, and `HttpClient`. It never constructs a trade context, never calls order or balance endpoints, and never turns on `LONGBRIDGE_PAPERTRADING` (that flag would point the SDK at the paper-trading host, which this job does not use).

If the developer console lets you limit the token, enable market data only. Even if the token is allowed to trade, this code does not send orders.

### Constituents, and the 20-of-95 MCP result

`MarketContext.constituent(symbol)` calls `GET /v1/quote/index-constituents?symbol=HSI.HK`. The Python SDK method takes **only** the symbol. There is no page, size, or cursor argument, and the typed response is `rise_num`, `fall_num`, `flat_num`, and `stocks`. When the server sends the full member list, one call is the whole index. The SDK does not page because the documented call has nothing to page with.

The Longbridge MCP tool behaved differently. A saved `HSI.HK` response had `"total": 95` and only 20 objects in `stocks`. That cut is in the MCP adapter, not in `MarketContext.constituent`.

This program still checks. After the SDK call it reads the raw JSON. If that body has a `total` larger than the number of symbols, it retries with `limit=0` (the CLI's "all" switch) and then with `page` / `size` until the distinct symbols cover `total`. If those parameters do not add names, the run stops with `ConstituentTruncatedError` instead of scoring a 20-name slice of the Hang Seng. Set `universe.max_tickers` only when you intentionally want a shorter list (the first N symbols in alphabetical order).

## TypeSafe key

1. Open the [TypeSafe quick start](https://docs.typesafe.ai/introduction/quickstart) and sign in to the dashboard linked there.
2. Copy the API key into `.env` as `TYPESAFE_API_KEY`.

`run` sends:

```http
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer $TYPESAFE_API_KEY
Content-Type: application/json
```

```json
{
  "state": {"symbol": "700.HK", "kline_1y": {}},
  "model": "jev-latest",
  "questions": {
    "decision": {
      "type": "choice",
      "instructions": "Given `state` for this stock, ...",
      "criteria": {"buy": "...", "hold": "...", "sell": "..."}
    }
  }
}
```

The instructions and the buy/hold/sell rubrics are `typesafe.question` in `config.yaml`. The response field used here is `answers.decision`: `choice`, `probabilities`, and `confidence`. `jev-latest` currently points at Jev 1.13. The published limit is 64,000 tokens for the whole request and **32,000 tokens for the state plus the longest question**. This job sends one question, so the 32,000 figure is the one that matters. A ticker over that limit is printed as `OVER LIMIT` and, on `run`, skipped. Jev is not called for it.

HTTP **429** and **529** are retried with exponential backoff. A `Retry-After` or `retry-after-ms` header is honored (capped at 120 seconds). Other 4xx responses are not retried. Defaults are 5 retries, starting at 1 second and doubling up to 32 seconds (`typesafe.max_retries`, `backoff_initial_seconds`, `backoff_max_seconds`).

Jev does not publish its tokenizer. Token counts use **tiktoken `cl100k_base`**, the same proxy as the earlier one-ticker estimator. Treat the count as a guardrail, not as Jev's internal total. The price used for the estimate is `typesafe.price_per_million_tokens`, currently **$0.042 per million input tokens**. Output tokens are not included. Each ticker is a separate request, so the cost is `(sum of state tokens + question tokens) / 1_000_000 * price`.

## Google service account

1. In [Google Cloud Console](https://console.cloud.google.com/), create or select a project.
2. Enable the **Google Sheets API** for that project.
3. Create a service account (IAM → Service accounts → Create).
4. Create a JSON key for it and download the file. Store it outside the repo. `.gitignore` ignores `.env` and `service-account*.json`.
5. Create a spreadsheet (or use an existing one). The sheet id is the long token in the URL: `https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit`.
6. Click **Share** and add the service account's `client_email` (it is inside the JSON) as **Editor**. A link-shared sheet is not enough; the service account must be on the member list.
7. Set `GOOGLE_SERVICE_ACCOUNT_JSON` to the file path, or paste the raw JSON as the value. Set `GOOGLE_SHEET_ID` to the id.

`sheets.enabled: false` skips Google entirely. `run` still needs the TypeSafe key. `estimate` needs neither.

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `LONGBRIDGE_APP_KEY` | estimate (live) and run | Longbridge app key |
| `LONGBRIDGE_APP_SECRET` | estimate (live) and run | Longbridge app secret |
| `LONGBRIDGE_ACCESS_TOKEN` | estimate (live) and run | Longbridge access token |
| `LONGBRIDGE_LANGUAGE` | no | `en` (default in `.env.example`), `zh-CN`, or `zh-HK` |
| `LONGBRIDGE_PRINT_QUOTE_PACKAGES` | no | Set `false` to keep the quote socket quiet |
| `TYPESAFE_API_KEY` | run only | TypeSafe bearer token |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | run, if sheets are on | Path to the JSON key, or the JSON itself |
| `GOOGLE_SHEET_ID` | run, if sheets are on | Spreadsheet id |

The names of the TypeSafe and Google variables can be changed with `typesafe.api_key_env`, `sheets.credentials_env`, and `sheets.spreadsheet_id_env`. A `.env` file in the working directory is loaded without overriding variables that are already set.

## Config

`config.yaml` in the repo root is the file the commands read. Override the path with `--config`.

| Key | Meaning |
| --- | --- |
| `universe.index_symbol` | Index passed to Longbridge constituents. Default `HSI.HK`. Other examples: `HSTECH.HK`, `.DJI.US`. US index symbols need the leading dot. |
| `universe.max_tickers` | `null` uses every constituent. A number keeps the first N symbols after sorting A→Z. Same flag: `--max-tickers`. |
| `datasets.*.enabled` | Turn each feed on or off. |
| `datasets.financial_report.kind` / `period` | `All` and `Annual` by default. `kind` is `All`, `IncomeStatement`, `BalanceSheet`, or `CashFlow`. `period` is `Annual`, `SemiAnnual`, `Q1`, `Q2`, `Q3`, `ThreeQ`, or `QuarterlyFull`. The older names `ALL` and `af` still work. |
| `datasets.institution_rating` | Latest analyst snapshot and consensus summary. |
| `datasets.forecast_eps` | EPS forecast history. Compact mode keeps the last `compact.forecast_items` (default 6). |
| `datasets.valuation_comparison.currency` | Peer valuation. `HKD`, `USD`, or `CNY`. Peers are chosen by Longbridge when you do not pass symbols. |
| `datasets.shareholder` | Institutional holders. |
| `datasets.insider_trades` | There is **no** insider-trade endpoint. This switch calls `shareholder_top`, the top holders (institutions, individuals, and insiders) and their share-count change. Compact mode keeps `compact.shareholder_periods` (default 2). |
| `datasets.kline` | Daily candles. `lookback_days` (default 365), `forward_adjust` (default true), `trade_sessions` (`intraday` or `all`). |
| `compact.financial_periods` | Newest annual columns kept. Default 5. `null` keeps every year. |
| `compact.valuation_history_points` | Trailing peer-history points. Default 12. `null` keeps the series. |
| `compact.weekly_closes` | How many weekly-sampled closes to keep. Default 26. |
| `market.pause_seconds` | Sleep between Longbridge calls. Default **0.55** so a full index stays near the published cap of 60 historical kline requests per 30 seconds. |
| `typesafe.model` | `jev-latest`. |
| `typesafe.max_tokens` | Limit for state + question. Default 32000. |
| `typesafe.price_per_million_tokens` | Estimate price. Default `0.042`. |
| `typesafe.question` | The choice instructions and the buy/hold/sell criteria. |
| `portfolio.amount` | Cash notion allocated across **today's buy signals**. Default `1000000`. Override with `--portfolio-amount`. |
| `portfolio.currency` | Label for that amount, and the default valuation currency. Default `HKD`. The amount is not converted between currencies. |
| `portfolio.default_lot_size_hk` | Board lot used for `.HK` names only when `static_info.lot_size` is missing. Default 100. Real Hong Kong board lots are not all 100. |
| `portfolio.default_lot_size` | Same fallback for every other market. Default 1. |
| `sheets.enabled` | Write the sheet on `run`. |
| `sheets.positions_tab` | Tab name for the paper book. Default `positions`. |
| `sheets.probabilities_tab` | Tab for Jev's probabilities. Default `jev_probabilities`. Set to `null` to skip that tab. |

The older dataset key `compare` is accepted as `valuation_comparison`.

## State compaction

Raw annual statements, peer histories, and a year of daily bars do not fit in 32k tokens. Compaction follows the earlier 700.HK estimator:

- Daily bars become indicators: 1-month, 3-month, 6-month, and window return, SMA 20/50/200, RSI(14) (simple average of the last 14 up/down moves, not Wilder), annualised volatility, max drawdown, **52-week high and low from the bar high and low**, 20-day average volume, and weekly closes (every 5th close in the last `weekly_closes * 5` sessions).
- Financial values keep the latest `financial_periods` years.
- EPS forecasts keep the last `forecast_items`.
- Shareholder-top periods keep the first `shareholder_periods` entries (Longbridge lists the newest first).
- Peer valuation history keeps the last `valuation_history_points`.
- Empty strings, empty lists, and empty objects are removed, along with bulky keys such as `tip`, `name_locales`, `name_cn`, `name_hk`, and `object_id`. The ASCII `field` code on a statement line is kept.
- Known Chinese labels (for example `总资产(HKD)`, `最新`) are replaced with English. A Chinese string that is not in the glossary is dropped so it cannot blow the token budget. Set `LONGBRIDGE_LANGUAGE=en` so fewer of those strings appear in the first place.

On the saved 700.HK compact sample, this pass landed around **10k state tokens + 86 question tokens**, under the 32k cap, with no Chinese characters left. That sample was already partly reduced; a live name with more line items can be larger. `estimate` is how you check before paying for `run`.

## Paper allocation

Nothing in this section is sent to a broker.

1. Let N be the number of tickers Jev labelled **buy** on this run. Each of those names is offered `portfolio.amount / N` in `portfolio.currency`.
2. Shares are the largest whole board lot whose cost (`shares * price`) fits in that budget. The price is the latest daily close, or the constituent last price if candles are missing.
3. The board lot is `lot_size` from Longbridge `static_info`. If that call fails, `.HK` uses `default_lot_size_hk` and other markets use `default_lot_size`. A budget smaller than one lot buys **zero** shares. The log line says so. The sheet still records status `buy` with share amount 0.
4. **hold** keeps the share amount already on the positions tab for that ticker. If the sheet has no row, or sheets are disabled, the share amount is 0. Holds are not given a slice of `portfolio.amount`.
5. **sell** sets the share amount to 0.
6. A new buy **replaces** that ticker's previous share amount. Because holds are left as they were, the sum of `total price` can be above or below `portfolio.amount`. This is a paper log of the buy sleeve, not a check that a brokerage account could fund it.

Example: amount `1000000` HKD and 4 buys, so each budget is `250000`. At 400 HKD with a board lot of 100, one lot costs 40000, so the name gets 600 shares (6 lots, total price 240000). At 600 HKD with a 50000 budget and a lot of 100, one lot costs 60000, so the name gets 0 shares.

## Sheet columns

The positions tab is created if it is missing. The first row must be exactly these headers, in this order. You may add columns to the right; updates only write columns A–G.

| Column | Meaning |
| --- | --- |
| `ticker` | Longbridge symbol, such as `700.HK`. |
| `created_at` | UTC time the row was first written (`YYYY-MM-DDTHH:MM:SSZ`). Not changed on later runs. |
| `updated_at` | UTC time of the latest run that touched this ticker. |
| `status` | `buy`, `hold`, or `sell`. |
| `share amount` | Paper shares after the rules above. Integer. |
| `price` | Last close, or constituent last price. |
| `total price` | `share amount * price`, rounded to 0.01. |

If the ticker is already in column A, that row is updated. A second row is not appended.

The optional `jev_probabilities` tab:

| Column | Meaning |
| --- | --- |
| `ticker` | Same symbol. Upserted the same way. |
| `created_at` / `updated_at` | Same clock rules as the positions tab. |
| `choice` | `buy`, `hold`, or `sell`. |
| `confidence` | Jev confidence, 0 to 1. |
| `probabilities` | JSON object of the raw probability map, for example `{"buy":0.7,"hold":0.2,"sell":0.1}`. |
| `model` | Model id returned by the API, such as `jev-1.13.0`. |
| `input_tokens` | `usage.input_tokens` from that response, when present. |

Tickers that are over the token limit, or whose datasets all failed, are printed and **left unchanged** on the sheet. A failed run does not wipe yesterday's row.

## Commands

Token estimate (Longbridge reads, no TypeSafe, no sheet):

```bash
python -m jev_trader estimate
python -m jev_trader estimate --max-tickers 3
python -m jev_trader estimate --config config.yaml
```

Offline estimate from files named `700.HK.json`, each file a compact state object. No API calls:

```bash
python -m jev_trader estimate --states-dir ./states
```

The report looks like this:

```text
tickers: 2
question_tokens: 86
limit: 32000 (state + question)
price_per_million_usd: 0.042
ticker               state  question     total  status
700.HK               10122        86     10208  ok
5.HK                 33000        86     33086  OVER LIMIT
TOTAL                                  43294
estimated_cost_usd: 0.001818
over_limit: 1
over_limit_tickers: 5.HK
```

Paper run:

```bash
python -m jev_trader run
python -m jev_trader run --portfolio-amount 500000
python -m jev_trader run --max-tickers 5
```

A result line:

```text
700.HK: buy shares=600 price=400.0000 total=240000.00 confidence=0.660 p_buy=0.700 p_hold=0.200 p_sell=0.100
```

Exit codes:

- `estimate` exits 0 after printing the report, including any `OVER LIMIT` rows. It exits 1 if the config is invalid, credentials are missing, or the index came back empty.
- `run` exits 1 if the TypeSafe key or sheet settings are missing, the index is empty, or every ticker was skipped or errored. It exits 0 when at least one ticker received a decision. Per-ticker data errors are printed and the rest of the index still runs.

Tests (no live calls):

```bash
python -m pytest tests -q
```

## Daily schedule

Run after the Hong Kong cash close so that day's daily bar is in the candle request. The example assumes the repo lives in `/opt/jev-trader`, the virtualenv is `.venv`, and secrets are in `/opt/jev-trader/.env`.

```cron
CRON_TZ=Asia/Hong_Kong
15 16 * * 1-5 cd /opt/jev-trader && .venv/bin/python -m jev_trader run >> /var/log/jev-trader.log 2>&1
```

`15 16 * * 1-5` is 16:15 on weekdays. Cron needs `CRON_TZ` if the box is not already set to Hong Kong time. A full Hang Seng pass is hundreds of read-only HTTP calls; with `pause_seconds: 0.55` expect on the order of several minutes, not seconds.

Check the log for `OVER LIMIT`, `skipped`, and `error`. Those tickers were not updated on the sheet.

## Troubleshooting

**`Missing Longbridge read-only credentials`**
`LONGBRIDGE_APP_KEY`, `LONGBRIDGE_APP_SECRET`, or `LONGBRIDGE_ACCESS_TOKEN` is unset. `python -m jev_trader` only auto-loads `.env` from the current directory. Cron must `cd` to the repo, or you must export the variables in the crontab.

**Longbridge 401 or "token invalid"**
The access token expired (90 days) or was copied with quotes or a trailing space. Issue a new token in User Center. Confirm `LONGBRIDGE_LANGUAGE` is `en`, `zh-CN`, or `zh-HK`.

**`ConstituentTruncatedError` / "20 of 95"**
The payload's `total` was higher than the number of symbols, and `limit=0` plus `page`/`size` did not fill the gap. The run refuses to trade a partial index. Try the call later, or set `universe.max_tickers` if you truly want a subset. Do not point this job at the MCP constituent tool; that path was the one that returned 20 names.

**A dataset prints `failed` and that block is missing from the state**
That one call failed and the ticker continues with the datasets that worked. If every dataset failed, `run` skips Jev for that symbol. Read the exception text in the log. Historical kline also has a **monthly symbol quota** tied to account assets (the published floor is 100 distinct symbols a month for a new account). The Hang Seng is about 95 names, so one index fits that floor and a second index in the same month may not. Repeated pulls of the same symbol in one month count once.

**Longbridge rate limit (business code 301606 or HTTP 429)**
Raise `market.pause_seconds`. The shipped value 0.55 targets the kline cap of 60 requests per 30 seconds. Failed calls are skipped for that dataset only.

**`OVER LIMIT`**
That ticker's compact state plus the question is above `typesafe.max_tokens` under the cl100k_base proxy. Lower `compact.financial_periods` or `compact.valuation_history_points`, or disable a dataset, then re-run `estimate`. `run` will not send the oversized state.

**`Missing TYPESAFE_API_KEY`**
Expected for `run` until `.env` has the key. `estimate` does not need it.

**TypeSafe 401**
The bearer token is wrong. 422 means the body failed validation; the log includes a short excerpt and the call is not retried. 429 and 529 are retried. If it still fails, the ticker is `error` and the sheet row is left as it was.

**`sheets.enabled is true but the spreadsheet id or service-account credentials are missing`**
Set `GOOGLE_SHEET_ID` and `GOOGLE_SERVICE_ACCOUNT_JSON`, or set `sheets.enabled: false`.

**`service-account file not found`**
The env value is a path and that path does not exist on this machine. Cron often has a different home directory than your shell.

**Google 403 or "The caller does not have permission"**
Share the spreadsheet with the service account `client_email` as Editor. Enable the Google Sheets API on the same project that owns the key.

**`worksheet header ... does not match`**
Row 1 of the positions tab must start with `ticker`, `created_at`, `updated_at`, `status`, `share amount`, `price`, `total price`. The same rule applies to the probabilities tab and its own headers. Rename or delete the tab and let the program recreate it, or fix the header. Extra columns to the right are fine.

**Buy with share amount 0**
The log's parenthetical says which rule fired: no positive price, unknown board lot, or the equal-weight budget is smaller than one lot. `static_info` is the source of the real board lot; the config defaults are only a fallback.

**Chinese text disappeared**
That string was not in the glossary, so it was removed on purpose. Numbers next to it are kept. Add the label to `GLOSSARY` in `jev_trader/compact.py` if you need it in the state.

**Tests trying to reach the network**
`python -m pytest tests -q` mocks Longbridge, TypeSafe, and Google. It should not need keys. A live `estimate` or `run` is the only thing that uses credentials.
