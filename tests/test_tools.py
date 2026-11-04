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
"""tools/list snapshot, per-tool behaviour against a stubbed PlaidClient, and
the error-mapping table. No real network access -- see docs/tickets/PRD-21315.md
Self-test for which of these stand in for a real Plaid Sandbox call.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from plaid_mcp import api_client
from plaid_mcp.api_client import classify_plaid_error
from plaid_mcp.middleware import _credentials_var
from plaid_mcp.api_client import PlaidCredentials
from plaid_mcp.server import create_mcp_server

CREDS = PlaidCredentials("client-123", "secret-abc", "sandbox")

# Tool names and required arguments are a public contract: existing agents and
# tenant connections depend on them. Changing this snapshot is a breaking change
# (SOP S13).
EXPECTED_TOOLS: dict[str, set[str]] = {
    "plaid_create_link_token": {"products", "client_user_id"},
    "plaid_get_link_session": {"link_token"},
    "plaid_exchange_public_token": {"public_token"},
    "plaid_get_item": {"access_token"},
    "plaid_remove_item": {"access_token"},
    "plaid_get_accounts": {"access_token"},
    "plaid_get_auth": {"access_token"},
    "plaid_sync_transactions": {"access_token"},
    "plaid_get_identity": {"access_token"},
    "plaid_get_investments_holdings": {"access_token"},
    "plaid_get_liabilities": {"access_token"},
}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(api_client.asyncio, "sleep", instant)


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(create_mcp_server().list_tools())}


# ---- TC18: tools/list contract ----------------------------------------------


def test_tool_names_are_stable(tools):
    assert set(tools) == set(EXPECTED_TOOLS)


def test_required_arguments_match_contract(tools):
    for name, required in EXPECTED_TOOLS.items():
        schema = tools[name].inputSchema
        assert set(schema.get("required", [])) == required, name


def test_descriptions_are_short(tools):
    for name, tool in tools.items():
        description = tool.description or ""
        assert len(description) <= 500, name
        first_line = (description.splitlines() or [""])[0]
        assert len(first_line) <= 100, name


# ---- stubbed Plaid transport -------------------------------------------------


class Recorder:
    """Mock transport: replies by request path below the Plaid base URL."""

    def __init__(self, routes=None):
        self.requests: list[httpx.Request] = []
        self.routes = routes or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.routes.get(request.url.path, {})
        if callable(reply):
            return reply(request)
        return httpx.Response(200, json=reply)

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]


def body(request: httpx.Request) -> dict:
    return json.loads(request.content) if request.content else {}


async def _call(monkeypatch, recorder: Recorder, tool: str, args: dict):
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    monkeypatch.setattr(api_client, "_get_shared_http_client", lambda: http)
    ctx = _credentials_var.set(CREDS)
    try:
        content, _ = await create_mcp_server().call_tool(tool, args)
    finally:
        _credentials_var.reset(ctx)
    return json.loads(content[0].text)


# ---- TC1: create_link_token --------------------------------------------------


async def test_create_link_token_returns_hosted_link_url_and_token(monkeypatch):
    rec = Recorder(
        {
            "/link/token/create": {
                "link_token": "link-abc",
                "hosted_link_url": "https://hosted.plaid.com/x",
                "expiration": "2026-12-01T00:00:00Z",
            }
        }
    )
    result = await _call(
        monkeypatch, rec, "plaid_create_link_token", {"products": ["transactions", "auth"], "client_user_id": "cust-1"}
    )
    assert result["hosted_link_url"] == "https://hosted.plaid.com/x"
    assert result["link_token"] == "link-abc"
    sent = body(rec.requests[0])
    assert sent["products"] == ["transactions", "auth"]
    assert sent["user"]["client_user_id"] == "cust-1"
    assert sent["hosted_link"] == {}  # hosted_link=true, no delivery_method/redirect_uri given


# ---- TC3 / TC5: link session status (stubbed -- see module docstring) ------


async def test_get_link_session_reports_pending_before_completion(monkeypatch):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    rec = Recorder({"/link/token/get": {"expiration": future, "link_sessions": []}})
    result = await _call(monkeypatch, rec, "plaid_get_link_session", {"link_token": "link-abc"})
    assert result == {"status": "pending", "public_token": None, "institution": None}


async def test_get_link_session_reports_expired_after_lifetime(monkeypatch):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    rec = Recorder({"/link/token/get": {"expiration": past, "link_sessions": []}})
    result = await _call(monkeypatch, rec, "plaid_get_link_session", {"link_token": "link-abc"})
    assert result == {"status": "expired", "public_token": None, "institution": None}


async def test_get_link_session_reports_completed_with_public_token(monkeypatch):
    rec = Recorder(
        {
            "/link/token/get": {
                "expiration": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "link_sessions": [
                    {
                        "results": {
                            "item_add_results": [
                                {"public_token": "public-xyz", "institution": {"name": "First Platypus Bank"}}
                            ]
                        }
                    }
                ],
            }
        }
    )
    result = await _call(monkeypatch, rec, "plaid_get_link_session", {"link_token": "link-abc"})
    assert result == {"status": "completed", "public_token": "public-xyz", "institution": {"name": "First Platypus Bank"}}


# ---- TC6: exchange public token, access_token never logged ------------------


async def test_exchange_public_token_returns_access_token_and_item_id_not_logged(monkeypatch, caplog):
    rec = Recorder({"/item/public_token/exchange": {"access_token": "access-secret-999", "item_id": "item-1"}})
    with caplog.at_level("DEBUG"):
        result = await _call(monkeypatch, rec, "plaid_exchange_public_token", {"public_token": "public-xyz"})
    assert result == {"access_token": "access-secret-999", "item_id": "item-1"}
    assert "access-secret-999" not in caplog.text


# ---- TC7 / TC8: accounts, cached vs. live balances --------------------------


async def test_get_accounts_default_uses_cached_balances(monkeypatch):
    rec = Recorder({"/accounts/get": {"accounts": [{"account_id": "a1", "balances": {"available": 100.0}}]}})
    result = await _call(monkeypatch, rec, "plaid_get_accounts", {"access_token": "access-1"})
    assert result["accounts"][0]["balances"]["available"] == 100.0
    assert rec.calls("/accounts/get") and not rec.calls("/accounts/balance/get")


async def test_get_accounts_refresh_balances_calls_live_balance_endpoint(monkeypatch):
    rec = Recorder({"/accounts/balance/get": {"accounts": [{"account_id": "a1", "balances": {"available": 101.5}}]}})
    result = await _call(monkeypatch, rec, "plaid_get_accounts", {"access_token": "access-1", "refresh_balances": True})
    assert result["accounts"][0]["balances"]["available"] == 101.5
    assert rec.calls("/accounts/balance/get") and not rec.calls("/accounts/get")


# ---- TC9 / TC10: transaction sync --------------------------------------------


async def test_sync_transactions_first_call_returns_added_and_cursor(monkeypatch):
    rec = Recorder(
        {
            "/transactions/sync": {
                "added": [{"transaction_id": "t1"}],
                "modified": [],
                "removed": [],
                "next_cursor": "cursor-1",
                "has_more": False,
            }
        }
    )
    result = await _call(monkeypatch, rec, "plaid_sync_transactions", {"access_token": "access-1"})
    assert result["added"] == [{"transaction_id": "t1"}]
    assert result["next_cursor"] == "cursor-1"
    assert "cursor" not in body(rec.requests[0])


async def test_sync_transactions_second_call_with_cursor_returns_no_changes(monkeypatch):
    rec = Recorder(
        {
            "/transactions/sync": {
                "added": [],
                "modified": [],
                "removed": [],
                "next_cursor": "cursor-1",
                "has_more": False,
            }
        }
    )
    result = await _call(monkeypatch, rec, "plaid_sync_transactions", {"access_token": "access-1", "cursor": "cursor-1"})
    assert result == {"added": [], "modified": [], "removed": [], "next_cursor": "cursor-1", "has_more": False}
    assert body(rec.requests[0])["cursor"] == "cursor-1"


# ---- TC12 / TC13: product-not-enabled structured errors ----------------------


async def test_get_investments_holdings_without_product_returns_invalid_argument(monkeypatch):
    rec = Recorder(
        {
            "/investments/holdings/get": lambda r: httpx.Response(
                400,
                json={
                    "error_type": "INVALID_REQUEST",
                    "error_code": "INVALID_PRODUCT",
                    "error_message": "Investments is not enabled for this item",
                },
            )
        }
    )
    result = await _call(monkeypatch, rec, "plaid_get_investments_holdings", {"access_token": "access-1"})
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["retryable"] is False
    assert "Investments" in result["error"]["message"]


async def test_get_liabilities_without_product_returns_invalid_argument(monkeypatch):
    rec = Recorder(
        {
            "/liabilities/get": lambda r: httpx.Response(
                400,
                json={
                    "error_type": "INVALID_REQUEST",
                    "error_code": "INVALID_PRODUCT",
                    "error_message": "Liabilities is not enabled for this item",
                },
            )
        }
    )
    result = await _call(monkeypatch, rec, "plaid_get_liabilities", {"access_token": "access-1"})
    assert result["error"]["code"] == "invalid_argument"
    assert "Liabilities" in result["error"]["message"]


# ---- TC14 / TC15: remove_item confirmation ----------------------------------


async def test_remove_item_without_confirm_is_refused_and_makes_no_call(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "plaid_remove_item", {"access_token": "access-1"})
    assert result["error"]["code"] == "invalid_argument"
    assert "confirm" in result["error"]["message"].lower()
    assert not rec.requests


async def test_remove_item_with_confirm_true_removes_item_then_get_item_reports_missing(monkeypatch):
    rec = Recorder(
        {
            "/item/remove": {},
            "/item/get": lambda r: httpx.Response(
                400,
                json={
                    "error_type": "ITEM_ERROR",
                    "error_code": "ITEM_NOT_FOUND",
                    "error_message": "The Item you requested cannot be found",
                },
            ),
        }
    )
    removed = await _call(monkeypatch, rec, "plaid_remove_item", {"access_token": "access-1", "confirm": True})
    assert removed == {"removed": True}
    after = await _call(monkeypatch, rec, "plaid_get_item", {"access_token": "access-1"})
    assert after["error"]["code"] == "invalid_argument"
    assert "not be found" in after["error"]["message"]


# ---- TC16 / TC17: rate limiting ----------------------------------------------


async def test_retries_429_honoring_retry_after_then_succeeds(monkeypatch):
    # no_sleep makes the retry delays instant; the delays list below proves the
    # 1s Retry-After header was honored twice (total ~2s if actually slept),
    # matching the house convention of not sleeping for real in tests (see
    # hudu-mcp's test_api_client.py no_sleep fixture).
    delays: list[float] = []

    async def record_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(api_client.asyncio, "sleep", record_sleep)
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "1"}, json={"error_type": "RATE_LIMIT_EXCEEDED"}),
            httpx.Response(429, headers={"Retry-After": "1"}, json={"error_type": "RATE_LIMIT_EXCEEDED"}),
            httpx.Response(200, json={"accounts": []}),
        ]
    )
    rec = Recorder({"/accounts/get": lambda r: next(responses)})
    result = await _call(monkeypatch, rec, "plaid_get_accounts", {"access_token": "access-1"})
    assert result == {"accounts": []}
    assert len(rec.calls("/accounts/get")) == 3
    assert delays == [1.0, 1.0]


async def test_rate_limit_exhausted_returns_retryable_error(monkeypatch):
    rec = Recorder(
        {
            "/accounts/get": lambda r: httpx.Response(
                429, json={"error_type": "RATE_LIMIT_EXCEEDED", "error_message": "rate limited"}
            )
        }
    )
    result = await _call(monkeypatch, rec, "plaid_get_accounts", {"access_token": "access-1"})
    assert result["error"] == {"code": "rate_limited", "message": "rate limited", "retryable": True}
    assert len(rec.requests) == 3  # initial attempt + 2 retries, then give up


# ---- error-type classification table (Data & API contract) -----------------


@pytest.mark.parametrize(
    "error_type,error_code,http_status,expected",
    [
        ("INVALID_REQUEST", None, 400, ("invalid_argument", False)),
        ("INVALID_INPUT", None, 400, ("invalid_argument", False)),
        ("INVALID_PRODUCT", None, 400, ("invalid_argument", False)),
        ("RATE_LIMIT_EXCEEDED", None, 429, ("rate_limited", True)),
        ("ITEM_ERROR", "PRODUCT_NOT_READY", 400, ("upstream_error", True)),
        ("ITEM_ERROR", "ITEM_LOGIN_REQUIRED", 400, ("invalid_argument", False)),
        ("INSTITUTION_ERROR", None, 400, ("upstream_error", True)),
        ("API_ERROR", None, 500, ("upstream_error", True)),
        ("OAUTH_ERROR", None, 400, ("invalid_argument", False)),
        ("PAYMENT_ERROR", None, 400, ("invalid_argument", False)),
        (None, None, 401, ("unauthorized", False)),
        (None, None, 403, ("unauthorized", False)),
        (None, None, 500, ("upstream_error", True)),
        (None, None, 0, ("upstream_error", True)),
        (None, None, 400, ("invalid_argument", False)),
    ],
)
def test_classify_plaid_error_matches_contract_table(error_type, error_code, http_status, expected):
    assert classify_plaid_error(error_type, error_code, http_status) == expected
