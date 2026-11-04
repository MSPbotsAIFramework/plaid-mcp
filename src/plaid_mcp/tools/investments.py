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
"""Investment holdings. Narrower Sandbox coverage than Transactions/Auth (see
docs/tickets/PRD-21315.md Risks & dependencies), registered last so gaps here
do not block the rest of the service.
"""

from mcp.server.fastmcp import FastMCP

from ..api_client import PlaidClient
from ._common import READ_ONLY, AccessToken, AccountIds, ClientFactory, compact, run_tool


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def plaid_get_investments_holdings(access_token: AccessToken, account_ids: AccountIds = None) -> str:
        """Get investment holdings and the securities they reference for accounts on this Item.

        Returns a structured invalid_argument error (not an exception) if the Item was never
        linked with the investments product enabled.
        """

        async def action(client: PlaidClient) -> dict:
            options = compact({"account_ids": account_ids})
            body = compact({"access_token": access_token, "options": options or None})
            payload = await client.post("/investments/holdings/get", body)
            return {
                "accounts": payload.get("accounts") or [],
                "holdings": payload.get("holdings") or [],
                "securities": payload.get("securities") or [],
            }

        return await run_tool("plaid_get_investments_holdings", client_factory, action)
