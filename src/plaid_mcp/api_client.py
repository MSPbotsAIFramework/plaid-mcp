# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Async client for the Plaid REST API.

Every Plaid endpoint used here is POST-only and takes/returns JSON. Plaid
reports errors in the response body (``error_type``/``error_code``/
``error_message``) alongside a non-2xx HTTP status; ``classify_plaid_error``
maps that pair to the service's fixed six-value error vocabulary, per the
"Error envelope" table in docs/tickets/PRD-21315.md.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any

import httpx

from ._json import error_envelope
from .config import API_BASE_URLS

logger = logging.getLogger("plaid_mcp.api")

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_MAX_RETRIES = 2
_MAX_BACKOFF_SECONDS = 20.0
# Whole-call budget including retries; the gateway times a tool call out at 120s.
_CALL_BUDGET_SECONDS = 100.0

# One connection pool for the process. It never holds credentials: the
# PLAID-CLIENT-ID/PLAID-SECRET headers are built per request from the
# caller's own credentials, so sharing the pool across tenants is safe.
_shared_http_client: httpx.AsyncClient | None = None


class _RejectAllCookies(DefaultCookiePolicy):
    """Never store or send a cookie: the HTTP client is shared by every tenant."""

    def set_ok(self, cookie, request) -> bool:  # noqa: ANN001 - stdlib signature
        return False

    def return_ok(self, cookie, request) -> bool:  # noqa: ANN001 - stdlib signature
        return False


def _no_cookies() -> CookieJar:
    # A plain CookieJar: httpx uses it as is (an httpx.Cookies would be copied into a default jar).
    return CookieJar(policy=_RejectAllCookies())


def _get_shared_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None:
        _shared_http_client = httpx.AsyncClient(cookies=_no_cookies(), timeout=_TIMEOUT)
    return _shared_http_client


# Plaid error_type -> (MCP code, retryable). "causes" (multi-item batch errors)
# is not used: every tool here addresses a single Item.
_ERROR_TYPE_TO_CODE: dict[str, tuple[str, bool]] = {
    "INVALID_REQUEST": ("invalid_argument", False),
    "INVALID_INPUT": ("invalid_argument", False),
    "INVALID_PRODUCT": ("invalid_argument", False),
    "RATE_LIMIT_EXCEEDED": ("rate_limited", True),
    "INSTITUTION_ERROR": ("upstream_error", True),
    "API_ERROR": ("upstream_error", True),
    "OAUTH_ERROR": ("invalid_argument", False),
    "PAYMENT_ERROR": ("invalid_argument", False),
}
# ITEM_ERROR is split by error_code: PRODUCT_NOT_READY is transient (upstream is
# still syncing); everything else (e.g. ITEM_LOGIN_REQUIRED) needs the end
# customer to re-link, which is not something retrying fixes.
_ITEM_ERROR_RETRYABLE_CODES = {"PRODUCT_NOT_READY"}


def classify_plaid_error(error_type: str | None, error_code: str | None, http_status: int) -> tuple[str, bool]:
    """Map a Plaid error (or, when the body did not parse, the bare HTTP status) to
    one of the six MCP error codes: not_configured/unauthorized/not_found/
    invalid_argument/rate_limited/upstream_error.
    """
    if error_type == "ITEM_ERROR":
        return ("upstream_error", True) if error_code in _ITEM_ERROR_RETRYABLE_CODES else ("invalid_argument", False)
    if error_type in _ERROR_TYPE_TO_CODE:
        return _ERROR_TYPE_TO_CODE[error_type]
    if http_status in (401, 403):
        return "unauthorized", False
    if http_status == 0 or http_status >= 500:
        return "upstream_error", True
    return "invalid_argument", False


class PlaidError(Exception):
    def __init__(self, http_status: int, message: str, error_type: str | None = None, error_code: str | None = None):
        self.http_status = http_status
        self.message = message
        self.error_type = error_type
        self.error_code = error_code
        super().__init__(f"Plaid API error {http_status} ({error_type}/{error_code}): {message}")

    def to_envelope(self) -> str:
        code, retryable = classify_plaid_error(self.error_type, self.error_code, self.http_status)
        return error_envelope(code, self.message, retryable)


