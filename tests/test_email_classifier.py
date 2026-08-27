"""Intent parsing, deterministic date resolution, and the legal override.

The expectations below are the *whole* reply set: every one of the twenty
messages in the pack has a named intent, so a regression that silently downgrades
a message to UNKNOWN fails here.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ar_collections_agent import llm
from ar_collections_agent.email_classifier import (
    EmailClassifier,
    load_or_classify,
    read_cache,
    resolve_dates,
    write_cache,
)
from ar_collections_agent.models import InboundEmail, Intent

EXPECTED_INTENTS = {
    "01_reply.txt": Intent.OUT_OF_OFFICE,
    "02_reply.txt": Intent.CLAIMS_ALREADY_PAID,
    "03_reply.txt": Intent.CLAIMS_ALREADY_PAID,
    "04_reply.txt": Intent.DISPUTE,
    "05_reply.txt": Intent.PARTIAL_PROMISE,
    "06_reply.txt": Intent.CONTACT_CHANGE,
    "07_reply.txt": Intent.RELATIONSHIP_RISK,
    "08_reply.txt": Intent.ACKNOWLEDGEMENT,
    "09_reply.txt": Intent.BOUNCE,
    "10_reply.txt": Intent.AUTO_TICKET,
    "11_reply.txt": Intent.LEGAL_THREAT,
    "12_reply.txt": Intent.UNRECOGNIZED_INVOICE,
    "13_reply.txt": Intent.INVOICE_NOT_RECEIVED,
    "14_reply.txt": Intent.PAYMENT_PLAN_REQUEST,
    "15_reply.txt": Intent.PROMISE_TO_PAY,
    "16_reply.txt": Intent.INFO_REQUEST,
    "17_reply.txt": Intent.INFO_REQUEST,
    "18_reply.txt": Intent.UNKNOWN,
    "19_reply.txt": Intent.REMITTANCE_ADVICE,
    "20_reply.txt": Intent.PO_MISMATCH,
}


def by_file(classifications) -> dict[str, object]:
    return {c.source_file: c for c in classifications}


def test_every_reply_has_the_expected_intent(classifications) -> None:
    actual = {f: c.intent for f, c in by_file(classifications).items()}
    assert actual == EXPECTED_INTENTS


def test_every_reply_resolves_to_a_customer(classifications) -> None:
    unresolved = [c.source_file for c in classifications if c.customer_id is None]
    assert unresolved == []


def test_promise_date_is_extracted(classifications) -> None:
    promise = by_file(classifications)["15_reply.txt"]
    assert promise.promised_payment_date == date(2026, 8, 29)


def test_split_promise_holds_until_the_balance_clears(classifications) -> None:
    """Reply 05 offers 50% on Friday and the rest on the 30th."""
    partial = by_file(classifications)["05_reply.txt"]
    assert partial.promised_payment_date == date(2026, 8, 30)
    assert "split payment offered" in partial.notes


def test_out_of_office_return_date_is_used(classifications) -> None:
    ooo = by_file(classifications)["01_reply.txt"]
    assert ooo.defer_until == date(2026, 9, 1)


def test_hours_are_not_mistaken_for_money(classifications) -> None:
    """Reply 04 argues 112 hours vs 140 - neither is a disputed dollar amount."""
    dispute = by_file(classifications)["04_reply.txt"]
    assert dispute.disputed_amount == Decimal("0.00")
    assert "whole balance treated as disputed" in dispute.notes


def test_bounce_identifies_the_dead_mailbox(classifications) -> None:
    bounce = by_file(classifications)["09_reply.txt"]
    assert bounce.customer_id == "C-11"
    assert "sam.ito@ingleby.com" in bounce.notes


def test_contact_change_captures_the_proposed_address(classifications) -> None:
    change = by_file(classifications)["06_reply.txt"]
    assert change.new_email_address == "ap-team@vantage.com"


def test_unknown_invoice_references_are_flagged(classifications) -> None:
    files = by_file(classifications)
    assert files["12_reply.txt"].unmatched_refs == ("INV-9911",)
    assert files["19_reply.txt"].unmatched_refs == ("INV-9999",)


def test_unknown_sender_is_noted_as_unverified(classifications) -> None:
    """Reply 17 comes from someone who is not on the account."""
    new_person = by_file(classifications)["17_reply.txt"]
    assert new_person.customer_id == "C-10"
    assert "not a known contact" in new_person.notes


def test_bare_question_mark_is_not_guessed(classifications) -> None:
    unknown = by_file(classifications)["18_reply.txt"]
    assert unknown.intent is Intent.UNKNOWN
    assert unknown.confidence_score < 0.5


# --------------------------------------------------------------------------- #
# The override that matters
# --------------------------------------------------------------------------- #


def test_legal_keywords_override_the_model(ledger, policy, monkeypatch) -> None:
    """Even if the model is confident it is a promise, legal language wins."""
    classifier = EmailClassifier(ledger, policy, use_llm=True)
    monkeypatch.setattr(
        EmailClassifier,
        "_llm_intent",
        lambda self, email: (Intent.PROMISE_TO_PAY, 0.99, ""),
    )
    email = next(e for e in ledger.all_emails if e.source_file == "11_reply.txt")

    result = classifier.classify(email)
    assert result.intent is Intent.LEGAL_THREAT
    assert result.engine == "override"
    assert result.confidence_score == 1.0


def test_llm_label_is_used_when_no_override_applies(ledger, policy, monkeypatch) -> None:
    classifier = EmailClassifier(ledger, policy, use_llm=True)
    monkeypatch.setattr(
        EmailClassifier,
        "_llm_intent",
        lambda self, email: (Intent.DISPUTE, 0.88, ""),
    )
    email = next(e for e in ledger.all_emails if e.source_file == "08_reply.txt")

    result = classifier.classify(email)
    assert result.intent is Intent.DISPUTE
    assert result.engine == "llm"
    assert "rules=ACKNOWLEDGEMENT" in result.notes


def test_unconfigured_endpoint_degrades_to_rules(ledger, policy) -> None:
    classifier = EmailClassifier(
        ledger, policy, use_llm=True, llm_config=llm.LLMConfig()
    )
    email = next(e for e in ledger.all_emails if e.source_file == "15_reply.txt")

    result = classifier.classify(email)
    assert result.intent is Intent.PROMISE_TO_PAY
    assert result.engine == "rules"
    assert "llm_unavailable" in result.notes


def test_endpoint_settings_come_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "my-model")

    config = llm.LLMConfig.from_env()
    assert config.base_url == "https://gateway.example/v1"
    assert config.api_key == "test-key"
    assert config.model == "my-model"
    assert config.is_configured
    assert config.describe() == "my-model @ https://gateway.example/v1"


def test_openai_env_vars_are_honoured_as_fallbacks(monkeypatch) -> None:
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "llama3.1")

    config = llm.LLMConfig.from_env()
    assert config.base_url == "http://localhost:11434/v1"
    assert config.api_key == "sk-test"
    assert config.model == "llama3.1"


def test_explicit_arguments_override_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://from-env.example/v1")
    monkeypatch.setenv("LLM_MODEL", "from-env")

    config = llm.LLMConfig.from_env(model="from-flag", base_url="https://flag/v1")
    assert config.model == "from-flag"
    assert config.base_url == "https://flag/v1"


def test_a_local_endpoint_needs_no_api_key(monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:1234/v1")

    assert llm.LLMConfig.from_env().is_configured


def test_unconfigured_endpoint_is_not_called(monkeypatch) -> None:
    def explode(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("the openai client was constructed without a config")

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    result = llm.complete(llm.LLMConfig.from_env(), "system", "user")
    assert not result.ok
    assert "no LLM endpoint configured" in result.error


def test_endpoint_failure_is_returned_not_raised(monkeypatch) -> None:
    """A timeout or 500 from the gateway must degrade, never crash a run."""
    import sys
    import types

    module = types.ModuleType("openai")

    class BrokenClient:
        def __init__(self, **kwargs):
            raise RuntimeError("connection refused")

    module.OpenAI = BrokenClient
    monkeypatch.setitem(sys.modules, "openai", module)

    result = llm.complete(
        llm.LLMConfig(api_key="k", base_url="http://nowhere/v1", model="m"),
        "system",
        "user",
    )
    assert not result.ok
    assert "connection refused" in result.error


def test_draft_polish_keeps_the_deterministic_text_on_failure() -> None:
    from ar_collections_agent.drafting import Draft, polish_with_llm

    draft = Draft(subject="s", body="Invoice INV-2001 for $1,000.00", engine="template")
    assert polish_with_llm(draft, llm.LLMConfig()) == draft


def test_draft_polish_rejects_a_rewrite_that_changes_figures(monkeypatch) -> None:
    from ar_collections_agent import drafting

    draft = drafting.Draft(
        subject="s", body="Invoice INV-2001 for $1,000.00 is 5 days late.", engine="template"
    )
    monkeypatch.setattr(
        drafting.llm,
        "complete",
        lambda *a, **k: llm.LLMResult(text="Invoice INV-2001 for $9,999.00 is late."),
    )
    assert drafting.polish_with_llm(draft, llm.LLMConfig(api_key="k")) == draft


def test_draft_polish_accepts_a_rewrite_that_preserves_figures(monkeypatch) -> None:
    from ar_collections_agent import drafting

    draft = drafting.Draft(
        subject="s", body="Invoice INV-2001 for $1,000.00 is 5 days late.", engine="template"
    )
    polished = "INV-2001, $1,000.00, is now 5 days past due."
    monkeypatch.setattr(
        drafting.llm, "complete", lambda *a, **k: llm.LLMResult(text=polished)
    )

    result = drafting.polish_with_llm(draft, llm.LLMConfig(api_key="k"))
    assert result.body == polished
    assert result.subject == "s"
    assert result.engine == "llm"


# --------------------------------------------------------------------------- #
# Date resolution is Python's job, never the model's
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,anchor,expected",
    [
        ("scheduled on 29 August", date(2026, 8, 12), date(2026, 8, 29)),
        ("we will pay 2026-09-15", date(2026, 8, 12), date(2026, 9, 15)),
        ("the balance on the 30th", date(2026, 8, 21), date(2026, 8, 30)),
        ("the balance on the 5th", date(2026, 8, 21), date(2026, 9, 5)),
        ("50% this Friday", date(2026, 8, 21), date(2026, 8, 21)),
        ("payment next Friday", date(2026, 8, 21), date(2026, 8, 28)),
        ("out of office until 1 September", date(2026, 8, 18), date(2026, 9, 1)),
    ],
)
def test_relative_dates_resolve_against_the_email_header(
    text: str, anchor: date, expected: date
) -> None:
    assert expected in resolve_dates(text, anchor)


def test_no_date_means_no_promise(ledger, policy) -> None:
    """A promise we cannot pin to a date must not silently pause collections."""
    classifier = EmailClassifier(ledger, policy)
    email = InboundEmail(
        source_file="synthetic.txt",
        sender_email="dana.reyes@halvorsen.com",
        received_date=date(2026, 8, 20),
        subject="RE: Invoice INV-2033",
        body="We will pay this soon, don't worry.",
    )

    result = classifier.classify(email)
    assert result.intent is Intent.UNKNOWN
    assert result.promised_payment_date is None


# --------------------------------------------------------------------------- #
# Cache round-trip: this is what makes an LLM-assisted run reproducible
# --------------------------------------------------------------------------- #


def test_cache_round_trip_preserves_classifications(classifications, tmp_path) -> None:
    path = tmp_path / "reply_classifications.json"
    write_cache(path, classifications)
    restored = {c.source_file: c for c in read_cache(path)}

    for original in classifications:
        copy = restored[original.source_file]
        assert copy.intent is original.intent
        assert copy.promised_payment_date == original.promised_payment_date
        assert copy.defer_until == original.defer_until
        assert copy.disputed_amount == original.disputed_amount
        assert copy.resolved_invoice_id == original.resolved_invoice_id
        assert copy.engine == original.engine


def test_load_or_classify_uses_the_cache(ledger, policy, tmp_path, monkeypatch) -> None:
    data_dir = tmp_path
    first = load_or_classify(ledger, policy, data_dir)
    assert (data_dir / "reply_classifications.json").is_file()

    def explode(self, emails):  # pragma: no cover - must not be reached
        raise AssertionError("classifier ran despite a complete cache")

    monkeypatch.setattr(EmailClassifier, "classify_all", explode)
    second = load_or_classify(ledger, policy, data_dir)
    assert [c.intent for c in second] == [c.intent for c in first]
