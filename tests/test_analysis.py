from datetime import date
from zoneinfo import ZoneInfo

import pytest

from finance_mcp import analysis
from finance_mcp.analysis import Tx
from finance_mcp.config_store import ConfigStore, FinanceConfig

TODAY = date(2026, 10, 8)


def tx(d, amount, desc, cat="", cat_id="", account_type="CREDIT", inst=None, total=None, merchant=""):
    return Tx(
        id=f"{d}-{desc}-{amount}",
        date=d,
        description=desc,
        amount=amount,
        category=cat,
        category_id=cat_id,
        merchant=merchant,
        account_id="card1" if account_type == "CREDIT" else "bank1",
        account_name="Card" if account_type == "CREDIT" else "Bank",
        account_type=account_type,
        installment_number=inst,
        total_installments=total,
    )


def config(**income):
    c = FinanceConfig()
    c.income.monthly_net = income.get("net", 10000)
    c.income.payday = income.get("payday", 5)
    c.investing.monthly_target = income.get("target", 2000)
    return c


def history():
    txs = []
    for m in (7, 8, 9):
        txs += [
            tx(date(2026, m, 10), -55.9, "NETFLIX.COM", "Video streaming", "10010000"),
            tx(date(2026, m, 3), -2500, "ALUGUEL", "Rent", "18010000", account_type="BANK"),
            tx(date(2026, m, 15), -800, "SUPERMERCADO X", "Groceries", "11000000"),
            tx(date(2026, m, 20), -99.9, "SMART FIT", "Gyms", "24030000"),
            tx(date(2026, m, 6), -3000, "PAGTO FATURA", "Credit card payment", "05100000", account_type="BANK"),
            tx(date(2026, m, 7), -2000, "APLICACAO CDB", "Investments", "03000000", account_type="BANK"),
        ]
    txs.append(tx(date(2026, 8, 22), -300, "RESTAURANTE", "Restaurants", "12000000"))
    return txs


def test_normalize_flips_credit_card_sign():
    acc = {"id": "c", "type": "CREDIT", "name": "Card"}
    raw = {"id": "1", "date": "2026-10-05T02:00:00.000Z", "amount": 100, "description": "X"}
    t = analysis.normalize_transaction(raw, acc, ZoneInfo("America/Sao_Paulo"))
    assert t.amount == -100 and t.outflow == 100
    assert t.date == date(2026, 10, 4)  # converted to Brazil time


def test_budget_cycle():
    assert analysis.budget_cycle(date(2026, 10, 8), 5) == (date(2026, 10, 5), date(2026, 11, 4))
    assert analysis.budget_cycle(date(2026, 10, 3), 5) == (date(2026, 9, 5), date(2026, 10, 4))
    assert analysis.budget_cycle(date(2026, 1, 2), 5) == (date(2025, 12, 5), date(2026, 1, 4))


def test_monthly_costs_essentials_and_subscriptions():
    result = analysis.monthly_costs(history(), config(), TODAY)
    subs = {s["merchant"]: s for s in result["subscriptions"]["items"]}
    assert set(subs) == {"NETFLIX.COM", "SMART FIT"}  # by category rule and by recurrence
    assert subs["SMART FIT"]["detected_by"] == "recurring charge"
    assert result["subscriptions"]["monthly_total"] == pytest.approx(155.8)
    # rent + groceries; card payment and investments are excluded
    assert result["essentials"]["monthly_total"] == pytest.approx(3300)
    assert result["everything_else_monthly_average"] == pytest.approx(100)


def test_merchant_overrides():
    c = config()
    c.classification.not_subscription_merchants = ["smart fit"]
    c.classification.essential_merchants = ["smart fit"]
    result = analysis.monthly_costs(history(), c, TODAY)
    assert [s["merchant"] for s in result["subscriptions"]["items"]] == ["NETFLIX.COM"]
    assert result["essentials"]["monthly_total"] == pytest.approx(3399.9)


def test_spending_budget_and_affordability():
    txs = history() + [
        tx(date(2026, 10, 6), -400, "LOJA", "Shopping", "09000000"),
        tx(date(2026, 10, 7), -150, "SUPERMERCADO X", "Groceries", "11000000"),
    ]
    budget = analysis.spending_budget(txs, config(), TODAY)
    free = 10000 - 2000 - 3300 - 155.8
    assert budget["free_to_spend_per_cycle"] == pytest.approx(free)
    assert budget["discretionary_spent_this_cycle"] == pytest.approx(400)
    assert budget["remaining_free_to_spend"] == pytest.approx(free - 400)
    assert budget["status"] == "ok"
    assert budget["missing_config"] == []

    ok = analysis.affordability(budget, {}, 1000, 1, TODAY, 5000, False, 10)
    assert ok["verdict"] == "fits"
    over = analysis.affordability(budget, {}, 6000, 1, TODAY, 10000, False, 10)
    assert over["verdict"] == "cuts_into_investment"
    limit = analysis.affordability(budget, {}, 1000, 1, TODAY, 500, False, 10)
    assert limit["verdict"] == "exceeds_card_limit"


def test_committed_installments_uses_latest_installment():
    txs = [
        tx(date(2026, 8, 10), -100, "TV PARC 02/05", inst=2, total=5),
        tx(date(2026, 9, 10), -100, "TV PARC 03/05", inst=3, total=5),
    ]
    assert analysis.committed_installments(txs, TODAY) == {"2026-10": 100, "2026-11": 100}


def test_config_store_validates_and_backs_up(tmp_path):
    store = ConfigStore(tmp_path / "config.yaml", tmp_path / "backups")
    _, diff = store.update({"income": {"monthly_net": 9000}})
    assert diff == ["income.monthly_net: 0.0 -> 9000.0"]
    _, diff = store.update({"investing": {"monthly_target": 1500}})
    assert len(list((tmp_path / "backups").glob("*.yaml"))) == 1
    assert store.load().income.monthly_net == 9000
    with pytest.raises(Exception):
        store.update({"income": {"payday": 40}})
    with pytest.raises(Exception):
        store.update({"incmoe": {}})  # typo is rejected, not ignored
    assert store.load().investing.monthly_target == 1500
