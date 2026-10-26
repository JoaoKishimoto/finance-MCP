"""Pure budgeting logic (no network): classification, recurring detection, budgets.

Kept free of I/O so it can be unit-tested with fake transactions (see tests/).

Sign convention used everywhere here: `Tx.amount` < 0 means money left you.
Pluggy reports bank-account debits as negative but credit-card purchases as
positive, so `normalize_transaction` flips card amounts.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .config_store import Classification, FinanceConfig

INCOME_CATEGORY = "01"
INVESTMENT_CATEGORY = "03"


# ------------------------------------------------------------------ transactions


@dataclass
class Tx:
    id: str
    date: date
    description: str
    amount: float  # negative = money out
    category: str
    category_id: str
    merchant: str
    account_id: str
    account_name: str
    account_type: str  # BANK | CREDIT
    installment_number: int | None = None
    total_installments: int | None = None
    status: str = "POSTED"

    @property
    def outflow(self) -> float:
        return -self.amount if self.amount < 0 else 0.0

    @property
    def is_installment(self) -> bool:
        return bool(self.total_installments and self.total_installments > 1)

    @property
    def merchant_key(self) -> str:
        return merchant_key(self.merchant or self.description)

    @property
    def match_text(self) -> str:
        return _fold(f"{self.merchant} {self.description}")

    def to_dict(self) -> dict[str, Any]:
        d = {
            "date": self.date.isoformat(),
            "description": self.description,
            "amount": round(self.amount, 2),
            "category": self.category,
            "merchant": self.merchant or None,
            "account": self.account_name,
        }
        if self.is_installment:
            d["installment"] = f"{self.installment_number}/{self.total_installments}"
        if self.status != "POSTED":
            d["status"] = self.status
        return d


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


_INSTALLMENT_RE = re.compile(r"\b(parc(ela)?\.?\s*)?\d{1,2}\s*/\s*\d{1,2}\b")


def merchant_key(text: str) -> str:
    """Normalise a merchant/description so charges from the same place group together."""
    text = _fold(text)
    text = _INSTALLMENT_RE.sub(" ", text)
    text = re.sub(r"[^a-z ]+", " ", text)
    return " ".join(text.split())


def _parse_date(value: str | None, tz: ZoneInfo) -> date:
    if not value:
        return date.min
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz)
    return dt.date()


def normalize_transaction(raw: dict[str, Any], account: dict[str, Any], tz: ZoneInfo) -> Tx:
    account_type = account.get("type", "BANK")
    amount = float(raw.get("amount") or 0)
    if account_type == "CREDIT":
        amount = -amount
    merchant = raw.get("merchant") or {}
    card = raw.get("creditCardMetadata") or {}
    return Tx(
        id=raw.get("id", ""),
        date=_parse_date(raw.get("date"), tz),
        description=raw.get("description") or raw.get("descriptionRaw") or "",
        amount=amount,
        category=raw.get("category") or "",
        category_id=raw.get("categoryId") or "",
        merchant=merchant.get("name") or merchant.get("businessName") or "",
        account_id=account.get("id", ""),
        account_name=account.get("name") or account.get("marketingName") or "",
        account_type=account_type,
        installment_number=card.get("installmentNumber"),
        total_installments=card.get("totalInstallments"),
        status=raw.get("status") or "POSTED",
    )


# ------------------------------------------------------------------ classification


def _category_prefix(entry: str) -> str:
    # "18000000" -> "18", "05080000" -> "0508"
    while len(entry) > 2 and entry.endswith("00"):
        entry = entry[:-2]
    return entry


class Classifier:
    def __init__(self, rules: Classification):
        self.rules = rules

    @staticmethod
    def _category_match(entries: Iterable[str], tx: Tx) -> bool:
        for entry in entries:
            entry = entry.strip()
            if not entry:
                continue
            if entry.isdigit():
                if tx.category_id.startswith(_category_prefix(entry)):
                    return True
            elif _fold(entry) in _fold(tx.category):
                return True
        return False

    @staticmethod
    def _merchant_match(entries: Iterable[str], tx: Tx) -> bool:
        text = tx.match_text
        return any(_fold(e.strip()) in text for e in entries if e.strip())

    def is_excluded(self, tx: Tx) -> bool:
        return self._merchant_match(self.rules.ignored_merchants, tx) or self._category_match(
            self.rules.excluded_categories, tx
        )

    def is_essential(self, tx: Tx) -> bool:
        if self._merchant_match(self.rules.not_essential_merchants, tx):
            return False
        if self._merchant_match(self.rules.essential_merchants, tx):
            return True
        return self._category_match(self.rules.essential_categories, tx)

    def subscription_hint(self, tx: Tx) -> bool | None:
        """True/False when the rules decide, None when it depends on recurrence detection."""
        if self._merchant_match(self.rules.not_subscription_merchants, tx):
            return False
        if self._merchant_match(self.rules.subscription_merchants, tx):
            return True
        if self._category_match(self.rules.subscription_categories, tx):
            return True
        return None

    def spending(self, txs: Iterable[Tx]) -> list[Tx]:
        """Real consumption: money out, minus transfers between your accounts, card-bill payments, investments..."""
        return [t for t in txs if t.outflow > 0 and not self.is_excluded(t)]


# ------------------------------------------------------------------ periods


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def lookback_window(today: date, months: int) -> tuple[date, date, list[str]]:
    """The last `months` FULL calendar months before today's month."""
    first_this_month = today.replace(day=1)
    start = add_months(first_this_month, -months)
    end = first_this_month - timedelta(days=1)
    keys = [month_key(add_months(start, i)) for i in range(months)]
    return start, end, keys


