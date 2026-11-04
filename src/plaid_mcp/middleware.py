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
"""Per-request Plaid credentials: reads the three X-Plaid-* headers on /mcp,
sets a contextvar for the duration of the request, and resets it in
``finally`` -- this is the entire request-level isolation mechanism (AC7):
nothing credential-related is ever stored at module level.

Also sets ``request_id_var`` for the same duration (reusing hudu-mcp's
``server.py`` pattern): a stateless, multi-tenant server can log several
lines per tool call (see api_client.py's retry logging), and without a
shared id there is no way to tell which lines belong to the same request,
or to separate two tenants' concurrent requests in the log stream.
"""

import contextvars
import uuid

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_client import PlaidCredentials
from .config import API_BASE_URLS

HEADER_CLIENT_ID = "X-Plaid-Client-Id"
HEADER_SECRET = "X-Plaid-Secret"
HEADER_ENV = "X-Plaid-Env"
REQUIRED_HEADERS = [HEADER_CLIENT_ID, HEADER_SECRET, HEADER_ENV]

_credentials_var: contextvars.ContextVar[PlaidCredentials | None] = contextvars.ContextVar(
    "plaid_credentials", default=None
)
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("plaid_request_id", default="-")


def get_client_credentials() -> PlaidCredentials | None:
    """The credentials for the current request, or None outside a request."""
    return _credentials_var.get()


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


class PlaidCredentialsMiddleware:
    """Reads X-Plaid-Client-Id / X-Plaid-Secret / X-Plaid-Env on /mcp requests.

    Missing or invalid (including an X-Plaid-Env value that is neither
    "sandbox" nor "production") -> 401 listing the three required header
    names; the submitted values, valid or not, are never echoed back (AC2).
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        client_id = _clean(request.headers.get(HEADER_CLIENT_ID))
        secret = _clean(request.headers.get(HEADER_SECRET))
        env = _clean(request.headers.get(HEADER_ENV))
        if not (client_id and secret and env in API_BASE_URLS):
            await JSONResponse(
                {
                    "error": "Missing or invalid credentials",
                    "message": (
                        f"All of {', '.join(REQUIRED_HEADERS)} are required; "
                        f"{HEADER_ENV} must be one of {sorted(API_BASE_URLS)}."
                    ),
                    "required_headers": REQUIRED_HEADERS,
                },
                status_code=401,
            )(scope, receive, send)
            return

        request_id = (request.headers.get("x-request-id") or "").strip()[:64] or uuid.uuid4().hex[:12]
        creds_token = _credentials_var.set(PlaidCredentials(client_id, secret, env))
        rid_token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _credentials_var.reset(creds_token)
            request_id_var.reset(rid_token)
