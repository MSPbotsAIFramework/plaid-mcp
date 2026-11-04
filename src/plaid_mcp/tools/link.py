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
"""Hosted Link session lifecycle: create, poll, exchange.

This is the only multi-step flow in the service; every other tool is a
single request/response (see Tech design -> Key flow in docs/tickets/PRD-21315.md).
"""

from datetime import datetime, timezone
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import PlaidClient
from ._common import READ_ONLY, WRITES, ClientFactory, run_tool

# Plaid requires client_name/language/country_codes on link/token/create; these
# are not meaningful per-call choices for this service (one MSPbots tenant,
# English, US institutions for the first version) so they are fixed here
# rather than exposed as tool parameters the agent would have to guess at.
_CLIENT_NAME = "MSPbots Agent"
_DEFAULT_COUNTRY_CODES = ["US"]
_DEFAULT_LANGUAGE = "en"

Products = Annotated[
    list[str],
    Field(description='Plaid products to request, e.g. ["transactions","auth"]. See Plaid\'s product list.', min_length=1),
]
ClientUserId = Annotated[
    str, Field(description="Your own stable identifier for the end customer linking their bank.", min_length=1)
]
LinkToken = Annotated[str, Field(description="The link_token returned by plaid_create_link_token.", min_length=1)]
DeliveryMethod = Annotated[
    str | None,
    Field(description='How Plaid should deliver the hosted_link_url to the end customer (e.g. "sms"); omit to only return the URL for the caller to send.'),
]
RedirectUri = Annotated[
    str | None,
    Field(description="Where Plaid sends the end customer back to after completing Hosted Link, if applicable."),
]
PublicToken = Annotated[
    str,
    Field(description="The public_token returned once a Hosted Link session completes (plaid_get_link_session). Single-use.", min_length=1),
]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=WRITES)
    async def plaid_create_link_token(
        products: Products,
        client_user_id: ClientUserId,
        hosted_link: bool = True,
        delivery_method: DeliveryMethod = None,
        redirect_uri: RedirectUri = None,
    ) -> str:
        """Start a new bank-linking session and return a Hosted Link URL for the end customer to open.

        Send the returned hosted_link_url to the end customer out of band (this server has no
        UI of its own); poll plaid_get_link_session with the returned link_token to see when
        they finish. Not idempotent: each call mints a fresh session.
        """

        async def action(client: PlaidClient) -> dict:
            body: dict[str, Any] = {
                "client_name": _CLIENT_NAME,
                "language": _DEFAULT_LANGUAGE,
                "country_codes": _DEFAULT_COUNTRY_CODES,
                "user": {"client_user_id": client_user_id},
                "products": products,
            }
            if hosted_link:
                hosted: dict[str, Any] = {}
                if delivery_method:
                    hosted["delivery_method"] = delivery_method
                if redirect_uri:
                    hosted["completion_redirect_uri"] = redirect_uri
                body["hosted_link"] = hosted
            payload = await client.post("/link/token/create", body, idempotent=False)
            return {
                "link_token": payload.get("link_token"),
                "hosted_link_url": payload.get("hosted_link_url"),
                "expiration": payload.get("expiration"),
            }

        return await run_tool("plaid_create_link_token", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def plaid_get_link_session(link_token: LinkToken) -> str:
        """Check a Hosted Link session: pending (not finished), completed (has a public_token), or expired."""

        async def action(client: PlaidClient) -> dict:
            payload = await client.post("/link/token/get", {"link_token": link_token})
            return _session_status(payload)

        return await run_tool("plaid_get_link_session", client_factory, action)

    @mcp.tool(annotations=WRITES)
    async def plaid_exchange_public_token(public_token: PublicToken) -> str:
        """Exchange a completed Hosted Link session's public_token for a reusable access_token.

        The public_token is single-use: exchanging it a second time fails. Store the returned
        access_token yourself -- this server keeps no state between calls.
        """

        async def action(client: PlaidClient) -> dict:
            payload = await client.post("/item/public_token/exchange", {"public_token": public_token}, idempotent=False)
            return {"access_token": payload.get("access_token"), "item_id": payload.get("item_id")}

        return await run_tool("plaid_exchange_public_token", client_factory, action)


def _session_status(payload: dict) -> dict:
    """Map a /link/token/get response to {status, public_token, institution}.

    ASSUMPTION flagged for real-sandbox verification (see docs/tickets/PRD-21315.md
    Self-test): a completed Hosted Link session's result is read from
    ``link_sessions[].results.item_add_results[].public_token`` with a
    ``session.public_token`` / ``results.public_token`` fallback for other
    historical shapes. "Still pending" vs. "expired" falls back to comparing
    the response's own ``expiration`` timestamp to now, which is documented
    and unambiguous regardless of the session-result nesting.
    """
    public_token, institution = _find_completion(payload)
    if public_token:
        return {"status": "completed", "public_token": public_token, "institution": institution}
    expiration = payload.get("expiration")
    if expiration and _is_past(expiration):
        return {"status": "expired", "public_token": None, "institution": None}
    return {"status": "pending", "public_token": None, "institution": None}


def _find_completion(payload: dict) -> tuple[str | None, dict | None]:
    for session in payload.get("link_sessions") or []:
        results = session.get("results") or {}
        for item_result in results.get("item_add_results") or []:
            token = item_result.get("public_token")
            if token:
                return token, item_result.get("institution")
        token = session.get("public_token") or results.get("public_token")
        if token:
            return token, session.get("institution") or results.get("institution")
    return None, None


def _is_past(iso_timestamp: str) -> bool:
    try:
        ts = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts <= datetime.now(timezone.utc)
