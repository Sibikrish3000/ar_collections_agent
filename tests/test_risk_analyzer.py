"""Open-invoice risk: bands, explanations and the arithmetic behind them."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ar_collections_agent.risk_analyzer import RiskAnalyzer, render_markdown

LEDGER_DATE = date(2026, 8, 26)


def assess(ledger, policy, classifications):
    return RiskAnalyzer(ledger, policy, classifications).assess(LEDGER_DATE)


def test_covers_every_open_invoice(ledger, policy, classifications) -> None:
    assessments = assess(ledger, policy, classifications)
    open_ids = {i.invoice_id for i in ledger.view(LEDGER_DATE).open_invoices()}
    assert {a.invoice_id for a in assessments} == open_ids
    assert len(assessments) == 44  # 45 exported as open, one already paid


def test_settled_invoice_is_excluded(ledger, policy, classifications) -> None:
    assessments = assess(ledger, policy, classifications)
    assert "INV-2231" not in {a.invoice_id for a in assessments}


def test_bands_are_ordered_by_score(ledger, policy, classifications) -> None:
    assessments = assess(ledger, policy, classifications)
    order = {"High": 0, "Medium": 1, "Low": 2}
    keys = [(order[a.risk_band], -a.risk_score) for a in assessments]
    assert keys == sorted(keys)


def test_reliable_payer_is_low_risk(ledger, policy, classifications) -> None:
    """Halvorsen has never paid late in 32 settled invoices."""
    assessments = {a.invoice_id: a for a in assess(ledger, policy, classifications)}
    for invoice_id in ("INV-2033", "INV-2034", "INV-2035", "INV-2036"):
        assert assessments[invoice_id].risk_band == "Low"


def test_worst_payer_is_high_risk(ledger, policy, classifications) -> None:
    """Ardley pays 100% of invoices late, median ~69 days."""
    assessments = {a.invoice_id: a for a in assess(ledger, policy, classifications)}
    assert assessments["INV-2177"].risk_band == "High"
    assert assessments["INV-2177"].predicted_days_late >= 45


def test_disputed_invoice_is_flagged_with_its_reason(
    ledger, policy, classifications
) -> None:
    assessments = {a.invoice_id: a for a in assess(ledger, policy, classifications)}
    thackeray = assessments["INV-2356"]
    assert thackeray.risk_band == "High"
    assert "open dispute" in thackeray.reason


def test_legal_lock_appears_in_the_reason(ledger, policy, classifications) -> None:
    assessments = {a.invoice_id: a for a in assess(ledger, policy, classifications)}
    assert "legal counsel" in assessments["INV-2122"].reason


def test_de_minimis_invoices_say_how_they_are_worked(
    ledger, policy, classifications
) -> None:
    assessments = {a.invoice_id: a for a in assess(ledger, policy, classifications)}
    assert "cost-to-collect floor" in assessments["INV-2178"].reason


def test_every_reason_names_the_customer_and_a_date(
    ledger, policy, classifications
) -> None:
    for assessment in assess(ledger, policy, classifications):
        assert assessment.customer_name in assessment.reason
        assert "expected payment around" in assessment.reason
        assert assessment.reason.endswith(".")


def test_predictions_are_never_in_the_past(ledger, policy, classifications) -> None:
    for assessment in assess(ledger, policy, classifications):
        if assessment.days_past_due > 0:
            assert assessment.predicted_payment_date > LEDGER_DATE
        else:
            assert assessment.predicted_payment_date >= assessment.due_date


def test_scores_are_bounded(ledger, policy, classifications) -> None:
    for assessment in assess(ledger, policy, classifications):
        assert 0.0 <= assessment.risk_score <= 1.0


def test_exposure_totals_match_the_ledger(ledger, policy, classifications) -> None:
    assessments = assess(ledger, policy, classifications)
    view = ledger.view(LEDGER_DATE)
    expected = sum((view.balance(i) for i in view.open_invoices()), Decimal("0"))
    assert sum((a.balance for a in assessments), Decimal("0")) == expected


def test_markdown_report_lists_every_band_present(
    ledger, policy, classifications
) -> None:
    assessments = assess(ledger, policy, classifications)
    markdown = render_markdown(assessments, LEDGER_DATE)
    for band in {a.risk_band for a in assessments}:
        assert f"## {band} risk" in markdown

    # Every invoice appears once in its band section, plus the top ten again in
    # the exposure-weighted watchlist.
    watchlist, _, banded = markdown.partition("## High risk")
    assert watchlist.count("| INV-") == 10
    assert banded.count("| INV-") == len(assessments)
