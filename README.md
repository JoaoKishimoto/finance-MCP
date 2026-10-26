# Finance MCP

A private MCP server that gives Claude (claude.ai) **read-only** access to your real financial data through **Pluggy** (Open Finance Brasil), plus budgeting tools. It's meant to help with decisions like these:

- *Can I buy this now? In installments?* The answer accounts for your credit card bills and the installments you've already committed to.
- *How much can I still spend before payday* **without touching my monthly investment amount**?
- *What do my **essentials** cost per month? And **subscriptions** alone?*
- *What does my portfolio look like, and what are today's stock prices and Tesouro Direto rates?*

```
claude.ai ──HTTPS──▶ VPS ┬─ caddy        (HTTPS, certificates, routes domains)
                         └─ finance-mcp  (this repo: Python + FastMCP)
                                ├─▶ Pluggy API          banks, cards, investments
                                ├─▶ brapi.dev           B3 quotes
                                ├─▶ Tesouro Transparente official Tesouro Direto rates
                                └── /data/config.yaml   your rules (Claude can edit, auto-backup)
```

## Tools Claude gets

| Tool | What it does |
|---|---|
| `get_connections` / `refresh_connections` | Sync status of each institution / ask Pluggy to sync now |
| `get_balances` | Checking and savings balances |
| `get_credit_cards` | Limits, current bill, closing and due dates, recent bills, future installments by month |
| `get_transactions` | Search by period, text or category, with totals |
| `list_categories` | Pluggy's category tree, used to fine-tune the classification rules |
| `get_monthly_costs` | Monthly average of **essentials** and/or **subscriptions** (recurring charges are detected automatically) |
| `get_spending_budget` | Income − investment target − essentials − subscriptions − spent so far = free to spend until payday |
| `can_i_afford` | Simulates a purchase (cash or installments) against your budget, future installments and card limit |
| `get_investments` | Positions, allocation, emergency-fund progress |
| `get_stock_quotes` | B3 quotes |
| `get_tesouro_rates` | Latest Tesouro Direto rates and prices |
| `get_config` / `update_config` | Read or change your rules (validated, each change backed up) |

Nothing here can move money. Pluggy's data API is read-only, and the only things the server writes are your own `config.yaml` and its backups.

## Security

- claude.ai logs in with **OAuth ("Sign in with GitHub")**, and only the usernames in `ALLOWED_GITHUB_USERS` are accepted.
- Only Claude's own callback URL and Hermes' loopback callback (`http://127.0.0.1:8765/callback`) can receive login codes.
- Secrets live only in `.env` on the VPS, which is excluded from git and from the Docker image.
- The container runs as a non-root user with a read-only filesystem, a memory cap, and no published ports. It's only reachable through Caddy.

## Project layout

```
Dockerfile, docker-compose.yml   the MCP container (see docs/DOCKER.md)
.env.example                     template for your secrets (see docs/SETUP.md)
proxy/                           shared Caddy reverse proxy → copy to /opt/proxy on the VPS
src/finance_mcp/
  server.py       the MCP tools
  analysis.py     budgeting logic (pure functions, unit-tested)
  pluggy.py       Pluggy API client
  market.py       brapi + Tesouro data
  config_store.py your rules: schema, validation, backups
  auth.py         GitHub OAuth with allowlist
  check.py        `python -m finance_mcp.check`: validates your .env
scripts/connect.py  connect a bank and get its Item ID (runs on your computer)
tests/            pytest
```

## Get started

1. [docs/DOCKER.md](docs/DOCKER.md): how Docker works, explained with these files.
2. [docs/SETUP.md](docs/SETUP.md): Pluggy, `.env`, VPS, claude.ai, step by step.

Data comes from your institutions through Pluggy and can be a day or so behind. The tools give numbers and trade-offs; the decisions are yours.
