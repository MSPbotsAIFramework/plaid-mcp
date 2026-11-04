# Plaid MCP Server

An [MCP](https://modelcontextprotocol.io) server for the [Plaid](https://plaid.com) API. It
lets an AI agent, acting on behalf of one MSPbots tenant, read Plaid-linked bank account data
for an end customer -- account list and balances, routing/account numbers (Auth), transactions,
identity, investment holdings and liabilities -- and kick off linking a new bank account via
Plaid's Hosted Link, then remove (unlink) a connection on explicit confirmation.

- Transport: MCP Streamable HTTP at `POST /mcp`, health probe at `GET /health`
- Stateless and multi-tenant: the three `X-Plaid-*` credential headers arrive on every request
  and are never stored; every tool is listed from the first `tools/list`
- Python 3.12, [uv](https://docs.astral.sh/uv/), FastMCP, httpx
- License: Apache-2.0, Copyright 2026 MSPbots.ai

<div align="center">

## MSPbots Connector

**The MCP Gateway built for MSPs: visible, controlled, and your data stays where it belongs.**

[**Get started at connect.mspbots.ai →**](https://connect.mspbots.ai)

</div>

---

MSPbots Connector gives your AI clients one MCP URL to reach your entire MSP stack (RMM, PSA, documentation, security, finance), and gives you control over who can call which tool and a record of everything that was called.

**Hosted service: [connect.mspbots.ai](https://connect.mspbots.ai)** · Free to use.

### Why Connector

![MSPbots Connector](https://stgoss.mspbots.ai/files/mcp/mspbots-mcp.png)

| | |
|---|---|
| **Visible** | Every tool call is logged: who, which vendor, when. Usage dashboard, CSV export, SIEM streaming. |
| **Controlled** | Role-based, per-tool allowlist. Deny by default. |
| **Protected** | Credentials encrypted per record. AI clients never see your vendor passwords or tokens. |
| **MSP-native** | Built for the MSP stack only: RMM, PSA, documentation, security, finance. |

### Quick start

1. **Sign up** at [connect.mspbots.ai](https://connect.mspbots.ai).
2. **Connect your vendors.** Pick from 150+ connectors and authorize each one on the vendor's own page, or bring your own API keys. No extra vendor accounts needed.
3. **Set access.** Create roles and decide which tools each role may call. Nothing is allowed until you allow it.
4. **Add your MCP URL to your AI client.** Copy the URL from your Connector dashboard into Claude, ChatGPT, Cursor, VS Code, Gemini, Codex, or any MCP-capable client.
5. **Ask your AI to do real work**, and watch every call appear in the audit log.

### Features

**Unified gateway**
- One MCP URL for all your AI clients
- 150+ connectors across the MSP stack
- Custom MCP servers supported, free
- Authorization happens on each vendor's own page; the AI never sees passwords or tokens
- Bring your own credentials: use the vendor API keys you already have
- Multiple accounts per app; connect once, reuse across AI clients
- Tool search: finds the right action for the task
- One sign-in; vendor credentials are injected behind the scenes and OAuth tokens refresh automatically

**Visibility**
- Audit log for every tool call (who, which vendor, when)
- Usage dashboard: call volume, top vendors, error rate, per-user activity
- Export audit logs to CSV, or stream to Loki, Graylog, or LogScale (SIEM)
- Optional recording of tool arguments and result summaries (off by default, enabled per organization)

**Control**
- Per-role, per-tool allowlist, deny by default
- owner / admin / member roles
- SCIM provisioning with Entra ID, Okta, JumpCloud, and Google Workspace

**Security**
- Credentials encrypted one record at a time (AES-256-GCM, per-record key)
- AI clients authenticate with OAuth 2.1 + PKCE
- Vendor responses are scanned for prompt-injection patterns and returned as structured data. This is one layer of defense, not a guarantee against every attack.
- Static egress IP for customer firewall and API allowlisting

### Pricing

**Free.**

Links: [connect.mspbots.ai](https://connect.mspbots.ai) · [mspbots.ai](https://mspbots.ai)

---

## Authentication headers

| Header | Required | Type | Default | Allowed values | Description | Example |
|---|---|---|---|---|---|---|
| `X-Plaid-Client-Id` | Yes | string | none | - | Plaid developer `client_id`, from the Plaid Dashboard | `5f8a...` |
| `X-Plaid-Secret` | Yes | string | none | - | Plaid developer `secret` matching the environment below | `shhh...` |
| `X-Plaid-Env` | Yes | string | none | `sandbox`, `production` | Which Plaid base URL to call (Plaid retired the separate "development" environment; only these two exist today) | `sandbox` |

Where to get them: sign up (free) on the [Plaid Dashboard](https://dashboard.plaid.com), which
issues a `client_id`/`secret` pair for the Sandbox environment immediately; Production access
requires Plaid's standard approval step on the same Dashboard (not a flag on this server).

Missing or invalid any of the three -- including an `X-Plaid-Env` value that is neither
`sandbox` nor `production` -- gets HTTP 401 naming all three required headers; none of the
submitted values are echoed back.

`access_token` is **not** a header: it identifies which end customer's bank connection (Item) a
call is about, not who is calling, so it travels as an ordinary argument on every data tool
below. One tenant's `client_id`/`secret` manages many end customers' `access_token`s.

## Tools

All tools return a JSON string. `plaid_get_accounts`, `plaid_get_auth`, `plaid_get_identity`,
`plaid_get_investments_holdings` and `plaid_get_liabilities` accept an optional
`account_ids: list[str]` filter. **write** marks tools that change data; **confirm** marks
`plaid_remove_item`, which also needs `confirm: true`.

| Area | Tools |
|---|---|
| Link (new connection) | **write**: `plaid_create_link_token` (**products**, **client_user_id**, hosted_link, delivery_method, redirect_uri), `plaid_get_link_session` (**link_token**), `plaid_exchange_public_token` (**public_token**) |
| Item | `plaid_get_item` (**access_token**); **confirm**: `plaid_remove_item` (**access_token**) |
| Accounts | `plaid_get_accounts` (**access_token**, account_ids, refresh_balances), `plaid_get_auth` (**access_token**, account_ids) |
| Transactions | `plaid_sync_transactions` (**access_token**, cursor, count) |
| Identity | `plaid_get_identity` (**access_token**, account_ids) |
| Investments | `plaid_get_investments_holdings` (**access_token**, account_ids) |
| Liabilities | `plaid_get_liabilities` (**access_token**, account_ids) |

New-connection flow: `plaid_create_link_token` returns a `hosted_link_url` to send the end
customer -- poll `plaid_get_link_session` until `status=completed` -- `plaid_exchange_public_token`
for an `access_token` -- pass that `access_token` to every tool below Item.

Errors come back as a JSON envelope instead of an exception:

```json
{"error": {"code": "invalid_argument", "message": "Investments is not enabled for this Item. Re-link with the investments product to use this tool.", "retryable": false}}
```

`code` is one of `not_configured`, `unauthorized`, `not_found`, `invalid_argument`,
`rate_limited`, `upstream_error`. Reads are retried with backoff on 429 and 5xx; the
non-idempotent Link/exchange calls are retried only on 429, honoring `Retry-After` when present.

## Run locally

```bash
uv sync
uv run python -m plaid_mcp          # listens on 0.0.0.0:8080
```

or with Docker:

```bash
docker build --platform linux/amd64 -t plaid-mcp:dev .
docker run --rm -p 8080:8080 plaid-mcp:dev
```

Configuration (environment variables, all optional): `MCP_HTTP_HOST` (default `0.0.0.0`),
`MCP_HTTP_PORT` (default `8080`), `LOG_LEVEL` (default `INFO`). Credentials are never read
from the environment.

## Testing example

```bash
# list tools
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Plaid-Client-Id: your-client-id" \
  -H "X-Plaid-Secret: your-secret" \
  -H "X-Plaid-Env: sandbox" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# accounts for a linked Item
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Plaid-Client-Id: your-client-id" \
  -H "X-Plaid-Secret: your-secret" \
  -H "X-Plaid-Env: sandbox" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"plaid_get_accounts","arguments":{"access_token":"access-sandbox-..."}}}'
```

Unit tests (no network access needed):

```bash
uv run pytest
```

## Delivery notes

- **New service, first version (PRD-21315).** No existing tenants depend on a prior tool/header
  shape; future changes follow SOP §13 (breaking vs. compatible) starting from this version's
  `tools/list` snapshot.
- **Two-tier credential model.** The three `X-Plaid-*` headers identify the calling tenant;
  `access_token` is a plain tool argument identifying which end customer's Item a call is about
  -- see Authentication headers above and the Tech design "Conclusion" in
  `docs/tickets/PRD-21315.md`.
- **Hosted Link only.** No embedded (client-side SDK) Link flow and no webhook receiver -- this
  service has no UI and is not publicly reachable. Link completion and new transactions are
  discovered by polling (`plaid_get_link_session`, `/transactions/sync`'s own cursor), not pushed.
- **Scope.** Covers Auth, Transactions (sync), Identity, Investments, Liabilities and the
  Link/Item lifecycle. Transfer/Payments, Income and Monitor are explicitly out of scope for
  this version (see Non-goals in `docs/tickets/PRD-21315.md`).
- **Hosted Link session JSON shape is a flagged assumption.** `plaid_get_link_session`'s mapping
  of Plaid's `/link/token/get` response to `{status, public_token, institution}` was written
  from documented behavior without a live Sandbox call to confirm the exact field nesting; see
  the Self-test section of `docs/tickets/PRD-21315.md` for what to re-verify against a real
  Plaid Sandbox account.
- **No official `plaid-python` SDK.** A hand-rolled `httpx` client was chosen to match the house
  convention already used by every other first-party MCP here; see Tech design → Option analysis.

## License

Apache License 2.0 - see [LICENSE](LICENSE) and [NOTICE](NOTICE).
