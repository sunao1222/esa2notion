"""Sequential HTTP transport with deliberately narrow, safe retries."""

from __future__ import annotations

import math
import random
import re
import threading
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import requests

from .errors import MigrationError

TIMEOUT = (10, 120)
MAX_ATTEMPTS = 6


def safe_url(url: str) -> str:
    """Remove credentials and signed query/fragment material from diagnostics."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or "unknown-host"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return "[invalid URL]"


def _safe_message(message: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[redacted]")
    message = re.sub(r"https?://[^\s<>\"']+", lambda match: safe_url(match[0]), message)
    message = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", message)
    return message[:1000]


def json_object(response: requests.Response, *, context: str) -> dict[str, Any]:
    try:
        data = response.json()
    except (ValueError, requests.exceptions.JSONDecodeError):
        raise MigrationError(f"{context}: response is not valid JSON") from None
    if not isinstance(data, dict):
        raise MigrationError(f"{context}: expected a JSON object")
    return data


class HTTPClient:
    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        min_interval: float = 0.0,
        secrets: tuple[str, ...] = (),
        wall_time: Callable[[], float] = time.time,
        jitter: Callable[[], float] = lambda: random.uniform(0, 0.25),
    ) -> None:
        self.session = session if session is not None else requests.Session()
        # Never consult ~/.netrc for image downloads or replace supplied API auth.
        self.session.trust_env = False
        self.sleep = sleep
        self.monotonic = monotonic
        self.min_interval = min_interval
        self.secrets = secrets
        self.wall_time = wall_time
        self.jitter = jitter
        self._last_request: float | None = None
        self._lock = threading.Lock()

    def _retry_delay(self, response: requests.Response, attempt: int) -> float:
        value = response.headers.get("Retry-After", "")
        try:
            delay = float(value)
            if math.isfinite(delay) and delay >= 0:
                return delay
        except (TypeError, ValueError):
            pass
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, parsed.timestamp() - self.wall_time())
        except (TypeError, ValueError, OverflowError):
            return 2**attempt + self.jitter()

    def _error(self, method: str, url: str, response: requests.Response, data: dict) -> MigrationError:
        code = data.get("code")
        message = data.get("message")
        detail = ": ".join(str(value) for value in (code, message) if isinstance(value, str))
        detail = _safe_message(detail, self.secrets)
        text = f"{method} {safe_url(url)}: HTTP {response.status_code}"
        if detail:
            text += f" ({detail})"
        additional = data.get("additional_data")
        if isinstance(additional, dict):
            resource = additional.get("committed_resource_id")
            try:
                resource_id = UUID(str(resource)).hex
            except (ValueError, TypeError, AttributeError):
                pass
            else:
                text += f"; resource may already exist: https://www.notion.so/{resource_id}"
        return MigrationError(text)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Retry only explicit 429/529 rejection, never ambiguous network writes."""
        kwargs["timeout"] = TIMEOUT
        kwargs.setdefault("allow_redirects", False)
        positions: list[tuple[Any, int]] = []
        files = kwargs.get("files", {})
        entries = files.values() if isinstance(files, dict) else (value for _, value in files)
        for entry in entries:
            stream = entry[1] if isinstance(entry, (tuple, list)) else entry
            if hasattr(stream, "read"):
                try:
                    positions.append((stream, stream.tell()))
                except (AttributeError, OSError, ValueError):
                    raise MigrationError("Multipart upload requires a rewindable file") from None
        with self._lock:
            for attempt in range(MAX_ATTEMPTS):
                if attempt:
                    for stream, position in positions:
                        try:
                            stream.seek(position)
                        except (AttributeError, OSError, ValueError):
                            raise MigrationError("Cannot rewind multipart upload for retry") from None
                if self._last_request is not None:
                    delay = self.min_interval - (self.monotonic() - self._last_request)
                    if delay > 0:
                        self.sleep(delay)
                self._last_request = self.monotonic()
                try:
                    response = self.session.request(method, url, **kwargs)
                except requests.RequestException:
                    raise MigrationError(
                        f"{method} {safe_url(url)}: connection or timeout failure; "
                        "not retried because a write may already have committed"
                    ) from None
                if 200 <= response.status_code < 300:
                    return response
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                error = self._error(method, url, response, data)
                blocked = data.get("code") == "public_api_request_blocked"
                if response.status_code not in (429, 529) or blocked or attempt == MAX_ATTEMPTS - 1:
                    response.close()
                    raise error
                delay = self._retry_delay(response, attempt)
                response.close()
                self.sleep(delay)
        raise AssertionError("unreachable retry loop")
