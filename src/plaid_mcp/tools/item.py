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
"""Item lifecycle: inspect and permanently remove a bank connection."""

from mcp.server.fastmcp import FastMCP

from ..api_client import PlaidClient
from ._common import DESTRUCTIVE, READ_ONLY, AccessToken, ClientFactory, Confirm, run_tool


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def plaid_get_item(access_token: AccessToken) -> str:
        """Get an Item's institution, enabled products, and any error needing the end customer's attention."""

        async def action(client: PlaidClient) -> dict:
            payload = await client.post("/item/get", {"access_token": access_token})
            item = payload.get("item") or {}
            return {
                "item_id": item.get("item_id"),
                "institution": {
                    "institution_id": item.get("institution_id"),
                    "name": payload.get("institution_name"),
                },
                "available_products": item.get("available_products") or [],
                "billed_products": item.get("billed_products") or [],
                "consent_expiration_time": item.get("consent_expiration_time"),
                "error": item.get("error"),
            }

        return await run_tool("plaid_get_item", client_factory, action)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def plaid_remove_item(access_token: AccessToken, confirm: Confirm = None) -> str:
        """Permanently disconnect a bank connection (Item). Irreversible; needs confirm: true.

        Only call this after the end customer explicitly asked to disconnect. Without
        confirm: true, no request is sent to Plaid at all.
        """

        async def action(client: PlaidClient) -> dict:
            if confirm is not True:
                raise ValueError(
                    "Removing this Item is permanent. Call again with confirm: true only "
                    "after the end customer explicitly approved disconnecting it."
                )
            await client.post("/item/remove", {"access_token": access_token}, idempotent=False)
            return {"removed": True}

        return await run_tool("plaid_remove_item", client_factory, action)
