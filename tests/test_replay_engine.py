"""End-to-end replay: invariants that must hold across the whole simulation,
plus the specific scenarios in the data that the policy exists to handle."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

from fs_collections_agent import replay_engine
from fs_collections_agent.models import ActionType, ContactRole, HoldReason

LEDGER_DATE = date(2026, 8, 26)


def rows_for(replay, invoice_id: str):
    return [a for a in replay.actions if a.invoice_id == invoice_id]


def events_for(replay, invoice_id: str):
    return [e for e in replay.events if e.invoice_id == invoice_id]


# --------------------------------------------------------------------------- #
# Whole-run invariants
# --------------------------------------------------------------------------- #


def test_replay_covers_the_whole_history(replay) -> None:
    assert replay.start_date == date(2025, 3, 13)
    assert replay.end_date == LEDGER_DATE
    assert replay.days_simulated == (LEDGER_DATE - date(2025, 3, 13)).days + 1
    assert replay.actions, "the agent took no action at all"


def test_no_action_is_dated_outside_the_window(replay) -> None:
    assert all(
        replay.start_date <= a.date <= replay.end_date for a in replay.actions
    )


def test_customer_executives_are_never_auto_sent_to(replay, ledger) -> None:
    executive_emails = {
        contact.email
        for contact in ledger.contacts
        if contact.contact_type in (ContactRole.CEO, ContactRole.OWNER)
    }
    offenders = [
        a
        for a in replay.actions
        if a.action_type == ActionType.AUTO_SEND
        and set(a.recipient_email.split(";")) & executive_emails
    ]
    assert offenders == []


def test_every_tier_three_or_four_action_is_held(replay) -> None:
    for action in replay.actions:
        if action.recipient_tier.startswith(("T3", "T4")):
            assert action.action_type == ActionType.HELD_FOR_APPROVAL, action.invoice_id


def test_no_auto_send_carries_a_hold_reason(replay) -> None:
    for action in replay.actions:
        if action.action_type == ActionType.AUTO_SEND:
            assert action.hold_reason == ""


def test_one_customer_facing_email_per_customer_per_day(replay) -> None:
    counts = Counter(
        (a.date, a.customer_name)
        for a in replay.actions
        if not a.recipient_tier.startswith(("T1 internal", "T2 internal", "internal"))
        and "internal" not in a.recipient_tier
    )
    assert counts and max(counts.values()) == 1


def test_nothing_is_sent_at_the_weekend(replay) -> None:
    assert all(a.date.weekday() < 5 for a in replay.actions)


def test_no_action_after_an_invoice_is_settled(replay, ledger) -> None:
    view = ledger.view(LEDGER_DATE)
    for action in replay.actions:
        settled = view.settlement_date(action.invoice_id)
        if settled is not None:
            assert action.date < settled, f"{action.invoice_id} chased after payment"


def test_balances_logged_are_always_positive(replay) -> None:
    assert all(a.balance > Decimal("0") for a in replay.actions)


def test_every_action_explains_itself(replay) -> None:
    for action in replay.actions:
        assert action.trigger_reason
        assert action.message_body
        assert action.subject
        assert action.recipient_email


def test_message_bodies_carry_the_real_figures(replay) -> None:
    for action in replay.actions[:200]:
        if action.template == "internal_handover":
            continue
        assert action.invoice_id in action.message_body
        assert f"{action.balance:,.2f}" in action.message_body


def test_replay_is_deterministic(ledger, policy, classifications) -> None:
    def digest() -> str:
        result = replay_engine.simulate(
            ledger, policy, classifications, end=LEDGER_DATE
        )
        payload = "\n".join(
            "|".join(a.to_row().values()) for a in result.actions
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    assert digest() == digest()


def test_outputs_are_written_with_the_required_columns(replay, tmp_path) -> None:
    paths = replay_engine.write_outputs(replay, tmp_path)
    header = paths["replay_log"].read_text().splitlines()[0]
    required = (
        "date,invoice_id,customer_name,recipient_email,recipient_tier,"
        "action_type,hold_reason,message_body"
    )
    assert header.startswith(required)
    assert paths["state_events"].is_file()
    assert paths["unmatched_replies"].is_file()


# --------------------------------------------------------------------------- #
# Scenarios from the data
# --------------------------------------------------------------------------- #


def test_small_ardley_invoice_never_reaches_an_executive(replay) -> None:
    """INV-2177 is $314 and 44 days past due; reply 07 is a furious customer."""
    rows = rows_for(replay, "INV-2177")
    assert rows, "expected the small invoice to be chased at least once"
    assert all(not r.recipient_tier.startswith(("T3", "T4")) for r in rows)
    assert any(r.hold_reason == str(HoldReason.DE_MINIMIS) for r in rows)
    assert any(r.hold_reason == str(HoldReason.RELATIONSHIP_RISK) for r in rows)


def test_relationship_complaint_stops_customer_contact(replay) -> None:
    """After 2026-08-22, nothing further goes to the Ardley contact."""
    complaint = date(2026, 8, 22)
    after = [
        a
        for a in replay.actions
        if a.customer_name == "Ardley & Sons"
        and a.date >= complaint
        and "ardley.com" in a.recipient_email
    ]
    assert after == []


def test_legal_language_freezes_the_whole_account(replay) -> None:
    """Reply 11 arrives 2026-08-21 for C-03; every Perrin invoice goes quiet."""
    lock_date = date(2026, 8, 21)
    perrin_after = [
        a
        for a in replay.actions
        if a.customer_name == "Perrin Life Sciences" and a.date >= lock_date
    ]
    assert perrin_after, "expected internal handover notices after the lock"
    for action in perrin_after:
        assert action.action_type == ActionType.HELD_FOR_APPROVAL
        assert action.hold_reason == str(HoldReason.LEGAL_HOLD)
        assert "perrin.com" not in action.recipient_email


def test_promise_to_pay_pauses_chasing(replay) -> None:
    """INV-2121: promised 2026-08-29, so no customer contact in between."""
    holds = [
        e
        for e in events_for(replay, "INV-2121")
        if e.event_type == "HOLD_SET" and "PROMISE_TO_PAY" in e.detail
    ]
    assert len(holds) == 1
    after = [
        a
        for a in rows_for(replay, "INV-2121")
        if a.date >= date(2026, 8, 12) and "perrin.com" in a.recipient_email
    ]
    assert after == []


def test_settled_invoice_with_a_stale_status_is_left_alone(replay) -> None:
    """INV-2231 is exported as open but was paid; it must never be chased."""
    assert rows_for(replay, "INV-2231") == []
    quality = [e for e in replay.events if e.event_type == "DATA_QUALITY"]
    assert any(e.invoice_id == "INV-2231" for e in quality)


def test_reply_already_answered_by_the_ledger_sets_no_hold(replay) -> None:
    reconciled = [
        e for e in replay.events if e.event_type == "REPLY_ALREADY_RECONCILED"
    ]
    assert any(e.invoice_id == "INV-2231" for e in reconciled)


def test_hard_bounce_marks_the_address_undeliverable(replay) -> None:
    bounces = [e for e in replay.events if e.event_type == "ADDRESS_UNDELIVERABLE"]
    assert len(bounces) == 1
    assert "sam.ito@ingleby.com" in bounces[0].detail


def test_inbound_contact_change_is_not_applied_automatically(replay) -> None:
    proposals = [
        e for e in replay.events if e.event_type == "CONTACT_CHANGE_PROPOSED"
    ]
    assert proposals
    assert "NOT applied automatically" in proposals[0].detail
    assert all(
        "ap-team@vantage.com" not in a.recipient_email for a in replay.actions
    )


def test_hold_raised_before_the_invoice_existed_is_applied_later(replay) -> None:
    """Reply 20 (2026-08-17) names INV-2161, issued 2026-08-23."""
    deferred = [e for e in replay.events if e.event_type == "HOLD_DEFERRED"]
    assert any(e.invoice_id == "INV-2161" for e in deferred)
    applied = [
        e
        for e in events_for(replay, "INV-2161")
        if e.event_type == "HOLD_SET" and "REISSUE_REQUIRED" in e.detail
    ]
    assert applied and applied[0].date == date(2026, 8, 23)


def test_partial_payment_pauses_the_ladder(replay) -> None:
    partials = [e for e in replay.events if e.event_type == "PARTIAL_PAYMENT"]
    assert partials
    event = partials[0]
    follow_ups = [
        a
        for a in rows_for(replay, event.invoice_id)
        if event.date <= a.date <= event.date + timedelta(days=4)
    ]
    assert all(a.action_type == ActionType.HELD_FOR_APPROVAL for a in follow_ups)


def test_a_chronic_late_payer_is_not_escalated_to_its_controller(replay) -> None:
    """Cormack pays every invoice ~25 days late; grace keeps them off Tier 3."""
    cormack = [a for a in replay.actions if a.customer_name == "Cormack Retail Group"]
    assert cormack
    assert all(not a.recipient_tier.startswith(("T3", "T4")) for a in cormack)
    assert any(a.effective_days_past_due < a.days_past_due for a in cormack)


def test_unmatched_reply_references_are_recorded(replay) -> None:
    refs = {row["invoice_refs"] for row in replay.unmatched}
    assert {"INV-9911", "INV-9999"} <= refs


def test_business_day_helper() -> None:
    friday = date(2026, 8, 21)
    assert replay_engine.add_business_days(friday, 1) == date(2026, 8, 24)
    assert replay_engine.add_business_days(friday, 5) == date(2026, 8, 28)
