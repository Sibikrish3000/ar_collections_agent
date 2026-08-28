"""Deterministic escalation state machine.

Pure functions only: no IO, no clock, no randomness, no model calls. Given an
invoice's state, the account's state, the customer's payment history as known on
``as_of``, and the policy, there is exactly one correct decision — which is what
makes the replay log reproducible and auditable.

Evaluation order (each step can end the evaluation):

1. settled?                     -> CLOSED
2. days past due                -> not yet due means nothing to do
3. effective days past due      -> raw dpd minus earned grace
4. holds, by strict precedence  -> escalation suppressed, queued for a human
5. tier selection               -> highest tier whose threshold is met
6. cadence gates                -> interval, per-tier quota, business days
7. recipient resolution         -> roles to addresses, deduped, deliverable
8. guardrails                   -> what may never be auto-sent, regardless of config
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from .config import Policy, Tier
from .ledger import AsOfLedger, CustomerStats
from .models import (
    ActionType,
    Contact,
    ContactRole,
    CustomerState,
    Decision,
    DecisionOutcome,
    HoldReason,
    InvoiceState,
)

# Strict precedence: the most consequential reason to stay quiet wins. A legal
# hold outranks everything; a de-minimis cap is the weakest reason of all.
HOLD_PRECEDENCE: tuple[HoldReason, ...] = (
    HoldReason.LEGAL_HOLD,
    HoldReason.DISPUTE,
    HoldReason.PAYMENT_IN_FLIGHT,
    HoldReason.PROMISE_TO_PAY,
    HoldReason.PAYMENT_PLAN_PENDING,
    HoldReason.REISSUE_REQUIRED,
    HoldReason.INFO_REQUEST,
    HoldReason.PORTAL_SLA,
    HoldReason.RELATIONSHIP_RISK,
    HoldReason.UNREADABLE_REPLY,
    HoldReason.CADENCE_EXHAUSTED,
    HoldReason.DE_MINIMIS,
    HoldReason.OOO_DEFER,
)

_HOLD_RANK = {reason: i for i, reason in enumerate(HOLD_PRECEDENCE)}


# --------------------------------------------------------------------------- #
# Grace: separating "this customer is always 20 days late" from "this is trouble"
# --------------------------------------------------------------------------- #


def grace_offset(stats: CustomerStats, policy: Policy) -> int:
    """Days of tolerance earned by a customer's *predictable* payment rhythm.

    C-02 has paid 100% of 48 invoices late by a median 25 days with a standard
    deviation of 4. That is how they pay, not a collections emergency: chasing
    their controller every month would burn the relationship for nothing. An
    erratic payer (high variance) earns no grace, because with them lateness
    really does carry information.
    """
    cfg = policy.grace
    if not cfg.get("enabled", False) or not stats.has_history:
        return 0
    if stats.invoices_settled < int(cfg.get("min_history_invoices", 3)):
        return 0
    if stats.stdev_days_late > float(cfg.get("max_volatility_days", 12)):
        return 0
    cap = int(cfg.get("chronic_late_offset_days", 0))
    return max(0, min(int(round(stats.median_days_late)), cap))


def days_past_due(due_date: date, as_of: date) -> int:
    return max(0, (as_of - due_date).days)


# --------------------------------------------------------------------------- #
# Recipients
# --------------------------------------------------------------------------- #


def resolve_recipients(
    roles: tuple[str, ...],
    customer_id: str,
    ledger: AsOfLedger,
    customer_state: CustomerState,
) -> tuple[tuple[Contact, ...], tuple[str, ...]]:
    """Roles to contacts, dropping duplicates and dead mailboxes.

    C-11 lists the same person as both CEO and owner, so a Tier 4 escalation must
    not email them twice. C-11's AP address hard-bounced, so it must not be
    emailed at all.
    """
    contacts: list[Contact] = []
    seen: set[str] = set()
    skipped: list[str] = []
    for role in roles:
        contact = ledger.contact(customer_id, ContactRole(role))
        if contact is None:
            skipped.append(f"no {role} on file")
            continue
        if contact.email in customer_state.undeliverable_emails:
            skipped.append(f"{role} {contact.email} undeliverable")
            continue
        if contact.email in seen:
            continue
        seen.add(contact.email)
        contacts.append(contact)
    return tuple(contacts), tuple(skipped)


# --------------------------------------------------------------------------- #
# The state machine
# --------------------------------------------------------------------------- #


def evaluate(
    *,
    state: InvoiceState,
    customer_state: CustomerState,
    stats: CustomerStats,
    as_of: date,
    policy: Policy,
    ledger: AsOfLedger,
) -> Decision:
    """The one decision for this invoice on this day."""
    invoice = state.invoice

    # 1. Settled -------------------------------------------------------------
    if state.balance <= Decimal("0"):
        return Decision(outcome=DecisionOutcome.CLOSED, suppress_reason="settled")

    # 2. Past due? -----------------------------------------------------------
    raw_dpd = days_past_due(invoice.due_date, as_of)
    state.days_past_due = raw_dpd
    if raw_dpd == 0:
        state.effective_days_past_due = 0
        return Decision(outcome=DecisionOutcome.SUPPRESS, suppress_reason="not_yet_due")

    # 3. Earned grace --------------------------------------------------------
    offset = grace_offset(stats, policy)
    effective_dpd = max(0, raw_dpd - offset)
    state.effective_days_past_due = effective_dpd

    # 4. Which tier would fire? ---------------------------------------------
    tier = policy.tier_for(effective_dpd)
    if tier is None:
        return Decision(
            outcome=DecisionOutcome.SUPPRESS,
            suppress_reason=(
                f"within_grace(+{offset}d)" if offset else "below_tier_1_threshold"
            ),
        )

    # De-minimis: a small balance is capped, not escalated.
    de_minimis_applies = False
    capped_from = 0
    threshold = policy.de_minimis_threshold()
    if threshold > 0 and state.balance < threshold:
        cap = int(policy.de_minimis.get("max_tier", 1))
        if tier.tier > cap:
            capped = policy.tier_by_number(cap)
            if capped is None:
                return Decision(
                    outcome=DecisionOutcome.SUPPRESS, suppress_reason="de_minimis"
                )
            capped_from = tier.tier
            tier = capped
            de_minimis_applies = True

    # 5. Holds ---------------------------------------------------------------
    hold = _dominant_hold(state, customer_state, as_of)
    if hold is None and de_minimis_applies:
        hold = HoldReason.DE_MINIMIS

    # 6. Cadence -------------------------------------------------------------
    gate = _cadence_gate(state, tier, as_of, policy, hold=hold)
    if gate is not None:
        return Decision(outcome=DecisionOutcome.SUPPRESS, suppress_reason=gate)

    # 7/8. Build the action -------------------------------------------------
    if hold is not None:
        return _hold_decision(
            state,
            customer_state,
            tier,
            hold,
            policy,
            ledger,
            de_minimis_applies,
            capped_from=capped_from,
        )
    return _send_decision(state, customer_state, tier, policy, ledger, offset, as_of)


def _dominant_hold(
    state: InvoiceState, customer_state: CustomerState, as_of: date
) -> HoldReason | None:
    """Highest-precedence hold active on ``as_of``, account-level first."""
    active: list[HoldReason] = []
    if customer_state.legal_locked:
        active.append(HoldReason.LEGAL_HOLD)
    if (
        customer_state.relationship_risk_until is not None
        and as_of <= customer_state.relationship_risk_until
    ):
        active.append(HoldReason.RELATIONSHIP_RISK)
    active.extend(h.reason for h in state.active_holds(as_of))
    if not active:
        return None
    return min(active, key=lambda r: _HOLD_RANK.get(r, len(_HOLD_RANK)))


def _cadence_gate(
    state: InvoiceState,
    tier: Tier,
    as_of: date,
    policy: Policy,
    *,
    hold: HoldReason | None,
) -> str | None:
    """``None`` to proceed, otherwise the reason nothing happens today."""
    cadence = policy.cadence
    interval = int(cadence.get("repeat_interval_days", 7))
    per_tier = int(cadence.get("max_reminders_per_tier", 2))
    per_invoice = int(cadence.get("max_reminders_per_invoice", 0))

    if cadence.get("business_days_only", False) and as_of.weekday() >= 5:
        return "weekend"

    if state.last_contact_date is not None:
        # A newly-reached tier may speak immediately; a repeat must wait.
        repeat_at_same_tier = tier.tier <= state.highest_tier_reached
        if repeat_at_same_tier and (as_of - state.last_contact_date).days < interval:
            return f"cadence_interval(<{interval}d)"

    if hold is None:
        if per_tier and state.reminders_sent_by_tier.get(tier.tier, 0) >= per_tier:
            return f"tier_{tier.tier}_quota_reached"
        if per_invoice and state.total_reminders >= per_invoice:
            return "invoice_reminder_cap"
        return None

    # A hold produces one internal handover per reason per tier. Repeating it
    # weekly for months would bury the humans it is meant to inform; the stakes
    # only change when the tier does.
    if (str(hold), tier.tier) in state.hold_notices:
        return f"{hold}_already_handed_over_at_tier_{tier.tier}"
    return None


def _hold_decision(
    state: InvoiceState,
    customer_state: CustomerState,
    tier: Tier,
    hold: HoldReason,
    policy: Policy,
    ledger: AsOfLedger,
    de_minimis: bool,
    capped_from: int = 0,
) -> Decision:
    """Escalation is suppressed: draft an internal note, never a customer email."""
    invoice = state.invoice
    route_role = (
        str(policy.de_minimis.get("route_to", "sales_owner"))
        if de_minimis
        else "sales_owner"
    )
    contacts, skipped = resolve_recipients(
        (route_role,), invoice.customer_id, ledger, customer_state
    )
    if not contacts:
        contacts, _ = resolve_recipients(
            ("collections",), invoice.customer_id, ledger, customer_state
        )

    detail = _hold_detail(state, customer_state, hold, policy)
    if capped_from:
        detail = (
            f"escalation capped at tier {tier.tier} (tier {capped_from} was due); "
            f"{detail}"
        )
    return Decision(
        outcome=DecisionOutcome.HOLD,
        tier=tier.tier,
        tier_name=f"internal / {tier.name}",
        to_emails=tuple(c.email for c in contacts),
        to_roles=(route_role,) if contacts else (),
        cc_emails=(),
        template="internal_handover",
        action_type=ActionType.HELD_FOR_APPROVAL,
        hold_reason=str(hold),
        trigger_reason=(
            f"tier_{tier.tier}_due_at_{state.effective_days_past_due}d_effective; {detail}"
            + (f"; {'; '.join(skipped)}" if skipped else "")
        ),
    )


def _hold_detail(
    state: InvoiceState,
    customer_state: CustomerState,
    hold: HoldReason,
    policy: Policy,
) -> str:
    if hold is HoldReason.DE_MINIMIS:
        return str(
            policy.de_minimis.get(
                "reason", "balance below the cost-to-collect threshold"
            )
        )
    if hold is HoldReason.LEGAL_HOLD:
        when = customer_state.legal_locked_on
        return (
            "account legally locked"
            + (f" on {when.isoformat()}" if when else "")
            + "; human release required"
        )
    if hold is HoldReason.RELATIONSHIP_RISK:
        until = customer_state.relationship_risk_until
        return "customer escalated a complaint; chasing frozen" + (
            f" until {until.isoformat()}" if until else ""
        )
    existing = state.holds.get(hold)
    if existing is not None:
        window = (
            f" until {existing.expires_on.isoformat()}"
            if existing.expires_on
            else " (no automatic release)"
        )
        return f"{existing.detail}{window}".strip()
    return str(hold).lower().replace("_", " ")


def _send_decision(
    state: InvoiceState,
    customer_state: CustomerState,
    tier: Tier,
    policy: Policy,
    ledger: AsOfLedger,
    grace_days: int,
    as_of: date,
) -> Decision:
    """No hold: escalate to this tier, auto-sending only where policy allows."""
    invoice = state.invoice
    guardrails = policy.guardrails
    to_contacts, skipped = resolve_recipients(
        tier.to, invoice.customer_id, ledger, customer_state
    )
    notes = list(skipped)

    # Dead primary mailbox: reroute rather than keep emailing a bounce.
    rerouted = False
    if not to_contacts and policy.deliverability.get(
        "hard_bounce_marks_undeliverable", True
    ):
        fallback = str(policy.deliverability.get("reroute_to", "controller"))
        to_contacts, more = resolve_recipients(
            (fallback,), invoice.customer_id, ledger, customer_state
        )
        notes.extend(more)
        rerouted = bool(to_contacts)
        if rerouted:
            notes.append(f"rerouted to {fallback} after hard bounce")

    if not to_contacts:
        return _hold_decision(
            state,
            customer_state,
            tier,
            HoldReason.UNDELIVERABLE,
            policy,
            ledger,
            de_minimis=False,
        )

    cc_contacts, cc_skipped = resolve_recipients(
        tier.cc, invoice.customer_id, ledger, customer_state
    )
    notes.extend(cc_skipped)
    to_emails = tuple(c.email for c in to_contacts)
    cc_emails = tuple(c.email for c in cc_contacts if c.email not in set(to_emails))

    auto = tier.auto_send
    blocked: list[str] = []

    never_roles = {str(r) for r in guardrails.get("never_auto_send_to_roles", ())}
    hit_roles = sorted(set(tier.to) & never_roles)
    if hit_roles:
        auto = False
        blocked.append(f"guardrail:role({','.join(hit_roles)})")

    if rerouted and guardrails.get("never_auto_send_to_unverified_address", True):
        auto = False
        blocked.append("guardrail:rerouted_address")

    weekly_cap = int(guardrails.get("max_auto_sends_per_customer_per_week", 0))
    if auto and weekly_cap:
        if customer_state.auto_sends_in_week.get(_week_key(as_of), 0) >= weekly_cap:
            auto = False
            blocked.append(f"guardrail:weekly_auto_send_cap({weekly_cap})")

    trigger = f"tier_{tier.tier}_at_{state.effective_days_past_due}d_effective"
    if grace_days:
        trigger += f"(raw {state.days_past_due}d, grace {grace_days}d)"
    if state.promise_broken:
        trigger += "; promise_broken"
    if blocked:
        trigger += "; " + "; ".join(blocked)
    if notes:
        trigger += "; " + "; ".join(notes)

    return Decision(
        outcome=DecisionOutcome.SEND,
        tier=tier.tier,
        tier_name=tier.name,
        to_emails=to_emails,
        to_roles=tuple(
            str(c.contact_type) for c in to_contacts
        ),
        cc_emails=cc_emails,
        template=tier.template,
        action_type=ActionType.AUTO_SEND if auto else ActionType.HELD_FOR_APPROVAL,
        hold_reason="" if auto else "AWAITING_HUMAN_SIGNOFF",
        trigger_reason=trigger,
    )


def _week_key(day: date) -> tuple[int, int]:
    """ISO year/week, for the weekly auto-send cap."""
    iso = day.isocalendar()
    return iso.year, iso.week