def budget_cycle(today: date, payday: int) -> tuple[date, date]:
    """Payday-to-payday cycle containing `today` (end is the day before the next payday)."""
    if today.day >= payday:
        start = today.replace(day=payday)
    else:
        start = add_months(today.replace(day=1), -1).replace(day=payday)
    end = add_months(start.replace(day=1), 1).replace(day=payday) - timedelta(days=1)
    return start, end


# ------------------------------------------------------------------ monthly costs


def _r(x: float) -> float:
    return round(x, 2)


def monthly_costs(txs: list[Tx], config: FinanceConfig, today: date) -> dict[str, Any]:
    """Average monthly cost of essentials and of subscriptions over the lookback window."""
    clf = Classifier(config.classification)
    months = config.analysis.lookback_months
    start, end, keys = lookback_window(today, months)
    window = [t for t in clf.spending(txs) if start <= t.date <= end]

    # ---- subscriptions: recurring, non-installment charges from the same merchant
    groups: dict[str, list[Tx]] = defaultdict(list)
    for t in window:
        if not t.is_installment:
            groups[t.merchant_key].append(t)

    subscriptions = []
    subscription_keys: set[str] = set()
    for key, items in groups.items():
        hints = {clf.subscription_hint(t) for t in items}
        if False in hints:
            continue
        per_month: dict[str, float] = defaultdict(float)
        for t in items:
            per_month[month_key(t.date)] += t.outflow
        values = list(per_month.values())
        recurring = len(per_month) >= 2 and max(values) <= 1.3 * min(values)
        forced = True in hints
        essential = all(clf.is_essential(t) for t in items)
        if not (forced or (recurring and not essential)):
            continue
        last = max(items, key=lambda t: t.date)
        last_month_total = per_month[month_key(last.date)]
        active = (end - last.date).days <= 40
        subscriptions.append(
            {
                "merchant": last.merchant or last.description,
                "monthly_amount": _r(last_month_total),
                "average_amount": _r(sum(values) / len(values)),
                "months_charged": f"{len(per_month)}/{months}",
                "last_charge": last.date.isoformat(),
                "category": last.category,
                "account": last.account_name,
                "detected_by": "rule" if forced else "recurring charge",
                "possibly_cancelled": not active,
            }
        )
        subscription_keys.add(key)

    for fc in config.fixed_costs:
        if fc.subscription:
            subscriptions.append(
                {"merchant": fc.name, "monthly_amount": fc.amount, "detected_by": "config fixed_costs", "possibly_cancelled": False}
            )
    subscriptions.sort(key=lambda s: -s["monthly_amount"])
    subs_total = sum(s["monthly_amount"] for s in subscriptions if not s["possibly_cancelled"])

    # ---- essentials: by category, averaged over the window
    by_category: dict[str, float] = defaultdict(float)
    by_merchant: dict[str, float] = defaultdict(float)
    for t in window:
        if clf.is_essential(t) and t.merchant_key not in subscription_keys:
            by_category[t.category or "Uncategorized"] += t.outflow
            by_merchant[t.merchant or t.description] += t.outflow
    essentials = [
        {"category": c, "monthly_average": _r(v / months)} for c, v in sorted(by_category.items(), key=lambda kv: -kv[1])
    ]
    fixed_essentials = [
        {"name": fc.name, "monthly_amount": fc.amount} for fc in config.fixed_costs if fc.essential and not fc.subscription
    ]
    ess_total = sum(v for v in by_category.values()) / months + sum(f["monthly_amount"] for f in fixed_essentials)

    other_total = sum(
        t.outflow for t in window if not clf.is_essential(t) and t.merchant_key not in subscription_keys
    ) / months

    return {
        "window": {"from": start.isoformat(), "to": end.isoformat(), "months": keys},
        "essentials": {
            "monthly_total": _r(ess_total),
            "by_category": essentials,
            "top_merchants": [
                {"merchant": m, "monthly_average": _r(v / months)}
                for m, v in sorted(by_merchant.items(), key=lambda kv: -kv[1])[:15]
            ],
            "fixed_costs_from_config": fixed_essentials,
        },
        "subscriptions": {"monthly_total": _r(subs_total), "items": subscriptions},
        "everything_else_monthly_average": _r(other_total),
        "_subscription_keys": subscription_keys,
    }


