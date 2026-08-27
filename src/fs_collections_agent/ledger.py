"""The as-of ledger: the single gate through which the engine sees data.

The exercise's hard requirement is that on simulated date ``T`` the agent knows
only what was knowable on ``T``. Rather than sprinkle ``if row.date <= T`` checks
through the engine, all reads go through :class:`AsOfLedger`, which hands out an
immutable :class:`LedgerView` filtered to ``T``. The engine never receives the
underlying collections, so future leakage is structurally impossible rather than
merely avoided.

``invoices.csv`` ships a ``status`` column. It is deliberately ignored for
decisions: ``INV-2231`` is marked ``open`` in the pack yet was paid in full on
2026-08-11. Balances are always recomputed from ``payments.csv``.
"""

from __future__ import annotations

import csv
import statistics
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from .models import Contact, ContactRole, Customer, InboundEmail, Invoice, Payment

CENTS = Decimal("0.01")


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def _read_csv(path: Path) -> Iterator[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def _parse_email_file(path: Path) -> InboundEmail:
    """Parse an ``inbound_replies/*.txt`` file (RFC-ish headers, blank line, body)."""
    raw = path.read_text(encoding="utf-8")
    headers: dict[str, str] = {}
    lines = raw.splitlines()
    body_start = 0
    for i, line in enumerate(lines):
        if not line.strip():
            body_start = i + 1
            break
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        body_start = i + 1

    missing = {"from", "date"} - headers.keys()
    if missing:
        raise ValueError(f"{path.name}: missing header(s) {sorted(missing)}")

    return InboundEmail(
        source_file=path.name,
        sender_email=headers["from"].strip().lower(),
        received_date=date.fromisoformat(headers["date"]),
        subject=headers.get("subject", ""),
        body="\n".join(lines[body_start:]).strip(),
    )


@dataclass(frozen=True, slots=True)
class CustomerStats:
    """Payment behaviour of one customer, computed from a single as-of slice."""

    customer_id: str
    invoices_settled: int
    mean_days_late: float
    median_days_late: float
    stdev_days_late: float
    pct_paid_late: float
    max_days_late: int

    @property
    def has_history(self) -> bool:
        return self.invoices_settled > 0


class LedgerView:
    """Immutable projection of the ledger as it was known on ``as_of``."""

    __slots__ = ("as_of", "_ledger", "_invoices", "_payments_by_invoice", "_emails")

    def __init__(
        self,
        as_of: date,
        ledger: AsOfLedger,
        invoices: tuple[Invoice, ...],
        payments_by_invoice: dict[str, tuple[Payment, ...]],
        emails: tuple[InboundEmail, ...],
    ) -> None:
        self.as_of = as_of
        self._ledger = ledger
        self._invoices = invoices
        self._payments_by_invoice = payments_by_invoice
        self._emails = emails

    # -- issued documents --------------------------------------------------- #

    @property
    def invoices(self) -> tuple[Invoice, ...]:
        """Invoices issued on or before ``as_of``."""
        return self._invoices

    def payments_for(self, invoice_id: str) -> tuple[Payment, ...]:
        """Payments received on or before ``as_of``, oldest first."""
        return self._payments_by_invoice.get(invoice_id, ())

    def emails_on(self, day: date) -> tuple[InboundEmail, ...]:
        """Emails received exactly on ``day`` (``day`` must be <= ``as_of``)."""
        if day > self.as_of:
            raise ValueError(f"emails_on({day}) requested from a view as of {self.as_of}")
        return tuple(e for e in self._emails if e.received_date == day)

    @property
    def emails(self) -> tuple[InboundEmail, ...]:
        """Emails received on or before ``as_of``."""
        return self._emails

    # -- money -------------------------------------------------------------- #

    def amount_paid(self, invoice_id: str) -> Decimal:
        return sum(
            (p.amount for p in self.payments_for(invoice_id)), start=Decimal("0")
        ).quantize(CENTS)

    def balance(self, invoice: Invoice) -> Decimal:
        return (invoice.amount - self.amount_paid(invoice.invoice_id)).quantize(CENTS)

    def open_invoices(self) -> tuple[Invoice, ...]:
        """Issued, with a positive balance as of ``as_of``. Ignores ``status``."""
        return tuple(i for i in self._invoices if self.balance(i) > Decimal("0"))

    def settlement_date(self, invoice_id: str) -> date | None:
        """Date the final payment landed, if the invoice is settled as of ``as_of``."""
        invoice = self._ledger.invoice(invoice_id)
        payments = self.payments_for(invoice_id)
        if not payments:
            return None
        if (invoice.amount - sum((p.amount for p in payments), Decimal("0"))) > Decimal(
            "0"
        ):
            return None
        return max(p.payment_date for p in payments)

    # -- derived behaviour -------------------------------------------------- #

    def customer_stats(
        self, customer_id: str, trailing_window_days: int | None = None
    ) -> CustomerStats:
        """Lateness statistics from invoices settled on or before ``as_of``.

        ``trailing_window_days`` restricts the sample to invoices settled inside
        that window, so the agent's view of a customer can change over time.
        """
        cutoff = (
            self.as_of - timedelta(days=trailing_window_days)
            if trailing_window_days
            else None
        )
        lateness: list[int] = []
        for invoice in self._invoices:
            if invoice.customer_id != customer_id:
                continue
            settled = self.settlement_date(invoice.invoice_id)
            if settled is None or (cutoff is not None and settled < cutoff):
                continue
            lateness.append((settled - invoice.due_date).days)

        if not lateness:
            return CustomerStats(customer_id, 0, 0.0, 0.0, 0.0, 0.0, 0)

        late_only = [d for d in lateness if d > 0]
        return CustomerStats(
            customer_id=customer_id,
            invoices_settled=len(lateness),
            mean_days_late=statistics.fmean(lateness),
            median_days_late=statistics.median(lateness),
            stdev_days_late=statistics.pstdev(lateness) if len(lateness) > 1 else 0.0,
            pct_paid_late=len(late_only) / len(lateness),
            max_days_late=max(lateness),
        )


class AsOfLedger:
    """Loads the data pack once, then serves date-filtered views of it."""

    def __init__(
        self,
        customers: Iterable[Customer],
        contacts: Iterable[Contact],
        invoices: Iterable[Invoice],
        payments: Iterable[Payment],
        emails: Iterable[InboundEmail],
    ) -> None:
        self.customers: dict[str, Customer] = {c.customer_id: c for c in customers}
        self.contacts: tuple[Contact, ...] = tuple(contacts)
        self._invoices: tuple[Invoice, ...] = tuple(
            sorted(invoices, key=lambda i: (i.issue_date, i.invoice_id))
        )
        self._payments: tuple[Payment, ...] = tuple(
            sorted(payments, key=lambda p: (p.payment_date, p.invoice_id))
        )
        self._emails: tuple[InboundEmail, ...] = tuple(
            sorted(emails, key=lambda e: (e.received_date, e.source_file))
        )
        self._by_id: dict[str, Invoice] = {i.invoice_id: i for i in self._invoices}

        self._contacts_by_customer: dict[str, dict[ContactRole, Contact]] = {}
        for contact in self.contacts:
            self._contacts_by_customer.setdefault(contact.customer_id, {})[
                contact.contact_type
            ] = contact

        self._contact_by_email: dict[str, Contact] = {
            c.email.lower(): c for c in self.contacts
        }
        self._domain_to_customer: dict[str, str] = {}
        for contact in self.contacts:
            if contact.side != "customer":
                continue
            domain = contact.email.split("@", 1)[-1].lower()
            self._domain_to_customer.setdefault(domain, contact.customer_id)

    # -- construction ------------------------------------------------------- #

    @classmethod
    def from_data_dir(cls, data_dir: Path) -> AsOfLedger:
        customers = [
            Customer(
                customer_id=r["customer_id"],
                customer_name=r["customer_name"],
                payment_terms=r["payment_terms"],
            )
            for r in _read_csv(data_dir / "customers.csv")
        ]
        contacts = [
            Contact(
                customer_id=r["customer_id"],
                side=r["side"],
                contact_type=ContactRole(r["contact_type"]),
                name=r["name"],
                email=r["email"].strip().lower(),
                title=r["title"],
            )
            for r in _read_csv(data_dir / "contacts.csv")
        ]
        invoices = [
            Invoice(
                invoice_id=r["invoice_id"],
                customer_id=r["customer_id"],
                issue_date=date.fromisoformat(r["issue_date"]),
                due_date=date.fromisoformat(r["due_date"]),
                amount=Decimal(r["amount"]).quantize(CENTS),
                terms=r["terms"],
                status=r["status"],
            )
            for r in _read_csv(data_dir / "invoices.csv")
        ]
        payments = [
            Payment(
                invoice_id=r["invoice_id"],
                payment_date=date.fromisoformat(r["payment_date"]),
                amount=Decimal(r["amount"]).quantize(CENTS),
                method=r["method"],
            )
            for r in _read_csv(data_dir / "payments.csv")
        ]
        reply_dir = data_dir / "inbound_replies"
        emails = [
            _parse_email_file(p) for p in sorted(reply_dir.glob("*.txt"))
        ] if reply_dir.is_dir() else []

        return cls(customers, contacts, invoices, payments, emails)

    # -- whole-ledger reference lookups (date-independent) ------------------ #

    def invoice(self, invoice_id: str) -> Invoice:
        return self._by_id[invoice_id]

    def has_invoice(self, invoice_id: str) -> bool:
        return invoice_id in self._by_id

    def customer_name(self, customer_id: str) -> str:
        customer = self.customers.get(customer_id)
        return customer.customer_name if customer else customer_id

    def contact(self, customer_id: str, role: ContactRole | str) -> Contact | None:
        return self._contacts_by_customer.get(customer_id, {}).get(ContactRole(role))

    def contact_for_email(self, email: str) -> Contact | None:
        return self._contact_by_email.get(email.strip().lower())

    def customer_for_email(self, email: str) -> str | None:
        """Resolve a sender to a customer by exact contact, then by domain."""
        email = email.strip().lower()
        contact = self._contact_by_email.get(email)
        if contact is not None:
            return contact.customer_id
        return self._domain_to_customer.get(email.split("@", 1)[-1])

    @property
    def all_invoices(self) -> tuple[Invoice, ...]:
        """Every invoice in the pack. For reporting and bounds only."""
        return self._invoices

    @property
    def all_emails(self) -> tuple[InboundEmail, ...]:
        return self._emails

    def date_bounds(self) -> tuple[date, date]:
        """Earliest issue date, latest of the last issue/payment/email date."""
        first = min(i.issue_date for i in self._invoices)
        last = max(i.due_date for i in self._invoices)
        if self._payments:
            last = max(last, max(p.payment_date for p in self._payments))
        if self._emails:
            last = max(last, max(e.received_date for e in self._emails))
        return first, last

    # -- the gate ----------------------------------------------------------- #

    def view(self, as_of: date) -> LedgerView:
        """Everything knowable on ``as_of``, and nothing dated after it."""
        invoices = tuple(i for i in self._invoices if i.issue_date <= as_of)
        payments_by_invoice: dict[str, list[Payment]] = {}
        for payment in self._payments:
            if payment.payment_date <= as_of:
                payments_by_invoice.setdefault(payment.invoice_id, []).append(payment)
        emails = tuple(e for e in self._emails if e.received_date <= as_of)
        return LedgerView(
            as_of=as_of,
            ledger=self,
            invoices=invoices,
            payments_by_invoice={k: tuple(v) for k, v in payments_by_invoice.items()},
            emails=emails,
        )

    # -- data quality ------------------------------------------------------- #

    def status_discrepancies(self, as_of: date) -> list[tuple[str, str, str]]:
        """``(invoice_id, exported_status, computed_state)`` where the two differ.

        Catches stale accounting exports such as ``INV-2231``: exported ``open``,
        actually settled. Reported, never silently corrected.
        """
        view = self.view(as_of)
        out: list[tuple[str, str, str]] = []
        for invoice in view.invoices:
            balance = view.balance(invoice)
            computed = "open" if balance > Decimal("0") else "paid"
            if invoice.status != computed:
                out.append((invoice.invoice_id, invoice.status, computed))
        return out
