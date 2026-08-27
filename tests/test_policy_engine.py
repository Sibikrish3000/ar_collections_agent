"""Tier arithmetic, hold precedence, judgment rules and guardrails."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from fs_collections_agent import policy_engine
from fs_collections_agent.ledger import CustomerStats
from fs_collections_agent.models import (
    ActionType,
    DecisionOutcome,
    Hold,
    HoldReason,
)
from tests.conftest import (
    DEFAULT_DUE,
    make_customer_state,
    make_invoice,
    make_state,
    single_customer_ledger,
)

DUE = DEFAULT_DUE  # a Tuesday: the offsets used below land on weekdays
NO_HISTORY = CustomerStats("C-01", 0, 0.0, 0.0, 0.0, 0.0, 0)
CHRONIC = CustomerStats("C-01", 40, 25.0, 25.0, 4.0, 1.0, 32)  # C-02's shape
ERRATIC = CustomerStats("C-01", 30, 11.0, 1.0, 20.0, 0.53, 51)  # C-03's shape


def evaluate(state, customer_state, policy, as_of, stats=NO_HISTORY, ledger=None):
    return policy_engine.evaluate(
        state=state,
        customer_state=customer_state,
        stats=stats,
        as_of=as_of,
        policy=policy,
        ledger=ledger or single_customer_ledger(),
    )


def on_day(offset: int) -> date:
    """A weekday ``offset`` days past due (weekends are skipped by policy)."""
    day = DUE + timedelta(days=offset)
    assert day.weekday() < 5, f"{day} is a weekend; pick another offset"
    return day


# --------------------------------------------------------------------------- #
# Tier boundaries
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "offset,expected_tier",
    [(1, None), (2, None), (3, 1), (13, 1), (14, 2), (29, 2), (30, 3), (44, 3), (45, 4)],
)
def test_tier_thresholds(policy, offset: int, expected_tier: int | None) -> None:
    tier = policy.tier_for(offset)
    assert (tier.tier if tier else None) == expected_tier


def test_not_yet_due_produces_no_action(policy) -> None:
    decision = evaluate(make_state(), make_customer_state(), policy, DUE)
    assert decision.outcome is DecisionOutcome.SUPPRESS
    assert decision.suppress_reason == "not_yet_due"


def test_settled_invoice_is_closed(policy) -> None:
    decision = evaluate(
        make_state(balance="0.00"), make_customer_state(), policy, on_day(30)
    )
    assert decision.outcome is DecisionOutcome.CLOSED


def test_tier_one_auto_sends_to_ap_contact(policy) -> None:
    decision = evaluate(make_state(), make_customer_state(), policy, on_day(3))
    assert decision.outcome is DecisionOutcome.SEND
    assert decision.tier == 1
    assert decision.action_type is ActionType.AUTO_SEND
    assert decision.to_emails == ("ap@test.example",)


def test_tier_two_ccs_the_controller(policy) -> None:
    decision = evaluate(make_state(), make_customer_state(), policy, on_day(14))
    assert decision.tier == 2
    assert decision.cc_emails == ("controller@test.example",)
    assert decision.action_type is ActionType.AUTO_SEND


# --------------------------------------------------------------------------- #
# Guardrails
# --------------------------------------------------------------------------- #


def test_tier_three_and_four_are_never_auto_sent(policy) -> None:
    for offset in (30, 45):
        decision = evaluate(make_state(), make_customer_state(), policy, on_day(offset))
        assert decision.action_type is ActionType.HELD_FOR_APPROVAL, offset
        assert decision.hold_reason == "AWAITING_HUMAN_SIGNOFF"


def test_executive_tier_deduplicates_a_shared_mailbox(policy) -> None:
    """C-11 lists one person as both CEO and owner: email them once, not twice."""
    decision = evaluate(make_state(), make_customer_state(), policy, on_day(45))
    assert decision.tier == 4
    assert decision.to_emails == ("ceo@test.example",)


def test_weekly_auto_send_cap_forces_human_review(policy) -> None:
    customer_state = make_customer_state()
    day = on_day(3)
    iso = day.isocalendar()
    cap = int(policy.guardrails["max_auto_sends_per_customer_per_week"])
    customer_state.auto_sends_in_week[(iso.year, iso.week)] = cap

    decision = evaluate(make_state(), customer_state, policy, day)
    assert decision.action_type is ActionType.HELD_FOR_APPROVAL
    assert "weekly_auto_send_cap" in decision.trigger_reason


# --------------------------------------------------------------------------- #
# Deliverability
# --------------------------------------------------------------------------- #


def test_dead_mailbox_reroutes_to_controller_and_needs_signoff(policy) -> None:
    customer_state = make_customer_state()
    customer_state.undeliverable_emails.add("ap@test.example")

    decision = evaluate(make_state(), customer_state, policy, on_day(3))
    assert decision.to_emails == ("controller@test.example",)
    assert decision.action_type is ActionType.HELD_FOR_APPROVAL
    assert "rerouted to controller" in decision.trigger_reason


def test_no_deliverable_address_hands_over_internally(policy) -> None:
    customer_state = make_customer_state()
    customer_state.undeliverable_emails.update(
        {"ap@test.example", "controller@test.example"}
    )

    decision = evaluate(make_state(), customer_state, policy, on_day(3))
    assert decision.outcome is DecisionOutcome.HOLD
    assert decision.hold_reason == str(HoldReason.UNDELIVERABLE)
    assert decision.to_emails == ("director@provider.example",)


# --------------------------------------------------------------------------- #
# Judgment rules
# --------------------------------------------------------------------------- #


def test_chronic_late_payer_earns_grace(policy) -> None:
    """A predictable 25-days-late payer sits at Tier 1, not Tier 3, at 25 dpd."""
    state = make_state()
    decision = evaluate(state, make_customer_state(), policy, on_day(24), stats=CHRONIC)
    assert decision.tier == 1
    assert state.days_past_due == 24
    assert state.effective_days_past_due == 9


def test_erratic_payer_earns_no_grace(policy) -> None:
    state = make_state()
    evaluate(state, make_customer_state(), policy, on_day(24), stats=ERRATIC)
    assert state.effective_days_past_due == 24


def test_grace_is_capped_by_config(policy) -> None:
    extreme = CustomerStats("C-01", 20, 70.0, 70.0, 5.0, 1.0, 90)  # C-05's shape
    state = make_state()
    evaluate(state, make_customer_state(), policy, on_day(45), stats=extreme)
    cap = int(policy.grace["chronic_late_offset_days"])
    assert state.effective_days_past_due == 45 - cap


def test_thin_history_earns_no_grace(policy) -> None:
    thin = CustomerStats("C-01", 2, 30.0, 30.0, 1.0, 1.0, 31)
    state = make_state()
    evaluate(state, make_customer_state(), policy, on_day(20), stats=thin)
    assert state.effective_days_past_due == 20


def test_small_balance_never_escalates_past_tier_one(policy) -> None:
    """Ardley's ~$300 invoices: chasing them costs more than they are worth."""
    state = make_state(make_invoice(amount="314.14"))
    decision = evaluate(state, make_customer_state(), policy, on_day(45))
    assert decision.outcome is DecisionOutcome.HOLD
    assert decision.hold_reason == str(HoldReason.DE_MINIMIS)
    assert decision.to_emails == ("director@provider.example",)