@dataclass(frozen=True)
class PlaidCredentials:
    client_id: str
    secret: str
    env: str  # "sandbox" | "production" -- validated by middleware.py before this is built

    def __repr__(self) -> str:  # never leak the secret into logs or tracebacks
        return f"PlaidCredentials(client_id={self.client_id!r}, secret=<redacted>, env={self.env!r})"


class PlaidClient:
    """Thin wrapper over the Plaid API for one caller's client_id/secret/env.

    access_token is never carried here: it identifies which end customer's
    Item a call is about, not who is calling, so every tool passes it as an
    ordinary request-body field instead (see Tech design -> Conclusion).
    """

    def __init__(self, credentials: PlaidCredentials, http_client: httpx.AsyncClient | None = None):
        self._credentials = credentials
        self._http = http_client
        self.base_url = API_BASE_URLS[credentials.env]

    def _headers(self) -> dict[str, str]:
        return {
            "PLAID-CLIENT-ID": self._credentials.client_id,
            "PLAID-SECRET": self._credentials.secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def post(self, path: str, body: dict[str, Any] | None = None, *, idempotent: bool = True) -> Any:
        """POST to a Plaid endpoint and return the parsed JSON body.

        ``idempotent`` controls whether a connection error / timeout / 5xx is
        retried: reads (accounts/get, transactions/sync, ...) are idempotent
        and safe to retry; link/token/create and item/public_token/exchange
        are not (each call mints a fresh session or burns a single-use
        token), so only a 429 is retried for those -- matching the Hudu
        client's method-based rule, adapted because every Plaid call is POST.
        """
        http = self._http or _get_shared_http_client()
        url = f"{self.base_url}{path}"
        headers = self._headers()
        started = time.monotonic()
        attempt = 0
        while True:
            t0 = time.monotonic()
            resp: httpx.Response | None = None
            network_error: PlaidError | None = None
            try:
                resp = await http.post(url, json=body or {}, headers=headers)
                status = resp.status_code
            except httpx.TimeoutException:
                status, network_error = 0, PlaidError(0, f"Timed out calling Plaid ({path})")
            except httpx.RequestError as e:
                status, network_error = 0, PlaidError(0, f"Network error calling Plaid ({path}): {type(e).__name__}")

            logger.info(
                "plaid_api path=%s status=%s ms=%d attempt=%d",
                path, status, (time.monotonic() - t0) * 1000, attempt,
            )

            retryable = status == 429 or (idempotent and (status == 0 or status >= 500))
            if retryable and attempt < _MAX_RETRIES:
                delay = _retry_delay(resp, attempt)
                if time.monotonic() - started + delay < _CALL_BUDGET_SECONDS:
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
            if network_error is not None:
                raise network_error
            assert resp is not None
            if resp.status_code >= 400:
                error_type, error_code = _error_fields(resp)
                raise PlaidError(resp.status_code, _error_message(resp), error_type, error_code)
            if not resp.content or not resp.content.strip():
                return {}
            try:
                return resp.json()
            except ValueError:
                return {"raw_response": resp.text[:2000]}


def _error_fields(resp: httpx.Response) -> tuple[str | None, str | None]:
    try:
        payload = resp.json()
    except ValueError:
        return None, None
    if isinstance(payload, dict):
        return payload.get("error_type"), payload.get("error_code")
    return None, None


def _error_message(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        text = resp.text.strip()
        if text.lower().startswith("<"):
            return f"Plaid returned HTTP {resp.status_code}"  # an HTML error page, not useful to the agent
        return f"Plaid returned HTTP {resp.status_code}: {text[:300]}" if text else f"Plaid returned HTTP {resp.status_code}"
    if isinstance(payload, dict):
        message = payload.get("error_message") or payload.get("display_message")
        if message:
            return str(message)[:500]
    return f"Plaid returned HTTP {resp.status_code}"


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), _MAX_BACKOFF_SECONDS))
            except ValueError:
                pass
    return min(float(2**attempt), _MAX_BACKOFF_SECONDS)
