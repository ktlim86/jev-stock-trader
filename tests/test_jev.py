import requests

from jev_trader.jev import JevClient, JevError


class Response:
    def __init__(self, status_code, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text or str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json, headers, timeout):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return self.responses.pop(0)


def _ok():
    return Response(
        200,
        {
            "model": "jev-1.13.0",
            "answers": {
                "decision": {
                    "type": "choice",
                    "choice": "buy",
                    "confidence": 0.66,
                    "probabilities": {"buy": 0.7, "hold": 0.2, "sell": 0.1},
                }
            },
            "usage": {"input_tokens": 120, "output_tokens": 8},
        },
    )


def test_decide_posts_the_choice_question_and_stores_probabilities():
    session = Session([_ok()])
    client = JevClient(api_key="secret", session=session, sleep=lambda seconds: None)
    answer = client.decide({"symbol": "700.HK"}, {"decision": {"type": "choice"}})
    call = session.calls[0]
    assert call["url"] == "https://api.typesafe.ai/v1/systemone"
    assert call["headers"]["Authorization"] == "Bearer secret"
    assert call["json"]["model"] == "jev-latest"
    assert call["json"]["state"]["symbol"] == "700.HK"
    assert answer["choice"] == "buy"
    assert answer["confidence"] == 0.66
    assert answer["probabilities"]["sell"] == 0.1
    assert answer["input_tokens"] == 120
    assert answer["model"] == "jev-1.13.0"


def test_retries_429_and_529_with_backoff_then_succeeds():
    slept = []
    session = Session(
        [
            Response(429, {"error": "rate"}, {"Retry-After": "2"}),
            Response(529, {"error": "overloaded"}),
            _ok(),
        ]
    )
    client = JevClient(
        api_key="secret",
        session=session,
        sleep=slept.append,
        max_retries=5,
        backoff_initial_seconds=1,
        backoff_max_seconds=32,
    )
    answer = client.decide({}, {"decision": {"type": "choice"}})
    assert answer["choice"] == "buy"
    assert slept == [2.0, 2.0]  # retry-after, then doubled backoff capped only by the max
    assert len(session.calls) == 3


def test_retry_after_ms_and_exhausted_retries():
    slept = []
    session = Session([Response(429, headers={"retry-after-ms": "1500"}) for _ in range(3)])
    client = JevClient(api_key="k", session=session, sleep=slept.append, max_retries=2, backoff_initial_seconds=1)
    try:
        client.decide({}, {"decision": {}})
    except JevError as exc:
        assert "429" in str(exc)
    else:
        raise AssertionError("expected JevError")
    assert slept == [1.5, 1.5]
    assert len(session.calls) == 3


def test_client_error_is_not_retried():
    slept = []
    session = Session([Response(422, {"error": "bad question"}, text="bad question")])
    client = JevClient(api_key="k", session=session, sleep=slept.append)
    try:
        client.decide({}, {"decision": {}})
    except JevError as exc:
        assert "422" in str(exc)
    else:
        raise AssertionError("expected JevError")
    assert slept == []
    assert len(session.calls) == 1


def test_unexpected_choice_is_rejected():
    payload = _ok()._payload
    payload["answers"]["decision"]["choice"] = "maybe"
    session = Session([Response(200, payload)])
    client = JevClient(api_key="k", session=session, sleep=lambda seconds: None)
    try:
        client.decide({}, {"decision": {}})
    except JevError as exc:
        assert "maybe" in str(exc)
    else:
        raise AssertionError("expected JevError")


def test_session_import_is_requests():
    assert requests.Session
