"""The no-future-leakage guarantee, and reconciliation against a stale export."""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

import pytest

from ar_collections_agent.ledger import AsOfLedger

START = date(2025, 3, 1)
END = date(2026, 8, 26)


def _sample_dates(n: int = 40) -> list[date]:
    rng = random.Random(20260826)  # fixed seed: the sample is reproducible
    span = (END - START).days
    return sorted({START + timedelta(days=rng.randint(0, span)) for _ in range(n)})


@pytest.mark.parametrize("as_of", _sample_dates())
def test_view_never_exposes_future_records(ledger: AsOfLedger, as_of: date) -> None:
    view = ledger.view(as_of)

    assert all(i.issue_date <= as_of for i in view.invoices)
    assert all(e.received_date <= as_of for e in view.emails)
    for invoice in view.invoices:
        assert all(p.payment_date <= as_of for p in view.payments_for(invoice.invoice_id))


def test_view_excludes_invoices_issued_tomorrow(ledger: AsOfLedger) -> None:
    day = date(2026, 8, 14)  # INV-2432's issue date
    assert any(i.invoice_id == "INV-2432" for i in ledger.view(day).invoices)
    assert not any(
        i.invoice_id == "INV-2432"
        for i in ledger.view(day - timedelta(days=1)).invoices
    )


def test_emails_on_rejects_a_future_date(ledger: AsOfLedger) -> None:
    view = ledger.view(date(2026, 8, 15))
    with pytest.raises(ValueError):
        view.emails_on(date(2026, 8, 16))


def test_customer_stats_change_as_history_accumulates(ledger: AsOfLedger) -> None:
    early = ledger.view(date(2025, 6, 1)).customer_stats("C-02")
    late = ledger.view(END).customer_stats("C-02")
    assert early.invoices_settled < late.invoices_settled
    # Cormack pays late consistently, so the shape of the signal is stable.
    assert late.pct_paid_late == 1.0
    assert 20 <= late.median_days_late <= 30


def test_customer_stats_ignore_settlements_after_as_of(ledger: AsOfLedger) -> None:
    day = date(2025, 9, 1)
    stats = ledger.view(day).customer_stats("C-05")
    view = ledger.view(day)
    for invoice in view.invoices:
        if invoice.customer_id != "C-05":
            continue
        settled = view.settlement_date(invoice.invoice_id)
        assert settled is None or settled <= day
    assert stats.invoices_settled <= 18


def test_balance_is_recomputed_from_payments_not_status(ledger: AsOfLedger) -> None:
    """INV-2231 is exported as `open` but was paid in full on 2026-08-11."""
    invoice = ledger.invoice("INV-2231")
    assert invoice.status == "open"

    before = ledger.view(date(2026, 8, 10))
    after = ledger.view(date(2026, 8, 11))
    assert before.balance(invoice) == Decimal("34354.30")
    assert after.balance(invoice) == Decimal("0.00")
    assert invoice not in after.open_invoices()


def test_status_discrepancy_is_reported(ledger: AsOfLedger) -> None:
    discrepancies = dict(
        (invoice_id, (exported, computed))
        for invoice_id, exported, computed in ledger.status_discrepancies(END)
    )
    assert discrepancies["INV-2231"] == ("open", "paid")


def test_partial_payments_leave_a_mid_life_balance(ledger: AsOfLedger) -> None:
    """Several invoices are paid in two instalments; the balance must track that."""
    multi = [
        invoice
        for invoice in ledger.all_invoices
        if len(ledger.view(END).payments_for(invoice.invoice_id)) > 1
    ]
    assert multi, "expected invoices with more than one payment row"

    invoice = multi[0]
    payments = sorted(
        ledger.view(END).payments_for(invoice.invoice_id), key=lambda p: p.payment_date
    )
    mid = ledger.view(payments[0].payment_date).balance(invoice)
    assert Decimal("0") < mid < invoice.amount
    assert ledger.view(payments[-1].payment_date).balance(invoice) == Decimal("0.00")
