"""Strict day-by-day replay of the collections agent over the full history.

The engine walks one calendar day at a time. On day ``T`` it holds a
:class:`~fs_collections_agent.ledger.LedgerView` for ``T`` and nothing else, so
no decision can be informed by a payment, invoice or email that had not happened
yet. All carried state (holds, reminder counts, dead mailboxes, legal locks)
moves forward only — it is never recomputed from later facts.

Nothing is sent. Every action the agent *would* have taken is written to
``output/dry_run_replay_log.csv``; every non-message decision (hold set, hold
expired, bounce, promise broken, stale ledger status) is written to
``output/state_events.csv`` so a reviewer can explain any row in the main log.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from . import drafting, llm, policy_engine
from .config import Policy
from .email_classifier import CACHE_FILENAME
from .ledger import AsOfLedger, LedgerView
from .models import (
    ActionLog,
    ActionType,
    Classification,
    ContactRole,
    CustomerState,
    Decision,
    DecisionOutcome,
    Hold,
    HoldReason,
    Intent,
    InvoiceState,
    StateEvent,
)

REPLAY_LOG_FILENAME = "dry_run_replay_log.csv"
STATE_EVENTS_FILENAME = "state_events.csv"
UNMATCHED_FILENAME = "unmatched_replies.csv"
SUMMARY_FILENAME = "replay_summary.md"


def add_business_days(start: date, days: int) -> date:
    """``start`` plus ``days`` weekdays (Mon-Fri)."""
    current = start
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


@dataclass(slots=True)
class ReplayResult:
    actions: list[ActionLog] = field(default_factory=list)
    events: list[StateEvent] = field(default_factory=list)
    unmatched: list[dict[str, str]] = field(default_factory=list)
    days_simulated: int = 0
    start_date: date | None = None
    end_date: date | None = None

    @property
    def auto_sends(self) -> int:
        return sum(1 for a in self.actions if a.action_type == ActionType.AUTO_SEND)

    @property
    def held(self) -> int:
        return sum(
            1 for a in self.actions if a.action_type == ActionType.HELD_FOR_APPROVAL
        )

    def by_tier(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for action in self.actions:
            counts[action.recipient_tier] = counts.get(action.recipient_tier, 0) + 1
        return dict(sorted(counts.items()))

    def by_hold_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for action in self.actions:
            if action.hold_reason:
                counts[action.hold_reason] = counts.get(action.hold_reason, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


class ReplayEngine:
    """Deterministic simulation over ``[start_date, end_date]``."""

    def __init__(
        self,
        ledger: AsOfLedger,
        policy: Policy,
        classifications: Sequence[Classification],
        *,
        polish_with_llm: bool = False,
        llm_config: llm.LLMConfig | None = None,
    ) -> None:
        self.ledger = ledger
        self.policy = policy
        self.polish_with_llm = polish_with_llm
        self.llm_config = llm_config

        self.classifications_by_date: dict[date, list[Classification]] = {}
        for classification in sorted(
            classifications, key=lambda c: (c.received_date, c.source_file)
        ):
            self.classifications_by_date.setdefault(
                classification.received_date, []
            ).append(classification)

        self.invoice_states: dict[str, InvoiceState] = {}
        # Holds from replies that arrived before the invoice they name existed in
        # our ledger; applied on the day the invoice appears (see _sync_balances).
        self.pending_holds: dict[str, list[tuple[HoldReason, int, str, date]]] = {}
        self.customer_states: dict[str, CustomerState] = {
            cid: CustomerState(customer=customer)
            for cid, customer in ledger.customers.items()
        }
        self.result = ReplayResult()

    # -- main loop ---------------------------------------------------------- #

    def run(self, start: date, end: date) -> ReplayResult:
        self.result = ReplayResult(start_date=start, end_date=end)
        day = start
        try:
            from tqdm import tqdm  # noqa: PLC0415

            pbar = tqdm(
                total=(end - start).days + 1,
                desc="replaying",
                unit="day",
                disable=False,
                colour="cyan",
                leave=False,
            )
        except ImportError:
            pbar = None
        try:
            while day <= end:
                self._step(day)
                self.result.days_simulated += 1
                if pbar:
                    pbar.update(1)
                day += timedelta(days=1)
        finally:
            if pbar:
                pbar.close()
        self._report_data_quality(end)
        return self.result

    def _step(self, day: date) -> None:
        view = self.ledger.view(day)

        self._sync_balances(day, view)
        self._ingest_replies(day)
        self._expire_holds(day)
        self._act(day, view)

    # -- 1. balances -------------------------------------------------------- #

    def _sync_balances(self, day: date, view: LedgerView) -> None:
        """Recompute every issued invoice's balance from payments known by ``day``."""
        for invoice in view.invoices:
            balance = view.balance(invoice)
            state = self.invoice_states.get(invoice.invoice_id)
            if state is None:
                state = InvoiceState(invoice=invoice, balance=balance)
                self.invoice_states[invoice.invoice_id] = state
                self._apply_pending_holds(day, state)
                continue

            if balance == state.balance:
                continue

            paid_today = sum(
                (
                    p.amount
                    for p in view.payments_for(invoice.invoice_id)
                    if p.payment_date == day
                ),
                start=Decimal("0"),
            )
            previous = state.balance
            state.balance = balance

            if balance <= Decimal("0"):
                state.closed_on = day
                cleared = [
                    reason
                    for reason in list(state.holds)
                    if reason is not HoldReason.LEGAL_HOLD
                ]
                for reason in cleared:
                    del state.holds[reason]
                self._event(
                    day,
                    invoice.customer_id,
                    invoice.invoice_id,
                    "INVOICE_SETTLED",
                    f"payment {paid_today:.2f} received; balance cleared"
                    + (f"; holds released: {','.join(str(c) for c in cleared)}" if cleared else ""),
                )
            elif paid_today > Decimal("0"):
                # Good faith, but not settlement: pause chasing briefly and let a
                # human decide whether the remainder needs a different approach.
                window = self.policy.hold_days("payment_in_flight_hold_days", 5)
                state.holds[HoldReason.PAYMENT_IN_FLIGHT] = Hold(
                    reason=HoldReason.PAYMENT_IN_FLIGHT,
                    set_on=day,
                    expires_on=day + timedelta(days=window),
                    detail=(
                        f"partial payment {paid_today:.2f} received, "
                        f"{balance:.2f} of {previous:.2f} still outstanding"
                    ),
                )
                self._event(
                    day,
                    invoice.customer_id,
                    invoice.invoice_id,
                    "PARTIAL_PAYMENT",
                    f"received {paid_today:.2f}; {balance:.2f} outstanding; "
                    f"PAYMENT_IN_FLIGHT hold for {window}d",
                )

    def _apply_pending_holds(self, day: date, state: InvoiceState) -> None:
        """Attach holds that a customer raised before we had issued the invoice.

        Two replies in the pack (``INV-2161``, ``INV-2162``) arrive days before the
        invoice's own issue date. Dropping them would lose a real instruction, so
        the hold is parked and applied when the invoice appears, with the window
        starting from the issue date rather than retroactively expired.
        """
        for reason, window, detail, origin in self.pending_holds.pop(
            state.invoice_id, []
        ):
            state.holds[reason] = Hold(
                reason=reason,
                set_on=day,
                expires_on=day + timedelta(days=window),
                detail=f"{detail} (raised {origin.isoformat()}, before the invoice "
                "was issued)",
            )
            self._event(
                day,
                state.invoice.customer_id,
                state.invoice_id,
                "HOLD_SET",
                f"{reason} until {(day + timedelta(days=window)).isoformat()} - "
                f"deferred from the reply received {origin.isoformat()}, which "
                "predated the invoice",
            )

    # -- 2. inbound replies ------------------------------------------------- #

    def _ingest_replies(self, day: date) -> None:
        for classification in self.classifications_by_date.get(day, []):
            self._apply_classification(day, classification)

    def _apply_classification(self, day: date, c: Classification) -> None:
        customer_id = c.customer_id
        if customer_id is None or customer_id not in self.customer_states:
            self.result.unmatched.append(
                {
                    "date": day.isoformat(),
                    "source_file": c.source_file,
                    "sender_email": c.sender_email,
                    "intent": str(c.intent),
                    "invoice_refs": ";".join(c.invoice_refs),
                    "issue": "sender could not be matched to a customer",
                }
            )
            self._event(
                day, "", "", "REPLY_UNMATCHED", f"{c.source_file}: {c.sender_email}"
            )
            return

        customer_state = self.customer_states[customer_id]
        customer_state.replies_received += 1

        for ref in c.unmatched_refs:
            self.result.unmatched.append(
                {
                    "date": day.isoformat(),
                    "source_file": c.source_file,
                    "sender_email": c.sender_email,
                    "intent": str(c.intent),
                    "invoice_refs": ref,
                    "issue": "customer quoted an invoice id we have never issued",
                }
            )

        state = (
            self.invoice_states.get(c.resolved_invoice_id)
            if c.resolved_invoice_id
            else None
        )
        holds = self.policy.holds
        self._event(
            day,
            customer_id,
            c.resolved_invoice_id or "",
            "REPLY_RECEIVED",
            f"{c.source_file} -> {c.intent} (conf {c.confidence_score:.2f}, "
            f"engine {c.engine})",
        )

        def set_hold(reason: HoldReason, days: int | None, detail: str) -> None:
            if state is None:
                target = c.resolved_invoice_id
                if target and self.ledger.has_invoice(target) and days is not None:
                    # The invoice exists in the pack but has not been issued yet as
                    # of today. Park the instruction rather than discard it.
                    self.pending_holds.setdefault(target, []).append(
                        (reason, days, detail, day)
                    )
                    self._event(
                        day,
                        customer_id,
                        target,
                        "HOLD_DEFERRED",
                        f"{reason}: {target} is not issued until "
                        f"{self.ledger.invoice(target).issue_date.isoformat()}; "
                        "hold parked until then",
                    )
                else:
                    self._event(
                        day,
                        customer_id,
                        c.resolved_invoice_id or "",
                        "HOLD_NOT_APPLIED",
                        f"{reason}: reply does not name an invoice we can act on",
                    )
                return
            if state.balance <= Decimal("0"):
                self._event(
                    day,
                    customer_id,
                    state.invoice_id,
                    "REPLY_ALREADY_RECONCILED",
                    f"{reason} not applied: invoice was settled on "
                    f"{state.closed_on.isoformat() if state.closed_on else 'or before'}"
                    "; the payment ledger already agrees with the customer",
                )
                return
            expires = None if days is None else day + timedelta(days=days)
            state.holds[reason] = Hold(
                reason=reason, set_on=day, expires_on=expires, detail=detail
            )
            self._event(
                day,
                customer_id,
                state.invoice_id,
                "HOLD_SET",
                f"{reason} until {expires.isoformat() if expires else 'human release'}"
                f" - {detail}",
            )

        match c.intent:
            case Intent.LEGAL_THREAT:
                customer_state.legal_locked = True
                customer_state.legal_locked_on = day
                self._event(
                    day,
                    customer_id,
                    c.resolved_invoice_id or "",
                    "LEGAL_LOCK",
                    "deterministic keyword override; every invoice on this account "
                    "is frozen until a human releases it",
                )
                if state is not None:
                    set_hold(HoldReason.LEGAL_HOLD, None, c.notes or "legal language")

            case Intent.RELATIONSHIP_RISK:
                window = holds.get("relationship_risk_days", 30)
                customer_state.relationship_risk_until = day + timedelta(days=window)
                self._event(
                    day,
                    customer_id,
                    c.resolved_invoice_id or "",
                    "RELATIONSHIP_RISK",
                    f"customer threatened the relationship; automated chasing frozen "
                    f"for {window}d and handed to the account director",
                )

            case Intent.PROMISE_TO_PAY | Intent.PARTIAL_PROMISE:
                if c.promised_payment_date is None:
                    set_hold(
                        HoldReason.UNREADABLE_REPLY,
                        holds.get("unknown_intent_days", 3),
                        "promise with no resolvable date",
                    )
                else:
                    grace = holds.get("promise_to_pay_grace_days", 2)
                    if state is not None:
                        state.promise_date = c.promised_payment_date
                    until = (
                        c.promised_payment_date + timedelta(days=grace) - day
                    ).days
                    set_hold(
                        HoldReason.PROMISE_TO_PAY,
                        max(until, 0),
                        f"promised {c.promised_payment_date.isoformat()} "
                        f"(+{grace}d grace)"
                        + (f"; {c.notes}" if c.intent is Intent.PARTIAL_PROMISE else ""),
                    )

            case Intent.PAYMENT_PLAN_REQUEST:
                set_hold(
                    HoldReason.PAYMENT_PLAN_PENDING,
                    holds.get("payment_plan_review_days", 10),
                    "customer requested a payment plan; agreement needs a human",
                )

            case Intent.CLAIMS_ALREADY_PAID | Intent.REMITTANCE_ADVICE:
                set_hold(
                    HoldReason.PAYMENT_IN_FLIGHT,
                    holds.get("payment_in_flight_hold_days", 5),
                    f"customer states payment made/sent; awaiting bank "
                    f"reconciliation. {c.notes}".strip(),
                )

            case Intent.DISPUTE:
                amount = (
                    f"{c.disputed_amount:.2f}"
                    if c.disputed_amount > 0
                    else "whole balance"
                )
                set_hold(
                    HoldReason.DISPUTE,
                    holds.get("dispute_review_days", 10),
                    f"customer disputes {amount}; chasing suspended pending review",
                )

            case Intent.PO_MISMATCH | Intent.INVOICE_NOT_RECEIVED | Intent.UNRECOGNIZED_INVOICE:
                set_hold(
                    HoldReason.REISSUE_REQUIRED,
                    holds.get("reissue_required_days", 7),
                    "invoice cannot be processed as issued (PO/delivery problem); "
                    "reissue is our action, not theirs",
                )

            case Intent.INFO_REQUEST:
                set_hold(
                    HoldReason.INFO_REQUEST,
                    holds.get("info_request_days", 5),
                    "customer asked us for information before paying",
                )

            case Intent.AUTO_TICKET:
                if state is not None:
                    sla = holds.get("portal_sla_business_days", 10)
                    expires = add_business_days(day, sla)
                    state.holds[HoldReason.PORTAL_SLA] = Hold(
                        reason=HoldReason.PORTAL_SLA,
                        set_on=day,
                        expires_on=expires,
                        detail=f"customer AP portal SLA of {sla} business days",
                    )
                    self._event(
                        day,
                        customer_id,
                        state.invoice_id,
                        "HOLD_SET",
                        f"PORTAL_SLA until {expires.isoformat()} - "
                        f"{sla} business days",
                    )

            case Intent.OUT_OF_OFFICE:
                default_days = holds.get("ooo_defer_days", 3)
                until = c.defer_until or (day + timedelta(days=default_days))
                set_hold(
                    HoldReason.OOO_DEFER,
                    max((until - day).days, default_days),
                    f"contact away until {until.isoformat()}",
                )

            case Intent.BOUNCE:
                dead = self._bounced_address(c)
                if dead:
                    customer_state.undeliverable_emails.add(dead)
                    self._event(
                        day,
                        customer_id,
                        c.resolved_invoice_id or "",
                        "ADDRESS_UNDELIVERABLE",
                        f"{dead} hard-bounced; future contact reroutes to "
                        f"{self.policy.deliverability.get('reroute_to', 'controller')}",
                    )

            case Intent.CONTACT_CHANGE:
                ap_contact = self.ledger.contact(customer_id, ContactRole.AP_CONTACT)
                if ap_contact is not None:
                    customer_state.undeliverable_emails.add(ap_contact.email)
                if c.new_email_address:
                    customer_state.proposed_email_changes.append(
                        (day, c.new_email_address)
                    )
                accept = self.policy.deliverability.get(
                    "accept_inbound_contact_change", False
                )
                self._event(
                    day,
                    customer_id,
                    c.resolved_invoice_id or "",
                    "CONTACT_CHANGE_PROPOSED",
                    f"inbound mail asks us to use {c.new_email_address or 'a new address'}; "
                    + (
                        "accepted by policy"
                        if accept
                        else "NOT applied automatically - a new remit-to contact is a "
                        "human decision; old address marked undeliverable"
                    ),
                )

            case Intent.ACKNOWLEDGEMENT:
                # They engaged. Restart the cadence clock instead of chasing again.
                if state is not None:
                    state.last_contact_date = day
                self._event(
                    day,
                    customer_id,
                    c.resolved_invoice_id or "",
                    "ACKNOWLEDGED",
                    "customer acknowledged without committing; cadence clock reset",
                )

            case Intent.UNKNOWN:
                set_hold(
                    HoldReason.UNREADABLE_REPLY,
                    holds.get("unknown_intent_days", 3),
                    f"reply could not be classified with confidence "
                    f"({c.confidence_score:.2f}); a human should read it",
                )

    def _bounced_address(self, c: Classification) -> str | None:
        """The dead mailbox named in a bounce, if we can identify it."""
        import re

        match = re.search(r"for ([\w.+-]+@[\w-]+\.[\w-]+(?:\.[\w-]+)*)", c.notes)
        if match:
            return match.group(1).lower().rstrip(".")
        if c.customer_id:
            contact = self.ledger.contact(c.customer_id, ContactRole.AP_CONTACT)
            if contact:
                return contact.email
        return None

    # -- 3. hold expiry ----------------------------------------------------- #

    def _expire_holds(self, day: date) -> None:
        for state in self.invoice_states.values():
            for reason, hold in list(state.holds.items()):
                if hold.expires_on is None or day <= hold.expires_on:
                    continue
                del state.holds[reason]
                detail = f"{reason} expired"
                if reason is HoldReason.PROMISE_TO_PAY and state.balance > Decimal("0"):
                    state.promise_broken = True
                    detail = (
                        f"promise of "
                        f"{state.promise_date.isoformat() if state.promise_date else '?'}"
                        f" not honoured; {state.balance:.2f} still outstanding"
                    )
                elif reason is HoldReason.PAYMENT_IN_FLIGHT and state.balance > Decimal(
                    "0"
                ):
                    detail = (
                        "claimed payment never arrived; reconciliation window closed"
                    )
                self._event(
                    day,
                    state.invoice.customer_id,
                    state.invoice_id,
                    "HOLD_EXPIRED",
                    detail,
                )

        for customer_state in self.customer_states.values():
            until = customer_state.relationship_risk_until
            if until is not None and day > until:
                customer_state.relationship_risk_until = None
                self._event(
                    day,
                    customer_state.customer_id,
                    "",
                    "HOLD_EXPIRED",
                    "relationship-risk freeze lapsed; account returns to the ladder "
                    "at the account director's discretion",
                )

    # -- 4. decide and log -------------------------------------------------- #

    def _act(self, day: date, view: LedgerView) -> None:
        window = int(self.policy.grace.get("trailing_window_days", 365))
        stats_cache: dict[str, object] = {}
        candidates: list[tuple[InvoiceState, Decision]] = []

        for state in self.invoice_states.values():
            if state.balance <= Decimal("0"):
                continue
            customer_id = state.invoice.customer_id
            if customer_id not in stats_cache:
                stats_cache[customer_id] = view.customer_stats(customer_id, window)
            decision = policy_engine.evaluate(
                state=state,
                customer_state=self.customer_states[customer_id],
                stats=stats_cache[customer_id],  # type: ignore[arg-type]
                as_of=day,
                policy=self.policy,
                ledger=self.ledger,
            )
            if decision.outcome in (DecisionOutcome.SEND, DecisionOutcome.HOLD):
                candidates.append((state, decision))

        # One customer, one conversation per day: a customer with six overdue
        # invoices gets one email about the largest, not six emails.
        cap = int(self.policy.cadence.get("max_daily_contacts_per_customer", 0))
        sent_today: dict[str, int] = {}
        candidates.sort(key=lambda pair: (-pair[0].balance, pair[0].invoice_id))

        for state, decision in candidates:
            customer_id = state.invoice.customer_id
            customer_facing = decision.outcome is DecisionOutcome.SEND
            if customer_facing and cap:
                if sent_today.get(customer_id, 0) >= cap:
                    continue
                sent_today[customer_id] = sent_today.get(customer_id, 0) + 1
            self._log_action(day, state, decision)

    def _log_action(self, day: date, state: InvoiceState, decision: Decision) -> None:
        invoice = state.invoice
        customer_id = invoice.customer_id
        customer_state = self.customer_states[customer_id]
        customer_name = self.ledger.customer_name(customer_id)

        recipient_contact = (
            self.ledger.contact_for_email(decision.to_emails[0])
            if decision.to_emails
            else None
        )
        cc_contacts = tuple(
            c
            for c in (
                self.ledger.contact_for_email(email) for email in decision.cc_emails
            )
            if c is not None
        )
        sender = self.ledger.contact(customer_id, ContactRole.COLLECTIONS)

        draft = drafting.render(
            self.policy,
            decision.template,
            invoice=invoice,
            customer_name=customer_name,
            balance=state.balance,
            days_past_due=state.days_past_due,
            as_of=day,
            recipient=recipient_contact,
            cc_contacts=cc_contacts,
            sender=sender,
            hold_reason=decision.hold_reason,
            trigger_reason=decision.trigger_reason,
        )
        if (
            self.polish_with_llm
            and decision.outcome is DecisionOutcome.SEND
            and self.llm_config
            and self.llm_config.is_configured
        ):
            draft = drafting.polish_with_llm(draft, self.llm_config)

        tier_label = (
            f"T{decision.tier} {decision.tier_name}" if decision.tier else "internal"
        )
        state.total_reminders += 1
        state.reminders_sent_by_tier[decision.tier] = (
            state.reminders_sent_by_tier.get(decision.tier, 0) + 1
        )
        if decision.outcome is DecisionOutcome.HOLD and decision.hold_reason:
            state.hold_notices.add((decision.hold_reason, decision.tier))
        state.last_contact_date = day
        state.highest_tier_reached = max(state.highest_tier_reached, decision.tier)
        customer_state.reminders_sent += 1
        if decision.action_type is ActionType.AUTO_SEND:
            iso = day.isocalendar()
            key = (iso.year, iso.week)
            customer_state.auto_sends_in_week[key] = (
                customer_state.auto_sends_in_week.get(key, 0) + 1
            )

        self.result.actions.append(
            ActionLog(
                date=day,
                invoice_id=invoice.invoice_id,
                customer_name=customer_name,
                recipient_email=";".join(decision.to_emails),
                recipient_tier=tier_label,
                action_type=str(decision.action_type),
                hold_reason=decision.hold_reason,
                message_body=draft.body,
                subject=draft.subject,
                recipient_role=";".join(decision.to_roles),
                cc_emails=";".join(decision.cc_emails),
                days_past_due=state.days_past_due,
                effective_days_past_due=state.effective_days_past_due,
                balance=state.balance,
                template=decision.template,
                trigger_reason=decision.trigger_reason,
                reminder_seq=state.total_reminders,
                engine=draft.engine,
            )
        )

    # -- 5. reporting ------------------------------------------------------- #

    def _event(
        self, day: date, customer_id: str, invoice_id: str, kind: str, detail: str
    ) -> None:
        self.result.events.append(
            StateEvent(
                date=day,
                customer_id=customer_id,
                invoice_id=invoice_id,
                event_type=kind,
                detail=detail,
            )
        )

    def _report_data_quality(self, end: date) -> None:
        for invoice_id, exported, computed in self.ledger.status_discrepancies(end):
            invoice = self.ledger.invoice(invoice_id)
            self._event(
                end,
                invoice.customer_id,
                invoice_id,
                "DATA_QUALITY",
                f"accounting export says status={exported}, payments say "
                f"{computed}; the agent used the payment ledger",
            )


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def write_outputs(result: ReplayResult, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    log_path = out_dir / REPLAY_LOG_FILENAME
    with log_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(ActionLog.FIELDS))
        writer.writeheader()
        for action in result.actions:
            writer.writerow(action.to_row())
    paths["replay_log"] = log_path

    events_path = out_dir / STATE_EVENTS_FILENAME
    with events_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(StateEvent.FIELDS))
        writer.writeheader()
        for event in result.events:
            writer.writerow(event.to_row())
    paths["state_events"] = events_path

    unmatched_path = out_dir / UNMATCHED_FILENAME
    fields = ["date", "source_file", "sender_email", "intent", "invoice_refs", "issue"]
    with unmatched_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(result.unmatched)
    paths["unmatched_replies"] = unmatched_path

    summary_path = out_dir / SUMMARY_FILENAME
    summary_path.write_text(render_summary(result), encoding="utf-8")
    paths["summary"] = summary_path

    return paths


