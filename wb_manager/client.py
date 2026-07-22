from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class WbApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class ApiResult:
    status: int
    ok: bool
    data: Any
    headers: dict[str, str]


class WbClient:
    def __init__(self, token: str, *, verify_tls: bool = True, timeout: int = 60):
        self.token = token
        self.timeout = timeout
        self.context = None if verify_tls else ssl._create_unverified_context()

    def request(
        self,
        method: str,
        url: str,
        *,
        body: Any = None,
        retries: int = 2,
        retry_sleep: float = 1.5,
        timeout: int | None = None,
    ) -> ApiResult:
        payload = None
        headers = {
            "Authorization": self.token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "wb-store-manager/0.1",
        }
        if body is not None:
            payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=payload, headers=headers, method=method.upper())
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=self.context) as resp:
                    return ApiResult(
                        status=resp.status,
                        ok=200 <= resp.status < 300,
                        data=self._decode(resp.read()),
                        headers=dict(resp.headers.items()),
                    )
            except urllib.error.HTTPError as exc:
                body_data = self._decode(exc.read())
                if exc.code == 429 and attempt < retries:
                    wait = self._retry_after(exc.headers, retry_sleep)
                    time.sleep(wait)
                    continue
                return ApiResult(
                    status=exc.code,
                    ok=False,
                    data=body_data,
                    headers=dict(exc.headers.items()),
                )
            except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(retry_sleep * (attempt + 1))
                    continue
                raise WbApiError(f"Network error calling {url}: {exc}") from exc

        raise WbApiError(f"Network error calling {url}: {last_error}")

    @staticmethod
    def _decode(raw: bytes) -> Any:
        if not raw:
            return None
        text = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    @staticmethod
    def _retry_after(headers: Any, fallback: float) -> float:
        for key in ("X-Ratelimit-Retry", "Retry-After"):
            value = headers.get(key)
            if value:
                try:
                    return max(float(value), fallback)
                except ValueError:
                    return fallback
        return fallback
