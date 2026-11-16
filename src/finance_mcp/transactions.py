"""Turns Pluggy transactions into one flat shape (no network, no interpretation).

Kept free of I/O so it can be unit-tested with fake transactions (see tests/).

The only changes made to Pluggy's data:
- `amount` uses one sign for every account: < 0 means money left you. Pluggy reports
  bank-account debits as negative but credit-card purchases as positive, so card
  amounts are flipped.
- `date` is converted to the local timezone (Pluggy sends UTC).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo


def _parse_date(value: str | None, tz: ZoneInfo) -> date | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz)
    return dt.date()


def normalize_transaction(raw: dict[str, Any], account: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
    account_type = account.get("type", "BANK")
    amount = float(raw.get("amount") or 0)
    if account_type == "CREDIT":
        amount = -amount
    merchant = raw.get("merchant") or {}
    card = raw.get("creditCardMetadata") or {}
    d = _parse_date(raw.get("date"), tz)
    return {
        "id": raw.get("id"),
        "date": d.isoformat() if d else None,
        "description": raw.get("description"),
        "description_raw": raw.get("descriptionRaw"),
        "amount": round(amount, 2),
        "currency": raw.get("currencyCode"),
        "type": raw.get("type"),  # DEBIT | CREDIT, as Pluggy reports it
        "status": raw.get("status"),
        "category": raw.get("category"),
        "category_id": raw.get("categoryId"),
        "merchant": merchant.get("name") or merchant.get("businessName"),
        "merchant_cnpj": merchant.get("cnpj"),
        "merchant_category": merchant.get("category"),
        "payment_data": raw.get("paymentData"),  # Pix/TED details: method, payer, receiver
        "installment_number": card.get("installmentNumber"),
        "total_installments": card.get("totalInstallments"),
        "total_amount": card.get("totalAmount"),
        "account_id": account.get("id"),
        "account": account.get("name") or account.get("marketingName"),
        "account_type": account_type,
    }