# ------------------------------------------------------------------ installments


def committed_installments(txs: list[Tx], today: date) -> dict[str, float]:
    """Future card installments already committed, by month (YYYY-MM), from the latest installment seen per purchase."""
    latest: dict[tuple[str, int, float], Tx] = {}
    for t in txs:
        if t.account_type != "CREDIT" or not t.is_installment or t.outflow <= 0 or not t.installment_number:
            continue
        key = (t.merchant_key, t.total_installments or 0, round(t.outflow, 2))
        if key not in latest or (t.installment_number or 0) > (latest[key].installment_number or 0):
            latest[key] = t
    out: dict[str, float] = defaultdict(float)
    base = today.replace(day=1)
    for t in latest.values():
        remaining = (t.total_installments or 0) - (t.installment_number or 0)
        tx_month = t.date.replace(day=1)
        for k in range(1, remaining + 1):
            m = add_months(tx_month, k)
            if m >= base:
                out[month_key(m)] += t.outflow
    return {k: _r(v) for k, v in sorted(out.items())}


# ------------------------------------------------------------------ budget


def spending_budget(txs: list[Tx], config: FinanceConfig, today: date) -> dict[str, Any]:
    clf = Classifier(config.classification)
    costs = monthly_costs(txs, config, today)
    sub_keys: set[str] = costs.pop("_subscription_keys")
    months = config.analysis.lookback_months
    w_start, w_end, _ = lookback_window(today, months)

    missing: list[str] = []
    income = config.income.monthly_net
    income_source = "config"
    if income <= 0:
        inflows = [
            t.amount
            for t in txs
            if t.amount > 0 and t.account_type == "BANK" and w_start <= t.date <= w_end and t.category_id.startswith(INCOME_CATEGORY)
        ]
        income = sum(inflows) / months
        income_source = "estimated from 'Income' transactions (set income.monthly_net to be exact)"
        missing.append("income.monthly_net")
    if config.investing.monthly_target <= 0:
        missing.append("investing.monthly_target")

    target = config.investing.monthly_target
    essentials = costs["essentials"]["monthly_total"]
    subs = costs["subscriptions"]["monthly_total"]
    free_budget = income - target - essentials - subs

    c_start, c_end = budget_cycle(today, config.income.payday)
    cycle = [t for t in txs if c_start <= t.date <= today]
    spend = clf.spending(cycle)
    discretionary = [t for t in spend if not clf.is_essential(t) and t.merchant_key not in sub_keys]
    disc_total = sum(t.outflow for t in discretionary)
    ess_spent = sum(t.outflow for t in spend if clf.is_essential(t))
    invested = sum(
        t.outflow for t in cycle if t.account_type == "BANK" and t.category_id.startswith(INVESTMENT_CATEGORY)
    )

    remaining = free_budget - disc_total
    days_left = (c_end - today).days + 1
    threshold = free_budget * config.analysis.tight_threshold_pct / 100
    status = "over_budget" if remaining < 0 else "tight" if remaining < threshold else "ok"

    top = defaultdict(float)
    for t in discretionary:
        top[t.category or "Uncategorized"] += t.outflow

    return {
        "cycle": {"from": c_start.isoformat(), "to": c_end.isoformat(), "days_left": days_left},
        "income": {"monthly": _r(income), "source": income_source},
        "investment_target": _r(target),
        "essentials_monthly_average": essentials,
        "subscriptions_monthly": subs,
        "free_to_spend_per_cycle": _r(free_budget),
        "discretionary_spent_this_cycle": _r(disc_total),
        "remaining_free_to_spend": _r(remaining),
        "per_day_until_payday": _r(remaining / days_left) if days_left > 0 else None,
        "status": status,
        "essentials_spent_this_cycle": _r(ess_spent),
        "invested_this_cycle_detected": _r(invested),
        "invested_note": "Only counts bank outflows Pluggy categorised as 'Investments'; transfers to your broker may show as 'Same person transfer'.",
        "discretionary_by_category": [
            {"category": c, "spent": _r(v)} for c, v in sorted(top.items(), key=lambda kv: -kv[1])
        ],
        "missing_config": missing,
    }


