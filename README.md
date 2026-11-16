# Finance MCP

A private MCP server that gives Claude (claude.ai) **read-only** access to your real financial data through **Pluggy** (Open Finance Brasil), plus B3 quotes and Tesouro Direto rates.

The tools only return data. They don't classify transactions, compute budgets or give verdicts. Claude does the interpreting in the conversation, with you.

```
claude.ai ──HTTPS──▶ VPS ┬─ caddy        (HTTPS, certificates, routes domains)
                         └─ finance-mcp  (this repo: Python + FastMCP)
                                ├─▶ Pluggy API          banks, cards, investments
                                ├─▶ brapi.dev           B3 quotes
                                └─▶ Tesouro Transparente official Tesouro Direto rates
```

## Tools Claude gets

| Tool | What it does |
|---|---|
| `get_connections` / `refresh_connections` | Sync status of each institution / ask Pluggy to sync now |
| `get_balances` | Checking and savings balances |
| `get_credit_cards` | Limits, current bill, closing and due dates, recent bills |
| `get_transactions` | All transactions (income, transfers, bill payments included) by period, text, Pluggy category or account type, with sums per category. Amount < 0 = money out on every account |
| `list_categories` | Pluggy's category tree, usable as the `get_transactions` category filter |
| `get_investments` | Positions and balance totals by type |
| `get_stock_quotes` | B3 quotes |
| `get_tesouro_rates` | Latest Tesouro Direto rates and prices |

Nothing here can move money. Pluggy's data API is read-only, and the server only writes its own OAuth login state to `/data`.

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
  transactions.py flattens Pluggy transactions (pure function, unit-tested)
  pluggy.py       Pluggy API client
  market.py       brapi + Tesouro data
  auth.py         GitHub OAuth with allowlist
  check.py        `python -m finance_mcp.check`: validates your .env
scripts/connect.py  connect a bank and get its Item ID (runs on your computer)
tests/            pytest
```

## Get started

1. [docs/DOCKER.md](docs/DOCKER.md): how Docker works, explained with these files.
2. [docs/SETUP.md](docs/SETUP.md): Pluggy, `.env`, VPS, claude.ai, step by step.

Data comes from your institutions through Pluggy and can be a day or so behind. The tools give the numbers; the decisions are yours.
