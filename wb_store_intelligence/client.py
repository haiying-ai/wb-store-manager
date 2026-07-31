from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .config import StoreConfig
from .policy import ReadOnlyPolicy


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class ReadResult:
    endpoint: str
    status: int
    ok: bool
    data: Any
    headers: dict[str, str]


class ReadOnlyWbClient:
    """WB client that cannot issue a request outside the read-only allowlist."""

    def __init__(
        self,
        store: StoreConfig,
        *,
        policy: ReadOnlyPolicy | None = None,
        timeout: int = 60,
        verify_tls: bool = True,
        opener: Callable[..., Any] | None = None,
    ):
        self.store = store
        self.policy = policy or ReadOnlyPolicy()
        self.timeout = timeout
        self.context = None if verify_tls else ssl._create_unverified_context()
        if opener is None:
            safe_opener = urllib.request.build_opener(
                _NoRedirect(), urllib.request.HTTPSHandler(context=self.context)
            )
            self._opener = (
                lambda request, *, timeout, context: safe_opener.open(
                    request, timeout=timeout
                )
            )
        else:
            self._opener = opener

    def request(self, method: str, url: str, *, body: Any = None) -> ReadResult:
        rule = self.policy.authorize(method, url)
        if method.upper() == "GET" and body is not None:
            raise ValueError("GET requests cannot contain a body")
        token = self.store.load_token()
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        headers = {
            "Authorization": token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "wb-store-intelligence/0.1 read-only",
        }
        request = urllib.request.Request(
            url, data=payload, headers=headers, method=method.upper()
        )
        try:
            with self._opener(
                request, timeout=self.timeout, context=self.context
            ) as response:
                raw = response.read()
                return ReadResult(
                    endpoint=rule.name,
                    status=response.status,
                    ok=200 <= response.status < 300,
                    data=self._decode(raw),
                    headers=dict(response.headers.items()),
                )
        except urllib.error.HTTPError as exc:
            return ReadResult(
                endpoint=rule.name,
                status=exc.code,
                ok=False,
                data=self._decode(exc.read()),
                headers=dict(exc.headers.items()),
            )

    @staticmethod
    def _decode(raw: bytes) -> Any:
        if not raw:
            return None
        text = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
