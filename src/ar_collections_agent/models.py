"""Typed domain state for the collections agent.

Money is always ``Decimal``. Dates are always ``datetime.date``. Nothing in this
module performs IO or reads a clock: every temporal value is passed in, so the
replay engine fully controls what "now" means.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar

# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class ContactRole(StrEnum):
    """Roles as they appear in ``contacts.csv`` ``contact_type``."""

    AP_CONTACT = "ap_contact"
    CONTROLLER = "controller"
    CEO = "ceo"
    OWNER = "owner"
    SALES_OWNER = "sales_owner"
    COLLECTIONS = "collections"


class Intent(StrEnum):
    """What an inbound customer reply means for the invoice."""

    PROMISE_TO_PAY = "PROMISE_TO_PAY"
    PARTIAL_PROMISE = "PARTIAL_PROMISE"
    PAYMENT_PLAN_REQUEST = "PAYMENT_PLAN_REQUEST"
    CLAIMS_ALREADY_PAID = "CLAIMS_ALREADY_PAID"
    REMITTANCE_ADVICE = "REMITTANCE_ADVICE"
    DISPUTE = "DISPUTE"
    LEGAL_THREAT = "LEGAL_THREAT"
    PO_MISMATCH = "PO_MISMATCH"
    INVOICE_NOT_RECEIVED = "INVOICE_NOT_RECEIVED"
    UNRECOGNIZED_INVOICE = "UNRECOGNIZED_INVOICE"
    INFO_REQUEST = "INFO_REQUEST"
    ACKNOWLEDGEMENT = "ACKNOWLEDGEMENT"
    OUT_OF_OFFICE = "OUT_OF_OFFICE"
    AUTO_TICKET = "AUTO_TICKET"
    BOUNCE = "BOUNCE"
    CONTACT_CHANGE = "CONTACT_CHANGE"
    RELATIONSHIP_RISK = "RELATIONSHIP_RISK"
    UNKNOWN = "UNKNOWN"


class HoldReason(StrEnum):
    """Why escalation is suppressed. Ordered by precedence in ``policy_engine``."""

    LEGAL_HOLD = "LEGAL_HOLD"
    DISPUTE = "DISPUTE"
    PAYMENT_IN_FLIGHT = "PAYMENT_IN_FLIGHT"
    PROMISE_TO_PAY = "PROMISE_TO_PAY"
    PAYMENT_PLAN_PENDING = "PAYMENT_PLAN_PENDING"
    REISSUE_REQUIRED = "REISSUE_REQUIRED"
    INFO_REQUEST = "INFO_REQUEST"
    PORTAL_SLA = "PORTAL_SLA"
    RELATIONSHIP_RISK = "RELATIONSHIP_RISK"
    DE_MINIMIS = "DE_MINIMIS"
    OOO_DEFER = "OOO_DEFER"
    UNREADABLE_REPLY = "UNREADABLE_REPLY"
    CADENCE_EXHAUSTED = "CADENCE_EXHAUSTED"
    UNDELIVERABLE = "UNDELIVERABLE"


class ActionType(StrEnum):
    AUTO_SEND = "AUTO_SEND"
    HELD_FOR_APPROVAL = "HELD_FOR_APPROVAL"
    NO_ACTION = "NO_ACTION"


class DecisionOutcome(StrEnum):
    """Internal result of a policy evaluation for one invoice on one day."""

    SEND = "SEND"
    HOLD = "HOLD"
    SUPPRESS = "SUPPRESS"  # nothing to do, nothing to log
    CLOSED = "CLOSED"


# --------------------------------------------------------------------------- #
# Static reference data
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Customer:
    customer_id: str
    customer_name: str
    payment_terms: str

    @property
    def terms_days(self) -> int:
        """``"Net 45"`` -> ``45``."""
        return int(self.payment_terms.rsplit(" ", 1)[-1])


@dataclass(frozen=True, slots=True)
class Contact:
    customer_id: str
    side: str  # "customer" | "provider"
    contact_type: ContactRole
    name: str
    email: str
    title: str

    @property
    def first_name(self) -> str:
        return self.name.split(" ", 1)[0]


@dataclass(frozen=True, slots=True)
class Invoice:
    invoice_id: str
    customer_id: str
    issue_date: date
    due_date: date
    amount: Decimal
    terms: str
    status: str  # as exported by the accounting system; never trusted for logic


@dataclass(frozen=True, slots=True)
class Payment:
    invoice_id: str
    payment_date: date
    amount: Decimal
    method: str


@dataclass(frozen=True, slots=True)
class InboundEmail:
    """One file from ``data/inbound_replies/``."""

    source_file: str
    sender_email: str
    received_date: date
    subject: str
    body: str

    @property
    def full_text(self) -> str:
        return f"{self.subject}\n{self.body}"


# --------------------------------------------------------------------------- #
# Classifier output
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Classification:
    """Structured meaning of one inbound email.

    ``engine`` records provenance: ``rules``, ``llm``, or ``override`` when the
    deterministic legal-keyword check overruled whatever the model said.
    """

    source_file: str
    received_date: date
    sender_email: str
    intent: Intent
    promised_payment_date: date | None = None
    defer_until: date | None = None  # e.g. an out-of-office return date
    disputed_amount: Decimal = Decimal("0.00")
    confidence_score: float = 0.0
    engine: str = "rules"
    invoice_refs: tuple[str, ...] = ()
    customer_id: str | None = None
    resolved_invoice_id: str | None = None
    unmatched_refs: tuple[str, ...] = ()
    new_email_address: str | None = None
    notes: str = ""

    def to_json_dict(self) -> dict[str, object]:
        """The interchange shape required by the spec, plus provenance fields."""
        return {
            "source_file": self.source_file,
            "received_date": self.received_date.isoformat(),
            "sender_email": self.sender_email,
            "intent": str(self.intent),
            "promised_payment_date": (
                self.promised_payment_date.isoformat()
                if self.promised_payment_date
                else None
            ),
            "defer_until": self.defer_until.isoformat() if self.defer_until else None,
            "disputed_amount": float(self.disputed_amount),
            "confidence_score": round(self.confidence_score, 2),
            "engine": self.engine,
            "invoice_refs": list(self.invoice_refs),
            "customer_id": self.customer_id,
            "resolved_invoice_id": self.resolved_invoice_id,
            "unmatched_refs": list(self.unmatched_refs),
            "new_email_address": self.new_email_address,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- #
# Mutable simulation state (owned by the replay engine)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Hold:
    reason: HoldReason
    set_on: date
    expires_on: date | None  # None => absorbing, only a human clears it
    detail: str = ""

    def is_active(self, as_of: date) -> bool:
        if self.expires_on is None:
            return True
        return as_of <= self.expires_on


@dataclass(slots=True)
class InvoiceState:
    """Everything the agent has learned about one invoice, carried day to day."""

    invoice: Invoice
    balance: Decimal
    days_past_due: int = 0
    effective_days_past_due: int = 0
    holds: dict[HoldReason, Hold] = field(default_factory=dict)
    # (hold reason, tier) pairs already reported internally, so a long-running
    # hold produces one handover note rather than one every cadence interval.
    hold_notices: set[tuple[str, int]] = field(default_factory=set)
    reminders_sent_by_tier: dict[int, int] = field(default_factory=dict)
    total_reminders: int = 0
    last_contact_date: date | None = None
    highest_tier_reached: int = 0
    promise_date: date | None = None
    promise_broken: bool = False
    closed_on: date | None = None

    @property
    def invoice_id(self) -> str:
        return self.invoice.invoice_id

    def active_holds(self, as_of: date) -> list[Hold]:
        return [h for h in self.holds.values() if h.is_active(as_of)]


@dataclass(slots=True)
class CustomerState:
    """Account-level state: legal locks and dead mailboxes outlive one invoice."""

    customer: Customer
    legal_locked: bool = False
    legal_locked_on: date | None = None
    relationship_risk_until: date | None = None
    undeliverable_emails: set[str] = field(default_factory=set)
    proposed_email_changes: list[tuple[date, str]] = field(default_factory=list)
    contacts_last_emailed: dict[date, int] = field(default_factory=dict)
    auto_sends_in_week: dict[tuple[int, int], int] = field(default_factory=dict)
    replies_received: int = 0
    reminders_sent: int = 0

    @property
    def customer_id(self) -> str:
        return self.customer.customer_id


# --------------------------------------------------------------------------- #
# Engine outputs
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Decision:
    """Result of evaluating one invoice on one simulated day."""

    outcome: DecisionOutcome
    tier: int = 0
    tier_name: str = ""
    to_emails: tuple[str, ...] = ()
    to_roles: tuple[str, ...] = ()
    cc_emails: tuple[str, ...] = ()
    template: str = ""
    action_type: ActionType = ActionType.NO_ACTION
    hold_reason: str = ""
    trigger_reason: str = ""
    suppress_reason: str = ""


@dataclass(slots=True)
class ActionLog:
    """One row of ``output/dry_run_replay_log.csv``.

    The first eight fields are the columns the exercise asks for, in order; the
    rest are appended context so a reviewer can see *why* each row exists.
    """

    date: date
    invoice_id: str
    customer_name: str
    recipient_email: str
    recipient_tier: str
    action_type: str
    hold_reason: str
    message_body: str
    subject: str
    recipient_role: str
    cc_emails: str
    days_past_due: int
    effective_days_past_due: int
    balance: Decimal
    template: str
    trigger_reason: str
    reminder_seq: int
    engine: str

    FIELDS: ClassVar[tuple[str, ...]] = (
        "date",
        "invoice_id",
        "customer_name",
        "recipient_email",
        "recipient_tier",
        "action_type",
        "hold_reason",
        "message_body",
        "subject",
        "recipient_role",
        "cc_emails",
        "days_past_due",
        "effective_days_past_due",
        "balance",
        "template",
        "trigger_reason",
        "reminder_seq",
        "engine",
    )

    def to_row(self) -> dict[str, str]:
        return {
            "date": self.date.isoformat(),
            "invoice_id": self.invoice_id,
            "customer_name": self.customer_name,
            "recipient_email": self.recipient_email,
            "recipient_tier": self.recipient_tier,
            "action_type": self.action_type,
            "hold_reason": self.hold_reason,
            "message_body": self.message_body,
            "subject": self.subject,
            "recipient_role": self.recipient_role,
            "cc_emails": self.cc_emails,
            "days_past_due": str(self.days_past_due),
            "effective_days_past_due": str(self.effective_days_past_due),
            "balance": f"{self.balance:.2f}",
            "template": self.template,
            "trigger_reason": self.trigger_reason,
            "reminder_seq": str(self.reminder_seq),
            "engine": self.engine,
        }


@dataclass(slots=True)
class StateEvent:
    """A non-message decision worth auditing: holds, bounces, data quality."""

    date: date
    customer_id: str
    invoice_id: str
    event_type: str
    detail: str

    FIELDS: ClassVar[tuple[str, ...]] = (
        "date",
        "customer_id",
        "invoice_id",
        "event_type",
        "detail",
    )

    def to_row(self) -> dict[str, str]:
        return {
            "date": self.date.isoformat(),
            "customer_id": self.customer_id,
            "invoice_id": self.invoice_id,
            "event_type": self.event_type,
            "detail": self.detail,
        }


@dataclass(slots=True)
class RiskAssessment:
    """One open invoice's late-payment risk as of the ledger date."""

    invoice_id: str
    customer_id: str
    customer_name: str
    due_date: date
    balance: Decimal
    days_past_due: int
    risk_band: str
    risk_score: float
    predicted_days_late: int
    predicted_payment_date: date
    reason: str

    FIELDS: ClassVar[tuple[str, ...]] = (
        "risk_band",
        "risk_score",
        "invoice_id",
        "customer_id",
        "customer_name",
        "due_date",
        "balance",
        "days_past_due",
        "predicted_days_late",
        "predicted_payment_date",
        "reason",
    )

    def to_row(self) -> dict[str, str]:
        return {
            "risk_band": self.risk_band,
            "risk_score": f"{self.risk_score:.3f}",
            "invoice_id": self.invoice_id,
            "customer_id": self.customer_id,
            "customer_name": self.customer_name,
            "due_date": self.due_date.isoformat(),
            "balance": f"{self.balance:.2f}",
            "days_past_due": str(self.days_past_due),
            "predicted_days_late": str(self.predicted_days_late),
            "predicted_payment_date": self.predicted_payment_date.isoformat(),
            "reason": self.reason,
        }
