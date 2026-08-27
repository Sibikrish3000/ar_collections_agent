"""Shared fixtures. The real data pack is used deliberately: the invariants worth
testing (no future leakage, no executive auto-sends) are properties of the agent
against real history, not against a toy ledger."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from fs_collections_agent import email_classifier, replay_engine
from fs_collections_agent.config import Policy
from fs_collections_agent.ledger import AsOfLedger
from fs_collections_agent.models import (
    Contact,
    ContactRole,
    Customer,
    CustomerState,
    InvoiceState,
    Invoice,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
CONFIG_PATH = REPO_ROOT / "config" / "escalation_policy.yaml"

LEDGER_DATE = date(2026, 8, 26)
# Default due date for synthetic invoices: a Tuesday, chosen so the day offsets
# the policy tests use (+3, +14, +30, +45 ...) land on weekdays.
DEFAULT_DUE = date(2026, 6, 30)


@pytest.fixture(scope="session")
def policy() -> Policy:
    return Policy.load(CONFIG_PATH)


@pytest.fixture(scope="session")
def ledger() -> AsOfLedger:
    return AsOfLedger.from_data_dir(DATA_DIR)


@pytest.fixture(scope="session")
def classifications(ledger: AsOfLedger, policy: Policy):
    """Classify from source rather than the cache, so tests check the engine."""
    return email_classifier.EmailClassifier(ledger, policy).classify_all(
        ledger.all_emails
    )


@pytest.fixture(scope="session")
def replay(ledger: AsOfLedger, policy: Policy, classifications):
    return replay_engine.simulate(ledger, policy, classifications, end=LEDGER_DATE)


# --------------------------------------------------------------------------- #
# Builders for unit-level policy tests
# --------------------------------------------------------------------------- #


def make_invoice(
    invoice_id: str = "INV-TEST",
    customer_id: str = "C-01",
    due: date = DEFAULT_DUE,
    amount: str = "10000.00",
    terms: str = "Net 45",
) -> Invoice:
    return Invoice(
        invoice_id=invoice_id,
        customer_id=customer_id,
        issue_date=due,
        due_date=due,
        amount=Decimal(amount),
        terms=terms,
        status="open",
    )


def make_state(invoice: Invoice | None = None, balance: str | None = None) -> InvoiceState:
    invoice = invoice or make_invoice()
    return InvoiceState(
        invoice=invoice,
        balance=Decimal(balance) if balance else invoice.amount,
    )


def make_customer_state(customer_id: str = "C-01", name: str = "Test Co") -> CustomerState:
    return CustomerState(
        customer=Customer(
            customer_id=customer_id, customer_name=name, payment_terms="Net 45"
        )
    )


def single_customer_ledger(customer_id: str = "C-01") -> AsOfLedger:
    """A ledger with one customer and a full contact set, for policy unit tests."""
    customer = Customer(
        customer_id=customer_id, customer_name="Test Co", payment_terms="Net 45"
    )
    roles = [
        (ContactRole.AP_CONTACT, "Ada Payable", "ap@test.example", "Accounts Payable"),
        (ContactRole.CONTROLLER, "Cal Controller", "controller@test.example", "FC"),
        (ContactRole.CEO, "Cleo Chief", "ceo@test.example", "CEO"),
        (ContactRole.OWNER, "Cleo Chief", "ceo@test.example", "Owner"),
        (ContactRole.SALES_OWNER, "Sam Sales", "director@provider.example", "AD"),
        (ContactRole.COLLECTIONS, "Ana Belova", "ar@provider.example", "AR"),
    ]
    contacts = [
        Contact(
            customer_id=customer_id,
            side="provider" if role in (ContactRole.SALES_OWNER, ContactRole.COLLECTIONS)
            else "customer",
            contact_type=role,
            name=name,
            email=email,
            title=title,
        )
        for role, name, email, title in roles
    ]
    return AsOfLedger([customer], contacts, [make_invoice()], [], [])
