"""The MCP server: the tools Claude sees, wired to Pluggy and market data. Data only, no interpretation."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import unicodedata
from collections import defaultdict
from datetime import date, datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .market import MarketData
from .pluggy import PluggyClient, PluggyError
from .settings import Settings, load_settings
from .transactions import normalize_transaction

log = logging.getLogger("finance_mcp")

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

INSTRUCTIONS = """\
Personal finance tools for the user (Brazil, amounts in BRL). Data comes from the user's
banks, cards and broker through Pluggy (Open Finance), plus B3 quotes and Tesouro Direto rates.

The tools return data only: no classification, budgets or verdicts. Interpreting it
(what is essential, what is a subscription, what fits a budget) is up to you and the user.

- Transaction amounts: negative = money out, on every account (credit-card purchases included).
  Categories are Pluggy's own; transfers between the user's accounts and card-bill payments
  show up as transactions too, so watch for double counting.
- Check get_connections when data looks stale; mention the last sync date.
- When unsure about anything (income, payday, what counts as essential), ask the user.
"""


class Finance:
    """Shared state and data loading used by the tools."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.tz = ZoneInfo(settings.timezone)
        self.pluggy = PluggyClient(settings.pluggy_client_id, settings.pluggy_client_secret, settings.pluggy_base_url)
        self.market = MarketData(settings.brapi_token)

    def today(self) -> date:
        return datetime.now(self.tz).date()

    def _require_items(self) -> list[str]:
        if not self.settings.pluggy_item_ids:
            raise PluggyError("No PLUGGY_ITEM_IDS configured on the server. Add your Item IDs to the .env file.")
        return self.settings.pluggy_item_ids

    async def accounts(self, account_type: str | None = None) -> list[dict[str, Any]]:
        items = self._require_items()
        per_item = await asyncio.gather(*(self.pluggy.get_accounts(i) for i in items))
        accounts = [a for group in per_item for a in group]
        return [a for a in accounts if account_type is None or a.get("type") == account_type]

    async def transactions(
        self, date_from: date, date_to: date, accounts: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        accounts = accounts if accounts is not None else await self.accounts()
        raw = await asyncio.gather(
            *(self.pluggy.get_transactions(a["id"], date_from.isoformat(), date_to.isoformat()) for a in accounts)
        )
        txs = [normalize_transaction(t, a, self.tz) for a, group in zip(accounts, raw) for t in group]
        txs.sort(key=lambda t: t["date"] or "")
        return txs


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def _r(x: float | None) -> float | None:
    return round(x, 2) if isinstance(x, (int, float)) else x


def create_server(settings: Settings) -> FastMCP:
    auth = None
    if settings.auth_mode == "github":
        from .auth import build_auth

        auth = build_auth(settings)
    elif settings.auth_mode == "none":
        if not ipaddress.ip_address(settings.host).is_loopback:
            raise SystemExit("AUTH_MODE=none is only allowed with HOST=127.0.0.1 (local testing).")
        log.warning("AUTH_MODE=none: no authentication. Local testing only!")
    else:
        raise SystemExit("AUTH_MODE must be 'github' or 'none'")

    mcp = FastMCP("Finance", instructions=INSTRUCTIONS, auth=auth)
    fin = Finance(settings)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    # ------------------------------------------------------------ connections

    @mcp.tool(annotations=READ_ONLY)
    async def get_connections() -> list[dict[str, Any]]:
        """Status of each bank/broker connection (Pluggy Item): institution, last sync, errors."""
        out = []
        for item_id in fin._require_items():
            item = await fin.pluggy.get_item(item_id)
            out.append(
                {
                    "item_id": item_id,
                    "institution": (item.get("connector") or {}).get("name"),
                    "status": item.get("status"),
                    "execution_status": item.get("executionStatus"),
                    "last_updated": item.get("lastUpdatedAt"),
                    "consent_expires": item.get("consentExpiresAt"),
                    "error": (item.get("error") or {}).get("message"),
                }
            )
        return out

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def refresh_connections() -> list[dict[str, Any]]:
        """Ask Pluggy to sync every connection with the banks now. Takes a few minutes; check get_connections afterwards."""
        out = []
        for item_id in fin._require_items():
            try:
                item = await fin.pluggy.refresh_item(item_id)
                out.append({"item_id": item_id, "status": item.get("status")})
            except PluggyError as e:
                out.append({"item_id": item_id, "error": str(e)})
        return out

    # ------------------------------------------------------------ accounts & cards

    @mcp.tool(annotations=READ_ONLY)
    async def get_balances() -> dict[str, Any]:
        """Balances of checking/savings accounts (not credit cards, not investments)."""
        accounts = await fin.accounts("BANK")
        rows = []
        for a in accounts:
            bank = a.get("bankData") or {}
            rows.append(
                {
                    "account": a.get("name") or a.get("marketingName"),
                    "subtype": a.get("subtype"),
                    "balance": _r(a.get("balance")),
                    "automatically_invested": _r(bank.get("automaticallyInvestedBalance")),
                    "overdraft_used": _r(bank.get("overdraftUsedLimit")),
                    "currency": a.get("currencyCode"),
                }
            )
        return {"accounts": rows, "total": _r(sum(a.get("balance") or 0 for a in accounts))}

    async def _cards() -> tuple[list[dict[str, Any]], float]:
        cards = await fin.accounts("CREDIT")
        bills = await asyncio.gather(*(fin.pluggy.get_bills(c["id"]) for c in cards))
        out, available_total = [], 0.0
        for card, card_bills in zip(cards, bills):
            cd = card.get("creditData") or {}
            available_total += cd.get("availableCreditLimit") or 0
            card_bills = sorted(card_bills, key=lambda b: b.get("dueDate") or "", reverse=True)[:4]
            out.append(
                {
                    "card": card.get("name") or card.get("marketingName"),
                    "brand": cd.get("brand"),
                    "credit_limit": _r(cd.get("creditLimit")),
                    "available_limit": _r(cd.get("availableCreditLimit")),
                    "current_bill_amount": _r(card.get("balance")),
                    "closing_date": cd.get("balanceCloseDate"),
                    "due_date": cd.get("balanceDueDate"),
                    "minimum_payment": _r(cd.get("minimumPayment")),
                    "recent_bills": [
                        {
                            "due_date": b.get("dueDate"),
                            "closing_date": b.get("billClosingDate"),
                            "total": _r(b.get("totalAmount")),
                            "minimum": _r(b.get("minimumPaymentAmount")),
                            "paid": _r(sum(p.get("amount") or 0 for p in b.get("payments") or [])),
                            "allows_installments": b.get("allowsInstallments"),
                            "finance_charges": b.get("financeCharges") or [],
                        }
                        for b in card_bills
                    ],
                }
            )
        return out, available_total

    @mcp.tool(annotations=READ_ONLY)
    async def get_credit_cards() -> dict[str, Any]:
        """Every credit card: limit, available limit, current bill, closing/due dates and the 4 most
        recent bills. Installments are in get_transactions (installment_number / total_installments)."""
        cards, available = await _cards()
        return {"cards": cards, "total_available_limit": _r(available)}

    # ------------------------------------------------------------ transactions

    @mcp.tool(annotations=READ_ONLY)
    async def get_transactions(
        date_from: Annotated[str, Field(description="YYYY-MM-DD")],
        date_to: Annotated[str | None, Field(description="YYYY-MM-DD, default today")] = None,
        search: Annotated[str | None, Field(description="Text to find in description/merchant")] = None,
        category: Annotated[str | None, Field(description="Pluggy category name (partial) or id prefix, e.g. '11' or 'Groceries'")] = None,
        account_type: Annotated[str | None, Field(description="'BANK' or 'CREDIT'")] = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 200,
    ) -> dict[str, Any]:
        """Transactions from all accounts and cards (negative amount = money out), with sums per Pluggy
        category. Every transaction is included: income, transfers, card-bill payments, investments."""
        d_from = date.fromisoformat(date_from)
        d_to = date.fromisoformat(date_to) if date_to else fin.today()
        accounts = await fin.accounts(account_type.upper() if account_type else None)
        txs = await fin.transactions(d_from, d_to, accounts)
        if search:
            s = _fold(search)
            txs = [t for t in txs if s in _fold(f"{t['merchant'] or ''} {t['description'] or ''} {t['description_raw'] or ''}")]
        if category:
            c = category.strip()
            if c.isdigit():
                while len(c) > 2 and c.endswith("00"):  # "11000000" -> "11", also matches subcategories
                    c = c[:-2]
                txs = [t for t in txs if (t["category_id"] or "").startswith(c)]
            else:
                txs = [t for t in txs if _fold(c) in _fold(t["category"] or "")]
        by_cat: dict[str, float] = defaultdict(float)
        for t in txs:
            by_cat[t["category"] or "Uncategorized"] += t["amount"]
        return {
            "count": len(txs),
            "shown": min(len(txs), limit),
            "total_in": _r(sum(t["amount"] for t in txs if t["amount"] > 0)),
            "total_out": _r(sum(-t["amount"] for t in txs if t["amount"] < 0)),
            "by_category": {c: _r(v) for c, v in sorted(by_cat.items(), key=lambda kv: kv[1])},
            "transactions": list(reversed(txs))[:limit],
        }

    @mcp.tool(annotations=READ_ONLY)
    async def list_categories() -> list[dict[str, Any]]:
        """Pluggy's category tree (id, name, parent). Ids/names can be used as get_transactions' category filter."""
        cats = await fin.pluggy.get_categories()
        return [
            {"id": c.get("id"), "name": c.get("description"), "name_pt": c.get("descriptionTranslated"), "parent": c.get("parentDescription")}
            for c in cats
        ]

    # ------------------------------------------------------------ investments & market

    @mcp.tool(annotations=READ_ONLY)
    async def get_investments() -> dict[str, Any]:
        """All active investments (stocks, FIIs, ETFs, Tesouro Direto, CDB/LCI/LCA, funds...) with
        balance totals by Pluggy type/subtype."""
        items = fin._require_items()
        groups = await asyncio.gather(*(fin.pluggy.get_investments(i) for i in items))
        invs = [i for g in groups for i in g if (i.get("status") or "ACTIVE") == "ACTIVE"]
        total = sum(i.get("balance") or 0 for i in invs)
        alloc: dict[str, float] = defaultdict(float)
        for i in invs:
            alloc[f"{i.get('type')}/{i.get('subtype') or '-'}"] += i.get("balance") or 0
        return {
            "total": _r(total),
            "allocation": [
                {"type": k, "balance": _r(v), "pct": _r(100 * v / total) if total else None}
                for k, v in sorted(alloc.items(), key=lambda kv: -kv[1])
            ],
            "positions": [
                {
                    "name": i.get("name"),
                    "code": i.get("code"),
                    "type": i.get("type"),
                    "subtype": i.get("subtype"),
                    "balance": _r(i.get("balance")),
                    "invested": _r(i.get("amountOriginal")),
                    "profit": _r(i.get("amountProfit")),
                    "quantity": i.get("quantity"),
                    "unit_value": _r(i.get("value")),
                    "rate": i.get("rate"),
                    "rate_type": i.get("rateType"),
                    "fixed_annual_rate": i.get("fixedAnnualRate"),
                    "last_12m_rate": i.get("lastTwelveMonthsRate"),
                    "due_date": i.get("dueDate"),
                    "issuer": i.get("issuer"),
                    "institution": (i.get("institution") or {}).get("name"),
                }
                for i in sorted(invs, key=lambda i: -(i.get("balance") or 0))
            ],
        }

    @mcp.tool(annotations=READ_ONLY)
    async def get_stock_quotes(
        tickers: Annotated[list[str], Field(min_length=1, max_length=20, description="B3 tickers, e.g. ['PETR4','ITSA4','HGLG11']")],
    ) -> list[dict[str, Any]]:
        """Current B3 quotes (stocks, FIIs, ETFs, BDRs) from brapi.dev, cached 15 min."""
        return await fin.market.quotes(tickers)

    @mcp.tool(annotations=READ_ONLY)
    async def get_tesouro_rates(
        bond_type: Annotated[str | None, Field(description="Filter, e.g. 'IPCA+', 'Selic', 'Prefixado', 'Renda+', 'Educa+'")] = None,
    ) -> dict[str, Any]:
        """Latest official Tesouro Direto buy/sell rates and prices for every bond."""
        data = await fin.market.tesouro_rates()
        if bond_type:
            f = bond_type.lower()
            data = {**data, "bonds": [b for b in data["bonds"] if f in b["type"].lower()]}
        return data

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO (account/item IDs, query strings): keep it out of the logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    settings = load_settings()
    mcp = create_server(settings)
    mcp.run(
        transport="http",
        host=settings.host,
        port=settings.port,
        path="/mcp",
        allowed_hosts=[settings.public_host, "localhost", "127.0.0.1"],
    )


if __name__ == "__main__":
    main()