def render_summary(result: ReplayResult) -> str:
    """A readable companion to the CSV: what happened, and where to look."""
    per_customer: dict[str, list[ActionLog]] = {}
    for action in result.actions:
        per_customer.setdefault(action.customer_name, []).append(action)

    lines = [
        "# Dry-run replay summary",
        "",
        f"Simulated {result.days_simulated} days, "
        f"{result.start_date} to {result.end_date}. Nothing was sent; every row below "
        "is an action the agent would have taken.",
        "",
        f"- **{len(result.actions)} actions**: {result.auto_sends} auto-send, "
        f"{result.held} held for human sign-off",
        f"- {len(result.events)} state events (holds, payments, bounces, data quality)",
        f"- {len(result.unmatched)} replies needing a human to resolve a reference",
        "",
        "## Actions by tier",
        "",
        "| Tier | Actions |",
        "| --- | ---: |",
    ]
    for tier, count in result.by_tier().items():
        lines.append(f"| {tier} | {count} |")

    holds = result.by_hold_reason()
    if holds:
        lines += ["", "## Why actions were held", "", "| Reason | Actions |", "| --- | ---: |"]
        for reason, count in holds.items():
            lines.append(f"| {reason} | {count} |")

    lines += [
        "",
        "## Actions by customer",
        "",
        "| Customer | Actions | Auto-sent | Held | Highest tier |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for name in sorted(per_customer, key=lambda n: -len(per_customer[n])):
        actions = per_customer[name]
        auto = sum(1 for a in actions if a.action_type == ActionType.AUTO_SEND)
        highest = max(
            (a.recipient_tier for a in actions if not a.recipient_tier.startswith("T1 internal")),
            default="-",
            key=lambda t: t[:2],
        )
        lines.append(
            f"| {name} | {len(actions)} | {auto} | {len(actions) - auto} | {highest} |"
        )

    # The inbound replies all land in the final fortnight, which is where the
    # interesting decisions are; give a reviewer that stretch in one place.
    if result.end_date is not None:
        window_start = result.end_date - timedelta(days=14)
        recent = [
            e
            for e in result.events
            if e.date >= window_start
            and e.event_type
            not in ("INVOICE_SETTLED", "PARTIAL_PAYMENT", "HOLD_EXPIRED")
        ]
        lines += [
            "",
            f"## Reply handling, {window_start} to {result.end_date}",
            "",
            "| Date | Customer | Invoice | Event | Detail |",
            "| --- | --- | --- | --- | --- |",
        ]
        for event in recent:
            detail = event.detail.replace("|", "/")
            lines.append(
                f"| {event.date} | {event.customer_id or '-'} "
                f"| {event.invoice_id or '-'} | {event.event_type} | {detail} |"
            )

    lines += [
        "",
        "## Where to look",
        "",
        f"- `{REPLAY_LOG_FILENAME}` - every action with its full drafted message body",
        f"- `{STATE_EVENTS_FILENAME}` - the decisions behind those actions",
        f"- `{UNMATCHED_FILENAME}` - references the agent refused to guess at",
        "",
    ]
    return "\n".join(lines)


def simulate(
    ledger: AsOfLedger,
    policy: Policy,
    classifications: Sequence[Classification],
    *,
    start: date | None = None,
    end: date | None = None,
    polish_with_llm: bool = False,
    llm_config: llm.LLMConfig | None = None,
) -> ReplayResult:
    """Convenience wrapper: resolve the window from policy/data and run."""
    cfg_start, cfg_end = policy.simulation_window()
    data_start, data_end = ledger.date_bounds()
    start = start or cfg_start or data_start
    end = end or cfg_end or data_end
    engine = ReplayEngine(
        ledger,
        policy,
        classifications,
        polish_with_llm=polish_with_llm,
        llm_config=llm_config,
    )
    return engine.run(start, end)


def iter_reply_files(data_dir: Path) -> Iterable[Path]:
    return sorted((data_dir / "inbound_replies").glob("*.txt"))


__all__ = [
    "CACHE_FILENAME",
    "REPLAY_LOG_FILENAME",
    "ReplayEngine",
    "ReplayResult",
    "add_business_days",
    "simulate",
    "write_outputs",
]
