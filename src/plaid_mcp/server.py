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
"""FastMCP factory: builds the server and registers every tool module."""

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from ._arguments import EnvelopeFastMCP
from .api_client import PlaidClient
from .config import Settings
from .middleware import get_client_credentials

INSTRUCTIONS = (
    "Plaid links one MSPbots tenant's agent to its end customers' bank "
    "accounts: account/balance data, routing/account numbers (Auth), "
    "transactions, identity, investment holdings and liabilities, plus the "
    "Link/Item lifecycle needed to create and remove a bank connection.\n"
    "Two-tier credential model: the X-Plaid-* request headers identify *who "
    "is calling* (one tenant's Plaid developer client_id/secret/env); the "
    "access_token argument on every data tool identifies *which end "
    "customer's bank connection* (Item) the call is about. One tenant "
    "manages many end customers' Items through the same headers.\n"
    "New connection flow: plaid_create_link_token -> send the returned "
    "hosted_link_url to the end customer -> poll plaid_get_link_session "
    "until status is completed -> plaid_exchange_public_token for an "
    "access_token -> pass that access_token to every data tool below. This "
    "server has no UI of its own and does not receive webhooks, so link "
    "completion and new transactions are discovered by polling, not pushed.\n"
    "Data tools (all take access_token): plaid_get_item, plaid_get_accounts "
    "(refresh_balances=true forces a live check instead of Plaid's cache), "
    "plaid_get_auth, plaid_sync_transactions (cursor-based: pass the "
    "previous call's next_cursor rather than re-fetching history), "
    "plaid_get_identity, plaid_get_investments_holdings, "
    "plaid_get_liabilities (the last two fail with a structured "
    "invalid_argument error, not a crash, when the Item was never linked "
    "with that product).\n"
    "plaid_remove_item permanently disconnects an Item; it needs "
    "confirm: true and must only be called after the end customer "
    "explicitly asked to disconnect."
)


def get_client_from_context() -> PlaidClient | None:
    """The client for the current request, or None outside a request."""
    credentials = get_client_credentials()
    if credentials is None:
        return None
    return PlaidClient(credentials)


def create_mcp_server(settings: Settings | None = None) -> FastMCP:
    """Build the FastMCP server and register every tool."""
    mcp = EnvelopeFastMCP(
        name="plaid-mcp",
        instructions=INSTRUCTIONS,
        # The service sits behind the gateway, so Host is never localhost;
        # DNS-rebinding protection would reject those requests with 421.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        stateless_http=True,
    )

    from .tools import accounts, identity, investments, item, liabilities, link, transactions

    # Registration order follows the tech design's build order: Item/Link
    # lifecycle first (needed before any data tool can be exercised
    # end-to-end), then the core read surface, then the two
    # narrower-sandbox-coverage products last.
    for module in (link, item, accounts, transactions, identity, investments, liabilities):
        module.register(mcp, get_client_from_context)

    return mcp