def test_small_balance_still_gets_a_tier_one_nudge(policy) -> None:
    state = make_state(make_invoice(amount="314.14"))
    decision = evaluate(state, make_customer_state(), policy, on_day(3))
    assert decision.outcome is DecisionOutcome.SEND
    assert decision.action_type is ActionType.AUTO_SEND


def test_weekends_are_quiet(policy) -> None:
    saturday = DUE + timedelta(days=3)
    while saturday.weekday() != 5:
        saturday += timedelta(days=1)
    decision = evaluate(make_state(), make_customer_state(), policy, saturday)
    assert decision.outcome is DecisionOutcome.SUPPRESS
    assert decision.suppress_reason == "weekend"


def test_repeat_at_same_tier_waits_for_the_interval(policy) -> None:
    state = make_state()
    state.last_contact_date = on_day(3)
    state.highest_tier_reached = 1
    state.reminders_sent_by_tier[1] = 1

    blocked = evaluate(state, make_customer_state(), policy, on_day(6))
    assert blocked.outcome is DecisionOutcome.SUPPRESS
    assert "cadence_interval" in blocked.suppress_reason

    allowed = evaluate(state, make_customer_state(), policy, on_day(10))
    assert allowed.outcome is DecisionOutcome.SEND


def test_per_tier_quota_stops_repeating(policy) -> None:
    state = make_state()
    state.highest_tier_reached = 1
    state.last_contact_date = on_day(3)
    state.reminders_sent_by_tier[1] = int(policy.cadence["max_reminders_per_tier"])

    decision = evaluate(state, make_customer_state(), policy, on_day(13))
    assert decision.outcome is DecisionOutcome.SUPPRESS
    assert decision.suppress_reason == "tier_1_quota_reached"


