"""The MCP server: the tools Claude sees, wired to Pluggy, market data and your config."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from . import analysis
from .analysis import Tx
from .config_store import ConfigStore
from .market import MarketData
from .pluggy import PluggyClient, PluggyError
from .settings import Settings, load_settings

log = logging.getLogger("finance_mcp")

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

INSTRUCTIONS = """\
Personal finance tools for the user (Brazil, amounts in BRL). Data comes from the user's
banks, cards and broker through Pluggy (Open Finance), plus B3 quotes and Tesouro Direto rates.

How to use them well:
- Call get_config first in a conversation about money. If income.monthly_net,
  income.payday or investing.monthly_target are missing (0), ASK the user before
  drawing conclusions, then save the answers with update_config.
- The monthly investment target is never spendable money: budgets and can_i_afford
  already subtract it. Say so explicitly when it matters.
- When a transaction looks misclassified (e.g. a subscription counted as essential),
  ask the user and fix it with update_config instead of silently ignoring it.
- Always ask before calling update_config, and tell the user what changed.
- Check get_connections when data looks stale; mention the last sync date.
- When unsure about anything (a purchase's urgency, whether an expense is recurring,
  which card will be used), ask the user rather than guessing.
- The user is an experienced investor: give data, trade-offs and numbers; they decide.
"""


class Finance:
    """Shared state and data loading used by the tools."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.tz = ZoneInfo(settings.timezone)
        self.pluggy = PluggyClient(settings.pluggy_client_id, settings.pluggy_client_secret, settings.pluggy_base_url)
        self.market = MarketData(settings.brapi_token)
        self.store = ConfigStore(settings.config_path, settings.backup_dir)

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

    async def transactions(self, date_from: date, date_to: date, accounts: list[dict[str, Any]] | None = None) -> list[Tx]:
        accounts = accounts if accounts is not None else await self.accounts()
        raw = await asyncio.gather(
            *(self.pluggy.get_transactions(a["id"], date_from.isoformat(), date_to.isoformat()) for a in accounts)
        )
        txs = [analysis.normalize_transaction(t, a, self.tz) for a, group in zip(accounts, raw) for t in group]
        txs.sort(key=lambda t: t.date)
        return txs

    async def analysis_transactions(self) -> list[Tx]:
        """Enough history for averages (lookback months) plus the current budget cycle."""
        config = self.store.load()
        today = self.today()
        w_start, _, _ = analysis.lookback_window(today, config.analysis.lookback_months)
        c_start, _ = analysis.budget_cycle(today, config.income.payday)
        return await self.transactions(min(w_start, c_start), today)


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
        today = fin.today()
        txs = await fin.transactions(today - timedelta(days=400), today, cards)
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
                        }
                        for b in card_bills
                    ],
                    "future_installments_by_month": analysis.committed_installments(
                        [t for t in txs if t.account_id == card["id"]], today
                    ),
                }
            )
        return out, available_total

    @mcp.tool(annotations=READ_ONLY)
    async def get_credit_cards() -> dict[str, Any]:
        """Every credit card: limit, available limit, current bill, closing/due dates, recent bills
        and installments already committed for the coming months."""
        cards, available = await _cards()
        return {"cards": cards, "total_available_limit": _r(available)}

    # ------------------------------------------------------------ transactions

    @mcp.tool(annotations=READ_ONLY)
    async def get_transactions(
        date_from: Annotated[str, Field(description="YYYY-MM-DD")],
        date_to: Annotated[str | None, Field(description="YYYY-MM-DD, default today")] = None,
        search: Annotated[str | None, Field(description="Text to find in description/merchant")] = None,
        category: Annotated[str | None, Field(description="Category name (partial) or Pluggy category id")] = None,
        only_spending: Annotated[bool, Field(description="Exclude income, own transfers, bill payments, investments")] = False,
        limit: Annotated[int, Field(ge=1, le=1000)] = 200,
    ) -> dict[str, Any]:
        """Transactions from all accounts and cards (negative amount = money out), with totals by category."""
        d_from = date.fromisoformat(date_from)
        d_to = date.fromisoformat(date_to) if date_to else fin.today()
        txs = await fin.transactions(d_from, d_to)
        config = fin.store.load()
        clf = analysis.Classifier(config.classification)
        if only_spending:
            txs = clf.spending(txs)
        if search:
            s = analysis._fold(search)
            txs = [t for t in txs if s in t.match_text]
        if category:
            txs = [t for t in txs if clf._category_match([category], t)]
        by_cat: dict[str, float] = defaultdict(float)
        for t in txs:
            by_cat[t.category or "Uncategorized"] += t.amount
        txs_sorted = sorted(txs, key=lambda t: t.date, reverse=True)
        return {
            "count": len(txs),
            "shown": min(len(txs), limit),
            "total_in": _r(sum(t.amount for t in txs if t.amount > 0)),
            "total_out": _r(sum(t.outflow for t in txs)),
            "by_category": {c: _r(v) for c, v in sorted(by_cat.items(), key=lambda kv: kv[1])},
            "transactions": [t.to_dict() for t in txs_sorted[:limit]],
        }

    @mcp.tool(annotations=READ_ONLY)
    async def list_categories() -> list[dict[str, Any]]:
        """Pluggy's category tree (id, name, parent). Use the ids/names in update_config classification rules."""
        cats = await fin.pluggy.get_categories()
        return [
            {"id": c.get("id"), "name": c.get("description"), "name_pt": c.get("descriptionTranslated"), "parent": c.get("parentDescription")}
            for c in cats
        ]

    # ------------------------------------------------------------ budgeting

    @mcp.tool(annotations=READ_ONLY)
    async def get_monthly_costs(
        kind: Annotated[Literal["essentials", "subscriptions", "all"], Field(description="Which costs to return")] = "all",
    ) -> dict[str, Any]:
        """Average monthly cost of ESSENTIALS (housing, utilities, groceries, health, transport...)
        and/or SUBSCRIPTIONS (recurring charges like streaming, apps, gym), over the last full months."""
        config = fin.store.load()
        result = analysis.monthly_costs(await fin.analysis_transactions(), config, fin.today())
        result.pop("_subscription_keys")
        if kind == "essentials":
            result.pop("subscriptions")
        elif kind == "subscriptions":
            result.pop("essentials")
        return result

    @mcp.tool(annotations=READ_ONLY)
    async def get_spending_budget() -> dict[str, Any]:
        """How much is free to spend this pay cycle WITHOUT touching the monthly investment target:
        income - investment target - essentials - subscriptions - discretionary already spent."""
        config = fin.store.load()
        return analysis.spending_budget(await fin.analysis_transactions(), config, fin.today())

    @mcp.tool(annotations=READ_ONLY)
    async def can_i_afford(
        amount: Annotated[float, Field(gt=0, description="Total price in BRL")],
        installments: Annotated[int, Field(ge=1, le=48, description="1 = paid at once")] = 1,
        on_credit_card: Annotated[bool, Field(description="Paid with credit card (checks the available limit)")] = True,
        essential: Annotated[bool, Field(description="Is this a need rather than a want?")] = False,
    ) -> dict[str, Any]:
        """Simulate a purchase: impact on this cycle's free budget, on future months (with existing
        installments) and on the card limit. Verdicts: fits, fits_but_tight, cuts_into_investment, exceeds_card_limit."""
        config = fin.store.load()
        today = fin.today()
        txs = await fin.analysis_transactions()
        budget = analysis.spending_budget(txs, config, today)
        available = None
        commitments: dict[str, float] = {}
        if on_credit_card:
            cards, available = await _cards()
            for c in cards:
                for m, v in c["future_installments_by_month"].items():
                    commitments[m] = commitments.get(m, 0) + v
        result = analysis.affordability(
            budget, commitments, amount, installments, today, available, essential, config.analysis.tight_threshold_pct
        )
        result["budget_cycle"] = budget["cycle"]
        return result

    # ------------------------------------------------------------ investments & market

    @mcp.tool(annotations=READ_ONLY)
    async def get_investments() -> dict[str, Any]:
        """All investments (stocks, FIIs, ETFs, Tesouro Direto, CDB/LCI/LCA, funds...) with allocation
        by type and progress of the emergency fund."""
        items = fin._require_items()
        groups = await asyncio.gather(*(fin.pluggy.get_investments(i) for i in items))
        invs = [i for g in groups for i in g if (i.get("status") or "ACTIVE") == "ACTIVE"]
        total = sum(i.get("balance") or 0 for i in invs)
        alloc: dict[str, float] = defaultdict(float)
        for i in invs:
            alloc[f"{i.get('type')}/{i.get('subtype') or '-'}"] += i.get("balance") or 0
        config = fin.store.load()
        ef_names = [n.lower() for n in config.investing.emergency_fund_investment_names]
        ef = sum(i.get("balance") or 0 for i in invs if any(n in (i.get("name") or "").lower() for n in ef_names))
        return {
            "total": _r(total),
            "allocation": [
                {"type": k, "balance": _r(v), "pct": _r(100 * v / total) if total else None}
                for k, v in sorted(alloc.items(), key=lambda kv: -kv[1])
            ],
            "emergency_fund": {
                "current": _r(ef),
                "target": config.investing.emergency_fund_target,
                "configured_names": config.investing.emergency_fund_investment_names,
            },
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

    # ------------------------------------------------------------ config

    @mcp.tool(annotations=READ_ONLY)
    async def get_config() -> dict[str, Any]:
        """The user's finance rules: income, payday, investment target, emergency fund, fixed costs,
        classification rules for essentials/subscriptions, and notes."""
        return fin.store.load().model_dump(mode="json")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    async def update_config(
        changes: Annotated[
            dict[str, Any],
            Field(
                description="Partial config to merge (same shape as get_config). Lists REPLACE the old list, so send "
                "the full list. Example: {'income': {'monthly_net': 8500, 'payday': 5}, "
                "'classification': {'subscription_merchants': ['spotify', 'netflix']}}"
            ),
        ],
        reason: Annotated[str, Field(description="Why this change is being made (kept in the server log)")],
    ) -> dict[str, Any]:
        """Change the user's finance rules. Always confirm with the user first. The previous version is backed up."""
        try:
            _, diff = fin.store.update(changes)
        except ValidationError as e:
            return {"ok": False, "error": "Invalid config, nothing was changed.", "details": e.errors(include_url=False)}
        # Log which fields changed, not the values (they are personal financial data).
        log.info("Config updated (%s): %s", reason, [line.split(":", 1)[0] for line in diff])
        return {"ok": True, "changes": diff or ["nothing changed"]}

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
