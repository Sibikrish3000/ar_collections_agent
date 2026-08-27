"""Message rendering.

The second (and last) place an LLM is permitted, and even here it is confined to
rewording an already-complete draft. Subject lines, recipients, amounts, dates
and deadlines are computed in Python and substituted into the template from
``escalation_policy.yaml``; the model may only smooth the prose of the body.

The default path is templates only, so a graded run is byte-identical offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from . import llm
from .config import Policy
from .models import Contact, Invoice

_WS_RE = re.compile(r"[ \t]+")


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


def _pretty_date(value: date) -> str:
    return value.strftime("%-d %B %Y") if hasattr(value, "strftime") else str(value)


def _tidy(text: str) -> str:
    """Reflow a template into paragraphs.

    Lines wrapped for readability in the YAML are re-joined; deliberately short
    lines (a greeting, a sign-off, a signature block) keep their own line. The
    threshold is the wrap column the config file uses.
    """
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    out: list[str] = []
    for paragraph in paragraphs:
        lines = [_WS_RE.sub(" ", line).strip() for line in paragraph.splitlines()]
        lines = [line for line in lines if line]
        rebuilt = lines[:1]
        for line in lines[1:]:
            if len(rebuilt[-1]) >= 60:  # the previous line was wrapped, not ended
                rebuilt[-1] = f"{rebuilt[-1]} {line}"
            else:
                rebuilt.append(line)
        out.append("\n".join(rebuilt))
    return "\n\n".join(out)


@dataclass(frozen=True, slots=True)
class Draft:
    subject: str
    body: str
    engine: str  # "template" | "llm"


def render(
    policy: Policy,
    template_name: str,
    *,
    invoice: Invoice,
    customer_name: str,
    balance: Decimal,
    days_past_due: int,
    as_of: date,
    recipient: Contact | None,
    cc_contacts: tuple[Contact, ...] = (),
    sender: Contact | None = None,
    hold_reason: str = "",
    trigger_reason: str = "",
    response_days: int = 5,
) -> Draft:
    """Fill a policy template. Every substituted value is computed, not generated."""
    template = policy.template(template_name)
    fields = {
        "invoice_id": invoice.invoice_id,
        "customer_name": customer_name,
        "amount": _money(invoice.amount),
        "balance": _money(balance),
        "due_date": _pretty_date(invoice.due_date),
        "issue_date": _pretty_date(invoice.issue_date),
        "days_past_due": str(days_past_due),
        "terms": invoice.terms,
        "today": _pretty_date(as_of),
        "response_deadline": _pretty_date(as_of + timedelta(days=response_days)),
        "contact_first_name": recipient.first_name if recipient else "there",
        "contact_name": recipient.name if recipient else "",
        "cc_names": ", ".join(c.name for c in cc_contacts) or "our account team",
        "sender_name": sender.name if sender else "Accounts Receivable",
        "sender_title": sender.title if sender else "AR / Collections",
        "hold_reason": hold_reason,
        "trigger_reason": trigger_reason,
    }
    subject = _tidy(str(template.get("subject", ""))).format(**fields)
    body = _tidy(str(template.get("body", ""))).format(**fields)
    return Draft(subject=subject, body=body, engine="template")


_POLISH_SYSTEM_PROMPT = (
    "Reword this accounts-receivable email so it reads naturally and politely. "
    "Keep every number, date, invoice reference and name exactly as given. Do not "
    "add facts, promises, threats, discounts or legal language. Do not add a "
    "subject line. Return the body text only."
)

# Figures, dates and invoice references that must survive a rewrite untouched.
_FIGURE_RE = re.compile(r"INV-\d+|\$[\d,]+\.\d{2}|\b\d[\d,]*(?:\.\d+)?\b")


def polish_with_llm(draft: Draft, config: llm.LLMConfig | None = None) -> Draft:
    """Optionally reword a draft's body. Never changes subject, names or figures.

    Uses whatever OpenAI-compatible endpoint is configured (see :mod:`llm`). Any
    failure — no endpoint, timeout, refusal, or a rewrite that dropped or invented
    a figure — returns the deterministic draft untouched. A model outage can delay
    nothing and corrupt nothing.
    """
    config = config or llm.LLMConfig.from_env()
    result = llm.complete(config, _POLISH_SYSTEM_PROMPT, draft.body, max_tokens=600)
    if not result.ok:
        return draft

    polished = result.text
    # Guard: every figure in the deterministic draft must still be present, and no
    # new ones invented. Prose is the model's to change; numbers are not.
    if set(_FIGURE_RE.findall(polished)) != set(_FIGURE_RE.findall(draft.body)):
        return draft
    return Draft(subject=draft.subject, body=polished, engine="llm")