# --------------------------------------------------------------------------- #
# Holds
# --------------------------------------------------------------------------- #


def test_promise_to_pay_suppresses_then_re_escalates(policy) -> None:
    state = make_state()
    promised = date(2026, 7, 20)
    state.holds[HoldReason.PROMISE_TO_PAY] = Hold(
        reason=HoldReason.PROMISE_TO_PAY,
        set_on=on_day(3),
        expires_on=promised,
        detail="promised 2026-07-20",
    )

    held = evaluate(state, make_customer_state(), policy, on_day(14))
    assert held.outcome is DecisionOutcome.HOLD
    assert held.hold_reason == str(HoldReason.PROMISE_TO_PAY)

    del state.holds[HoldReason.PROMISE_TO_PAY]  # the engine expires it
    state.promise_broken = True
    resumed = evaluate(state, make_customer_state(), policy, on_day(30))
    assert resumed.outcome is DecisionOutcome.SEND
    assert "promise_broken" in resumed.trigger_reason


def test_legal_lock_outranks_every_other_hold(policy) -> None:
    state = make_state()
    state.holds[HoldReason.DISPUTE] = Hold(
        reason=HoldReason.DISPUTE,
        set_on=on_day(3),
        expires_on=DUE + timedelta(days=40),
        detail="d",
    )
    customer_state = make_customer_state()
    customer_state.legal_locked = True
    customer_state.legal_locked_on = on_day(6)

    decision = evaluate(state, customer_state, policy, on_day(14))
    assert decision.hold_reason == str(HoldReason.LEGAL_HOLD)
    assert "human release required" in decision.trigger_reason


def test_legal_lock_never_expires_on_its_own(policy) -> None:
    customer_state = make_customer_state()
    customer_state.legal_locked = True
    customer_state.legal_locked_on = on_day(6)

    far_future = date(2027, 12, 31)
    decision = evaluate(make_state(), customer_state, policy, far_future)
    assert decision.hold_reason == str(HoldReason.LEGAL_HOLD)


def test_hold_precedence_order_is_total(policy) -> None:
    """Every hold reason has a rank, so precedence can never be ambiguous."""
    assert len(set(policy_engine.HOLD_PRECEDENCE)) == len(
        policy_engine.HOLD_PRECEDENCE
    )
    ranked = set(policy_engine.HOLD_PRECEDENCE)
    missing = {r for r in HoldReason if r not in ranked} - {HoldReason.UNDELIVERABLE}
    assert not missing, f"unranked hold reasons: {missing}"


def test_hold_is_handed_over_once_per_tier(policy) -> None:
    state = make_state()
    state.holds[HoldReason.DISPUTE] = Hold(
        reason=HoldReason.DISPUTE,
        set_on=on_day(3),
        expires_on=date(2027, 1, 1),
        detail="dispute",
    )
    first = evaluate(state, make_customer_state(), policy, on_day(14))
    assert first.outcome is DecisionOutcome.HOLD

    state.hold_notices.add((first.hold_reason, first.tier))
    state.last_contact_date = on_day(14)
    state.highest_tier_reached = 2

    again = evaluate(state, make_customer_state(), policy, on_day(24))
    assert again.outcome is DecisionOutcome.SUPPRESS
    assert "already_handed_over" in again.suppress_reason

    escalated = evaluate(state, make_customer_state(), policy, on_day(31))
    assert escalated.outcome is DecisionOutcome.HOLD
    assert escalated.tier == 3


def test_decisions_are_pure_and_repeatable(policy) -> None:
    state = make_state()
    customer_state = make_customer_state()
    first = evaluate(state, customer_state, policy, on_day(14))
    second = evaluate(state, customer_state, policy, on_day(14))
    assert first == second
