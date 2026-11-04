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
"""Account list/balances and routing/account numbers (Auth)."""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import PlaidClient
from ._common import READ_ONLY, AccessToken, AccountIds, ClientFactory, compact, run_tool

RefreshBalances = Annotated[
    bool,
    Field(
        description=(
            "false (default): return Plaid's last-known cached balances, fast. "
            "true: force a live check against the institution, slower and may fail with a "
            "retryable upstream error if the institution is temporarily unavailable."
        )
    ),
]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def plaid_get_accounts(
        access_token: AccessToken, account_ids: AccountIds = None, refresh_balances: RefreshBalances = False
    ) -> str:
        """List accounts on this Item with their balances.

        refresh_balances=true forces a live check against the institution instead of
        returning Plaid's cached balances; slower, and may fail retryable if the
        institution is temporarily unavailable.
        """

        async def action(client: PlaidClient) -> dict:
            options = compact({"account_ids": account_ids})
            body = compact({"access_token": access_token, "options": options or None})
            path = "/accounts/balance/get" if refresh_balances else "/accounts/get"
            payload = await client.post(path, body)
            return {"accounts": payload.get("accounts") or []}

        return await run_tool("plaid_get_accounts", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def plaid_get_auth(access_token: AccessToken, account_ids: AccountIds = None) -> str:
        """Get routing/account numbers (ACH, EFT, international, BACS) for accounts on this Item."""

        async def action(client: PlaidClient) -> dict:
            options = compact({"account_ids": account_ids})
            body = compact({"access_token": access_token, "options": options or None})
            payload = await client.post("/auth/get", body)
            numbers = payload.get("numbers") or {}
            return {
                "accounts": payload.get("accounts") or [],
                "numbers": {
                    "ach": numbers.get("ach") or [],
                    "eft": numbers.get("eft") or [],
                    "international": numbers.get("international") or [],
                    "bacs": numbers.get("bacs") or [],
                },
            }

        return await run_tool("plaid_get_auth", client_factory, action)
