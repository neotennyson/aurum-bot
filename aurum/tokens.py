"""Broker token client (read-only).

Fetches short-lived broker access tokens from the user's private token relay
over HTTPS and caches them locally. This module intentionally contains no
order, modify or cancel capability; every broker call it permits is a
historical-data read.

Protocol
--------
* API key: env ``TOKEN_API_KEY`` or ``.env`` next to the project root.
* ``GET {service}/token/{broker}`` with header ``X-API-Key`` returns
  ``{"token": ..., "expiry": ..., "updated": ...}``.
* Cached at ``~/.broker_tokens.json`` (mode 0600), max age 6 hours.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

SERVICE_URL = os.environ.get("AURUM_TOKEN_SERVICE", "https://tokens.example.invalid")
CACHE_PATH = Path.home() / ".broker_tokens.json"
CACHE_TTL_SECONDS = 6 * 60 * 60
BROKERS = ("dhan", "groww")
IST = ZoneInfo("Asia/Kolkata")


class TokenError(RuntimeError):
    """Raised when a token cannot be resolved or a guard rejects a call."""


def _project_env_paths() -> list[Path]:
    here = Path(__file__).resolve()
    return [here.parents[1] / ".env", Path.cwd() / ".env"]


def api_key() -> str:
    value = os.environ.get("TOKEN_API_KEY", "").strip()
    if value:
        return value
    for path in _project_env_paths():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            if text.startswith("export "):
                text = text[7:].lstrip()
            key, sep, candidate = text.partition("=")
            if not sep or key.strip() != "TOKEN_API_KEY":
                continue
            candidate = candidate.strip().strip("'\"")
            if candidate:
                return candidate
    raise TokenError("TOKEN_API_KEY is not configured")


def _normalise(broker: str) -> str:
    value = str(broker).strip().lower()
    if value not in BROKERS:
        raise ValueError(f"broker must be one of {BROKERS}")
    return value


def _expiry_timestamp(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", text):
        now = datetime.now(IST)
        moment = datetime.combine(now.date(), clock_time.fromisoformat(text), tzinfo=IST)
        if moment.timestamp() <= now.timestamp():
            moment += timedelta(days=1)
        return moment.timestamp()
    try:
        numeric = float(text)
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        if numeric > 10_000_000:
            return numeric
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise TokenError("token service returned an invalid expiry") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.timestamp()


def _load_cache() -> dict[str, Any]:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _store_cache(cache: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CACHE_PATH.with_name(f".{CACHE_PATH.name}.{os.getpid()}.tmp")
    handle = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(cache, stream, indent=2, allow_nan=False)
    os.replace(temporary, CACHE_PATH)
    os.chmod(CACHE_PATH, 0o600)


def _fresh_cached(cache: dict[str, Any], broker: str, now: float) -> str | None:
    entry = cache.get(broker)
    if not isinstance(entry, dict):
        return None
    token = entry.get("token")
    fetched = _expiry_timestamp(entry.get("fetched_at"))
    if not isinstance(token, str) or not token or fetched is None:
        return None
    if now - fetched >= CACHE_TTL_SECONDS or now < fetched - 300:
        return None
    expiry = _expiry_timestamp(entry.get("expiry"))
    if expiry is not None and expiry <= now:
        return None
    return token


def _api_request(path: str, method: str) -> Any:
    request = Request(
        f"{SERVICE_URL}{path}",
        data=b"" if method == "POST" else None,
        headers={"X-API-Key": api_key(), "Accept": "application/json"},
        method=method,
    )
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read()
    except HTTPError as error:
        raise TokenError(f"token service returned HTTP {error.code}") from None
    except URLError as error:
        raise TokenError("token service is unavailable") from error
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TokenError("token service returned invalid JSON") from error


def get_token(broker: str) -> str:
    broker = _normalise(broker)
    now = time.time()
    cache = _load_cache()
    cached = _fresh_cached(cache, broker, now)
    if cached is not None:
        return cached
    payload = _api_request(f"/token/{broker}", "GET")
    if not isinstance(payload, dict):
        raise TokenError("token service returned an invalid response")
    token = payload.get("token")
    if not isinstance(token, str) or not token.strip():
        raise TokenError("token service returned no token")
    cache[broker] = {
        "token": token.strip(),
        "expiry": payload.get("expiry"),
        "updated": payload.get("updated"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _store_cache(cache)
    return token.strip()


def refresh_tokens() -> None:
    _api_request("/refresh", "POST")
    cache = _load_cache()
    for broker in list(cache):
        if broker in BROKERS:
            cache.pop(broker, None)
    if cache:
        _store_cache(cache)
    time.sleep(5)


_BLOCKED_URL_WORDS = ("order", "trade", "buy", "sell", "cancel", "modify", "exit")


def _assert_read_only(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise TokenError("broker data URL must be HTTPS")
    probe = f"{parsed.path}?{parsed.query}".lower()
    if any(word in probe for word in _BLOCKED_URL_WORDS):
        raise TokenError("read-only guard rejected the URL")


def authorized_get(url: str, broker: str, headers: dict[str, str] | None = None, timeout: int = 25) -> bytes:
    broker = _normalise(broker)
    _assert_read_only(url)
    for attempt in range(2):
        token = get_token(broker)
        request_headers = dict(headers or {})
        request_headers["Authorization"] = f"Bearer {token}"
        request = Request(url, headers=request_headers, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except HTTPError as error:
            if error.code in {401, 403} and attempt == 0:
                refresh_tokens()
                continue
            raise TokenError(f"broker API returned HTTP {error.code}") from None
        except URLError as error:
            raise TokenError("broker API is unavailable") from error
    raise TokenError("broker API retry failed")


DHAN_DATA_ENDPOINTS = (
    "https://api.dhan.co/v2/charts/historical",
    "https://api.dhan.co/v2/charts/intraday",
)


def dhan_post(endpoint: str, payload: dict[str, Any], timeout: int = 25) -> bytes:
    if endpoint not in DHAN_DATA_ENDPOINTS:
        raise TokenError("only Dhan historical-data endpoints are permitted")
    if any("order" in str(key).lower() for key in payload):
        raise TokenError("order-like payload rejected")
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    for attempt in range(2):
        token = get_token("dhan")
        request = Request(
            endpoint,
            data=body,
            headers={
                "access-token": token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except HTTPError as error:
            if error.code in {401, 403} and attempt == 0:
                refresh_tokens()
                continue
            raise TokenError(f"Dhan API returned HTTP {error.code}") from None
        except URLError as error:
            raise TokenError("Dhan API is unavailable") from error
    raise TokenError("Dhan API retry failed")
