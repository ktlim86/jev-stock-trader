"""Call TypeSafe Jev over HTTP. No orders are placed here."""

from __future__ import annotations

import time
from typing import Any, Callable

import requests

CHOICES = ("buy", "hold", "sell")


class JevError(RuntimeError):
    pass


class JevClient:
    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = "https://api.typesafe.ai/v1/systemone",
        model: str = "jev-latest",
        max_retries: int = 5,
        backoff_initial_seconds: float = 1.0,
        backoff_max_seconds: float = 32.0,
        timeout_seconds: float = 120.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key.strip():
            raise JevError("TYPESAFE_API_KEY is empty")
        self.api_key = api_key.strip()
        self.endpoint = endpoint
        self.model = model
        self.max_retries = max_retries
        self.backoff_initial_seconds = backoff_initial_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self._sleep = sleep

    def decide(self, state: dict[str, Any], question: dict[str, Any]) -> dict[str, Any]:
        body = {"state": state, "model": self.model, "questions": question}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        delay = self.backoff_initial_seconds
        last_status: int | None = None
        for attempt in range(self.max_retries + 1):
            response = self.session.post(
                self.endpoint,
                json=body,
                headers=headers,
                timeout=self.timeout_seconds,
            )
            last_status = response.status_code
            if response.status_code in (429, 529):
                if attempt >= self.max_retries:
                    break
                self._sleep(_retry_delay(response, delay, self.backoff_max_seconds))
                delay = min(delay * 2, self.backoff_max_seconds)
                continue
            if response.status_code >= 400:
                raise JevError(
                    f"TypeSafe HTTP {response.status_code}: {_excerpt(response)}"
                )
            return _parse(response)
        raise JevError(
            f"TypeSafe HTTP {last_status} after {self.max_retries + 1} attempts"
        )


def _parse(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise JevError(f"TypeSafe returned non-JSON: {_excerpt(response)}") from exc
    answers = payload.get("answers") if isinstance(payload, dict) else None
    answer = answers.get("decision") if isinstance(answers, dict) else None
    if not isinstance(answer, dict):
        raise JevError("TypeSafe response has no answers.decision")
    choice = answer.get("choice")
    if choice not in CHOICES:
        raise JevError(f"TypeSafe choice {choice!r} is not buy, hold, or sell")
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict):
        raise JevError("TypeSafe response has no probabilities")
    confidence = answer.get("confidence")
    if not isinstance(confidence, (int, float)):
        raise JevError("TypeSafe response has no confidence")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return {
        "choice": choice,
        "probabilities": {str(key): float(value) for key, value in probabilities.items()},
        "confidence": float(confidence),
        "model": payload.get("model"),
        "input_tokens": usage.get("input_tokens"),
    }


def _retry_delay(response: requests.Response, fallback: float, cap: float) -> float:
    headers = {str(key).lower(): value for key, value in response.headers.items()}
    if "retry-after-ms" in headers:
        try:
            return min(float(headers["retry-after-ms"]) / 1000.0, 120.0)
        except ValueError:
            pass
    if "retry-after" in headers:
        try:
            return min(float(headers["retry-after"]), 120.0)
        except ValueError:
            pass
    return min(max(fallback, 0.0), cap)


def _excerpt(response: requests.Response) -> str:
    text = getattr(response, "text", "") or ""
    return text[:300].replace("\n", " ")
