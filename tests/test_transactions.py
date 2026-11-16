from zoneinfo import ZoneInfo

from finance_mcp.transactions import normalize_transaction

TZ = ZoneInfo("America/Sao_Paulo")


def test_credit_card_sign_is_flipped_and_date_is_local():
    acc = {"id": "c", "type": "CREDIT", "name": "Card"}
    raw = {
        "id": "1",
        "date": "2026-10-05T02:00:00.000Z",
        "amount": 100,
        "description": "TV PARC 02/05",
        "category": "Electronics",
        "categoryId": "08010000",
        "creditCardMetadata": {"installmentNumber": 2, "totalInstallments": 5, "totalAmount": 500},
    }
    t = normalize_transaction(raw, acc, TZ)
    assert t["amount"] == -100
    assert t["date"] == "2026-10-04"  # converted to Brazil time
    assert (t["installment_number"], t["total_installments"], t["total_amount"]) == (2, 5, 500)
    assert t["category_id"] == "08010000" and t["account_type"] == "CREDIT"


def test_bank_amount_kept_and_payment_data_passed_through():
    acc = {"id": "b", "type": "BANK", "name": "Bank"}
    payment = {"paymentMethod": "PIX", "receiver": {"name": "Fulano"}}
    t = normalize_transaction({"id": "2", "date": "2026-10-05", "amount": -50, "paymentData": payment}, acc, TZ)
    assert t["amount"] == -50
    assert t["payment_data"] == payment
    assert t["merchant"] is None
