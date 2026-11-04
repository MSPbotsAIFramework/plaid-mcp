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
"""Identity: names/addresses/emails/phones Plaid has on file for an Item's accounts."""

from mcp.server.fastmcp import FastMCP

from ..api_client import PlaidClient
from ._common import READ_ONLY, AccessToken, AccountIds, ClientFactory, compact, run_tool


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def plaid_get_identity(access_token: AccessToken, account_ids: AccountIds = None) -> str:
        """Get account-holder identity (names, addresses, emails, phone numbers) for accounts on this Item."""

        async def action(client: PlaidClient) -> dict:
            options = compact({"account_ids": account_ids})
            body = compact({"access_token": access_token, "options": options or None})
            payload = await client.post("/identity/get", body)
            return {"accounts": payload.get("accounts") or []}

        return await run_tool("plaid_get_identity", client_factory, action)
