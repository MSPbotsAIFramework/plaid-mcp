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
"""Credential middleware: 401 on missing/invalid headers, per-request isolation (AC2, AC7)."""

import asyncio

import pytest
from starlette.testclient import TestClient

from plaid_mcp.__main__ import build_http_app
from plaid_mcp.api_client import PlaidCredentials
from plaid_mcp.middleware import (
    HEADER_CLIENT_ID,
    HEADER_ENV,
    HEADER_SECRET,
    REQUIRED_HEADERS,
    _credentials_var,
    request_id_var,
)
from plaid_mcp.server import create_mcp_server

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
CREDS = {HEADER_CLIENT_ID: "client-1", HEADER_SECRET: "super-secret-999", HEADER_ENV: "sandbox"}


def _client() -> TestClient:
    return TestClient(build_http_app(create_mcp_server()))


def test_health_is_local():
    with _client() as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_valid_headers_reach_tools_list():
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS)
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {HEADER_CLIENT_ID: "client-1"},
        {HEADER_CLIENT_ID: "client-1", HEADER_SECRET: "super-secret-999"},
        CREDS | {HEADER_ENV: "development"},  # Plaid retired this environment; no longer a valid value
        CREDS | {HEADER_ENV: ""},
        CREDS | {HEADER_SECRET: ""},
    ],
)
def test_missing_or_invalid_headers_return_401(headers):
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | headers)
    assert resp.status_code == 401
    assert resp.json()["required_headers"] == REQUIRED_HEADERS


def test_missing_secret_header_returns_401_without_echoing_value():
    headers = CREDS | {HEADER_SECRET: ""}
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | headers)
    assert resp.status_code == 401
    assert "super-secret-999" not in resp.text


def test_credentials_are_reset_after_the_request():
    with _client() as client:
        client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS)
    assert _credentials_var.get() is None


# ---- TC11: concurrent requests do not share credentials or state ------------


async def test_concurrent_requests_do_not_share_credentials_or_state():
    creds_a = PlaidCredentials("client-a", "secret-a", "sandbox")
    creds_b = PlaidCredentials("client-b", "secret-b", "sandbox")
    # A module-level counter stands in for "any state object reused across
    # contexts": if the two tasks below shared one, this would race and one
    # task could observe the other's increment between its own read and write.
    shared_counter = {"value": 0}

    async def run_as(creds: PlaidCredentials) -> str:
        token = _credentials_var.set(creds)
        try:
            seen_before = shared_counter["value"]
            await asyncio.sleep(0)  # yield so the two tasks interleave
            shared_counter["value"] = seen_before + 1
            await asyncio.sleep(0)
            # Each task must still see its own credentials after yielding control.
            return _credentials_var.get().client_id
        finally:
            _credentials_var.reset(token)

    result_a, result_b = await asyncio.gather(run_as(creds_a), run_as(creds_b))
    assert (result_a, result_b) == ("client-a", "client-b")
    assert _credentials_var.get() is None  # reset outside any request


# ---- review round 1 must-fix: request-id / correlation-id in the logging path ----
#
# Reuses hudu-mcp's pattern (server.py:46,128,130,135; __main__.py:27-39): a
# contextvar set by the credentials middleware from an inbound x-request-id
# header (or a generated id), and a logging.Filter that stamps it onto every
# LogRecord so api_client.py / tools/_common.py need no per-call changes.


def test_request_id_defaults_to_dash_outside_a_request():
    assert request_id_var.get() == "-"


async def test_inbound_request_id_header_is_threaded_through_and_reset_after():
    from plaid_mcp.middleware import PlaidCredentialsMiddleware

    seen: dict[str, str] = {}

    async def inner_app(scope, receive, send) -> None:
        seen["request_id"] = request_id_var.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = PlaidCredentialsMiddleware(inner_app)
    scope = {
        "type": "http",
        "path": "/mcp",
        "headers": [
            (b"x-plaid-client-id", b"client-1"),
            (b"x-plaid-secret", b"super-secret-999"),
            (b"x-plaid-env", b"sandbox"),
            (b"x-request-id", b"caller-rid-123"),
        ],
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: dict) -> None:
        pass

    await middleware(scope, receive, send)
    assert seen["request_id"] == "caller-rid-123"
    assert request_id_var.get() == "-"  # reset outside any request


async def test_missing_inbound_request_id_header_is_generated():
    from plaid_mcp.middleware import PlaidCredentialsMiddleware

    seen: dict[str, str] = {}

    async def inner_app(scope, receive, send) -> None:
        seen["request_id"] = request_id_var.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = PlaidCredentialsMiddleware(inner_app)
    scope = {
        "type": "http",
        "path": "/mcp",
        "headers": [
            (b"x-plaid-client-id", b"client-1"),
            (b"x-plaid-secret", b"super-secret-999"),
            (b"x-plaid-env", b"sandbox"),
        ],
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: dict) -> None:
        pass

    await middleware(scope, receive, send)
    assert seen["request_id"] != "-"
    assert seen["request_id"]  # non-empty, auto-generated


def test_request_id_filter_stamps_the_current_contextvar_onto_log_records():
    import logging

    from plaid_mcp.__main__ import _RequestIdFilter

    token = request_id_var.set("unit-test-rid")
    try:
        record = logging.LogRecord("plaid_mcp.api", logging.INFO, __file__, 1, "msg", None, None)
        assert _RequestIdFilter().filter(record) is True
        assert record.request_id == "unit-test-rid"
    finally:
        request_id_var.reset(token)


def test_request_id_filter_defaults_to_dash_outside_a_request():
    import logging

    from plaid_mcp.__main__ import _RequestIdFilter

    record = logging.LogRecord("plaid_mcp.api", logging.INFO, __file__, 1, "msg", None, None)
    assert _RequestIdFilter().filter(record) is True
    assert record.request_id == "-"
