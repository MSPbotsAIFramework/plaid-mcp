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
"""Transaction sync (cursor-based, not the older /transactions/get)."""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import PlaidClient
from ..config import DEFAULT_TRANSACTIONS_COUNT, MAX_TRANSACTIONS_COUNT
from ._common import READ_ONLY, AccessToken, ClientFactory, compact, run_tool

Cursor = Annotated[
    str | None,
    Field(description="Cursor from a previous call's next_cursor; omit on the first call to get full history."),
]
Count = Annotated[
    int,
    Field(
        description=f"Transactions per page (default {DEFAULT_TRANSACTIONS_COUNT}, max {MAX_TRANSACTIONS_COUNT}).",
        ge=1,
        le=MAX_TRANSACTIONS_COUNT,
    ),
]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def plaid_sync_transactions(
        access_token: AccessToken, cursor: Cursor = None, count: Count = DEFAULT_TRANSACTIONS_COUNT
    ) -> str:
        """Get transaction changes since cursor (added/modified/removed).

        Omit cursor on the first call for full history, then call again with the returned
        next_cursor to get only what changed since -- not the whole history again.
        """

        async def action(client: PlaidClient) -> dict:
            body = compact({"access_token": access_token, "cursor": cursor, "count": count})
            payload = await client.post("/transactions/sync", body)
            return {
                "added": payload.get("added") or [],
                "modified": payload.get("modified") or [],
                "removed": payload.get("removed") or [],
                "next_cursor": payload.get("next_cursor"),
                "has_more": bool(payload.get("has_more")),
            }

        return await run_tool("plaid_sync_transactions", client_factory, action)