def affordability(
    budget: dict[str, Any],
    commitments: dict[str, float],
    amount: float,
    installments: int,
    today: date,
    available_credit: float | None,
    essential: bool,
    tight_pct: float,
) -> dict[str, Any]:
    per = amount / installments
    free = budget["free_to_spend_per_cycle"]
    remaining_now = budget["remaining_free_to_spend"]

    # An essential purchase is still paid from the same pot, but flag it so Claude can weigh it differently.
    after_now = remaining_now - per
    months = []
    base = today.replace(day=1)
    for k in range(1, installments):
        m = month_key(add_months(base, k))
        already = commitments.get(m, 0.0)
        months.append(
            {
                "month": m,
                "already_committed_installments": already,
                "this_purchase": _r(per),
                "free_budget_left_after_installments": _r(free - already - per),
            }
        )

    reasons = []
    if after_now < 0:
        verdict = "cuts_into_investment"
        reasons.append(f"This cycle would go {_r(-after_now)} over the free budget, i.e. into the investment target.")
    elif after_now < free * tight_pct / 100:
        verdict = "fits_but_tight"
        reasons.append(f"Only {_r(after_now)} would remain until payday.")
    else:
        verdict = "fits"
    if any(m["free_budget_left_after_installments"] < 0 for m in months):
        verdict = "cuts_into_investment" if verdict == "fits" else verdict
        reasons.append("Some future months would exceed the free budget once existing installments are counted.")
    if available_credit is not None and amount > available_credit:
        verdict = "exceeds_card_limit"
        reasons.append(f"Available card limit is {_r(available_credit)}; installment purchases block the full amount.")

    return {
        "amount": _r(amount),
        "installments": installments,
        "per_installment": _r(per),
        "essential": essential,
        "remaining_free_this_cycle_before": _r(remaining_now),
        "remaining_free_this_cycle_after": _r(after_now),
        "future_months": months,
        "available_credit_limit": _r(available_credit) if available_credit is not None else None,
        "verdict": verdict,
        "reasons": reasons,
        "missing_config": budget.get("missing_config", []),
    }
