"""Isolated inbound-email intent parser.

This is one of exactly two places an LLM is allowed to run (the other is draft
copy in :mod:`drafting`). Everything about it is designed so that a wrong or
unavailable model degrades the system rather than breaking it:

* The **rule engine is the default and always runs**. It produces a complete
  classification on its own, so the graded replay reproduces with no API key.
* The optional LLM pass (``--llm``) may only refine the *intent label* and its
  confidence. Every date, amount, address and invoice reference is (re)computed
  in deterministic Python from the email's own ``Date:`` header — the model is
  never trusted with arithmetic.
* A deterministic legal/insolvency keyword check runs **after** the LLM and
  overrides it unconditionally (``engine="override"``).
"""

from __future__ import annotations

import calendar
import json
import re
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import llm
from .config import Policy
from .ledger import AsOfLedger
from .models import Classification, InboundEmail, Intent

CACHE_FILENAME = "reply_classifications.json"

INVOICE_REF_RE = re.compile(r"\bINV-\d{3,6}\b", re.IGNORECASE)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Only currency-shaped figures count as money: a symbol, or explicit cents.
# "112 hours" in a dispute about timesheets must not be read as $112.
MONEY_RE = re.compile(
    r"(?:[$£€]\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?))"
    r"|(?<![\w.])(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})(?![\d%])"
)

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS |= {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

# --------------------------------------------------------------------------- #
# Deterministic date resolution (never delegated to the model)
# --------------------------------------------------------------------------- #

_DAY_MONTH_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_MONTH_DAY_RE = re.compile(
    r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_ORDINAL_DAY_RE = re.compile(r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b", re.IGNORECASE)
_WEEKDAY_RE = re.compile(
    r"\b(this|next|on)?\s*(" + "|".join(WEEKDAYS) + r")\b", re.IGNORECASE
)


def _nearest_year(month: int, day: int, anchor: date) -> date | None:
    """Pick the year that puts ``month/day`` closest to (and mostly after) ``anchor``."""
    best: date | None = None
    for year in (anchor.year - 1, anchor.year, anchor.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        delta = (candidate - anchor).days
        if not -45 <= delta <= 400:
            continue
        if best is None or abs(delta - 30) < abs((best - anchor).days - 30):
            best = candidate
    return best


def resolve_dates(text: str, anchor: date) -> list[date]:
    """All dates a human would read out of ``text``, anchored on the email's date.

    Deliberately conservative: only unambiguous forms are resolved, and the
    result is ordered as encountered so callers can choose first vs. last.
    """
    found: list[tuple[int, date]] = []

    for match in _ISO_RE.finditer(text):
        year, month, day = (int(g) for g in match.groups())
        try:
            found.append((match.start(), date(year, month, day)))
        except ValueError:
            continue

    for match in _DAY_MONTH_RE.finditer(text):
        day, month_name = match.group(1), match.group(2).lower()
        resolved = _nearest_year(MONTHS[month_name], int(day), anchor)
        if resolved:
            found.append((match.start(), resolved))

    for match in _MONTH_DAY_RE.finditer(text):
        month_name, day = match.group(1).lower(), match.group(2)
        resolved = _nearest_year(MONTHS[month_name], int(day), anchor)
        if resolved:
            found.append((match.start(), resolved))

    for match in _ORDINAL_DAY_RE.finditer(text):
        day = int(match.group(1))
        # "the 30th" means the next occurrence of that day-of-month.
        for offset in (0, 1, 2):
            month = anchor.month - 1 + offset
            year = anchor.year + month // 12
            month = month % 12 + 1
            if day > calendar.monthrange(year, month)[1]:
                continue
            candidate = date(year, month, day)
            if candidate >= anchor:
                found.append((match.start(), candidate))
                break

    for match in _WEEKDAY_RE.finditer(text):
        qualifier = (match.group(1) or "").lower()
        target = WEEKDAYS[match.group(2).lower()]
        ahead = (target - anchor.weekday()) % 7
        if qualifier == "next" and ahead == 0:
            ahead = 7
        elif qualifier == "next":
            ahead += 7
        found.append((match.start(), anchor + timedelta(days=ahead)))

    seen: set[date] = set()
    ordered: list[date] = []
    for _, value in sorted(found, key=lambda pair: pair[0]):
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _first_money(text: str) -> Decimal:
    match = MONEY_RE.search(text)
    if not match:
        return Decimal("0.00")
    raw = match.group(1) or match.group(2) or ""
    try:
        return Decimal(raw.replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation:  # pragma: no cover - regex already constrains this
        return Decimal("0.00")


# --------------------------------------------------------------------------- #
# Rule engine
# --------------------------------------------------------------------------- #

# Each rule is (intent, confidence, patterns). Order is precedence: the first
# rule whose pattern matches wins. Machine-generated mail is matched before
# human intent so an auto-reply is never read as a promise.
_RULES: tuple[tuple[Intent, float, tuple[str, ...]], ...] = (
    (
        Intent.BOUNCE,
        0.99,
        (
            r"mailer-daemon",
            r"undeliverable",
            r"delivery to the following recipient failed",
            r"\b5\.\d\.\d\b",
            r"does not exist",
        ),
    ),
    (
        Intent.OUT_OF_OFFICE,
        0.97,
        (
            r"out of (the )?office",
            r"automatic reply",
            r"annual leave",
            r"on holiday",
            r"limited access to email",
        ),
    ),
    (
        Intent.AUTO_TICKET,
        0.97,
        (
            r"ticket (has been )?created",
            r"\[ticket #\d+\]",
            r"accounts payable portal",
            r"do not reply to this address",
        ),
    ),
    (
        Intent.RELATIONSHIP_RISK,
        0.93,
        (
            r"take (the )?(whole )?account elsewhere",
            r"cancel (our|the) (account|contract)",
            r"explain that to your management",
            r"do not (reply|contact) (me|us) (with|again)",
            r"stop (emailing|chasing) me",
        ),
    ),
    (
        Intent.REMITTANCE_ADVICE,
        0.95,
        (r"remittance advice", r"payment run \d{1,2}", r"total transmitted"),
    ),
    (
        Intent.CLAIMS_ALREADY_PAID,
        0.92,
        (
            r"(this|it) was (already )?paid",
            r"already (been )?(paid|settled)",
            r"we'?ve (already )?settled",
            r"nothing outstanding",
            r"paid on \d{1,2}",
        ),
    ),
    (
        Intent.PO_MISMATCH,
        0.94,
        (
            r"po (number|ref\w*) (on this invoice )?is (wrong|incorrect)",
            r"should be po[- ]?\w+",
            r"auto-?rejects? on po mismatch",
            r"po mismatch",
        ),
    ),
    (
        Intent.INVOICE_NOT_RECEIVED,
        0.94,
        (
            r"never received (this|the) invoice",
            r"did not receive (this|the) invoice",
            r"resend it",
            r"can'?t process anything without a po",
        ),
    ),
    (
        Intent.UNRECOGNIZED_INVOICE,
        0.9,
        (
            r"which invoice is this about",
            r"doesn'?t match anything in our system",
            r"we (do not|don'?t) recognis[sz]e",
            r"no record of (this|that) invoice",
        ),
    ),
    (
        Intent.PAYMENT_PLAN_REQUEST,
        0.94,
        (
            r"payment plan",
            r"instal?lments?",
            r"spread (this|the payment)",
            r"across the next \w+ months",
        ),
    ),
    (
        Intent.DISPUTE,
        0.92,
        (
            r"can'?t approve this",
            r"cannot approve this",
            r"do not accept the amounts",
            r"hours billed .* don'?t match",
            r"holding payment until this is resolved",
            r"the rate on line \d+",
            r"disputed?",
            r"query on (the|this) invoice",
        ),
    ),
    (
        Intent.PARTIAL_PROMISE,
        0.9,
        (
            r"\d{1,3}\s*% (this|next|on)",
            r"(half|50%) (now|this|next)",
            r"balance on the",
            r"part payment",
        ),
    ),
    (
        Intent.PROMISE_TO_PAY,
        0.95,
        (
            r"scheduled in our payment run",
            r"payment run on",
            r"will be paid on",
            r"we will pay",
            r"you'?ll have it",
            r"confirmed - this is scheduled",
            r"expect payment",
        ),
    ),
    (
        Intent.CONTACT_CHANGE,
        0.9,
        (
            r"has left the business",
            r"no longer (with|at) (the company|us)",
            r"send all future (invoices|correspondence)",
            r"going forward",
        ),
    ),
    (
        Intent.INFO_REQUEST,
        0.88,
        (
            r"statement of account",
            r"send (us )?a (full )?statement",
            r"forward (it|the invoice|the original)",
            r"don'?t have visibility",
            r"come back to us with a breakdown",
            r"reconcile at our end",
        ),
    ),
    (
        Intent.ACKNOWLEDGEMENT,
        0.8,
        (
            r"i'?ll look into it",
            r"will look into (it|this)",
            r"come back to you",
            r"thanks for the reminder",
            r"noted",
        ),
    ),
)

_COMPILED_RULES = tuple(
    (intent, confidence, tuple(re.compile(p, re.IGNORECASE) for p in patterns))
    for intent, confidence, patterns in _RULES
)

_FAILED_RECIPIENT_RE = re.compile(
    r"failed permanently:\s*(?:\r?\n\s*)*([\w.+-]+@[\w.-]+)", re.IGNORECASE
)
_NEW_ADDRESS_HINT_RE = re.compile(
    r"(?:to|at)\s+([\w.+-]+@[\w-]+\.[\w.-]+)\s*(?:going forward|from now|in future)?",
    re.IGNORECASE,
)


class EmailClassifier:
    """Turns raw reply text into a :class:`Classification`."""

    def __init__(
        self,
        ledger: AsOfLedger,
        policy: Policy,
        use_llm: bool = False,
        llm_config: llm.LLMConfig | None = None,
    ) -> None:
        self.ledger = ledger
        self.policy = policy
        self.use_llm = use_llm
        self.llm_config = llm_config or llm.LLMConfig.from_env()
        self._legal_keywords = policy.legal_keywords()

    # -- public API --------------------------------------------------------- #

    def classify_all(self, emails: Iterable[InboundEmail]) -> list[Classification]:
        return [self.classify(email) for email in emails]

    def classify(self, email: InboundEmail) -> Classification:
        result = self._classify_by_rules(email)

        if self.use_llm:
            llm_intent, llm_confidence, note = self._llm_intent(email)
            if llm_intent is not None:
                if llm_intent is not result.intent:
                    result.notes = (
                        f"{result.notes} rules={result.intent} llm={llm_intent}; "
                        "llm label used."
                    ).strip()
                result.intent = llm_intent
                result.confidence_score = llm_confidence
                result.engine = "llm"
            elif note:
                result.notes = f"{result.notes} llm_unavailable: {note}".strip()

        # Deterministic override, applied last so it beats the model every time.
        self._apply_legal_override(email, result)
        self._apply_intent_side_effects(email, result)
        return result

    # -- rules -------------------------------------------------------------- #

    def _classify_by_rules(self, email: InboundEmail) -> Classification:
        text = email.full_text
        intent = Intent.UNKNOWN
        confidence = 0.35
        for candidate, candidate_confidence, patterns in _COMPILED_RULES:
            if any(p.search(text) for p in patterns):
                intent, confidence = candidate, candidate_confidence
                break

        refs = self._invoice_refs(text)
        known = tuple(r for r in refs if self.ledger.has_invoice(r))
        unknown = tuple(r for r in refs if not self.ledger.has_invoice(r))
        customer_id = self._resolve_customer(email, known)
        resolved = self._resolve_invoice(known, customer_id)

        return Classification(
            source_file=email.source_file,
            received_date=email.received_date,
            sender_email=email.sender_email,
            intent=intent,
            confidence_score=confidence,
            engine="rules",
            invoice_refs=refs,
            unmatched_refs=unknown,
            customer_id=customer_id,
            resolved_invoice_id=resolved,
        )

    def _invoice_refs(self, text: str) -> tuple[str, ...]:
        seen: list[str] = []
        for match in INVOICE_REF_RE.finditer(text):
            ref = match.group(0).upper()
            if ref not in seen:
                seen.append(ref)
        return tuple(seen)

    def _resolve_customer(
        self, email: InboundEmail, known_refs: Sequence[str]
    ) -> str | None:
        """Sender contact, then sender domain, then the invoice they quoted."""
        by_sender = self.ledger.customer_for_email(email.sender_email)
        if by_sender:
            return by_sender
        for ref in known_refs:
            return self.ledger.invoice(ref).customer_id
        return None

    def _resolve_invoice(
        self, known_refs: Sequence[str], customer_id: str | None
    ) -> str | None:
        """The invoice this reply is about: first quoted ref belonging to them."""
        for ref in known_refs:
            invoice = self.ledger.invoice(ref)
            if customer_id is None or invoice.customer_id == customer_id:
                return ref
        return known_refs[0] if known_refs else None

    def _apply_legal_override(
        self, email: InboundEmail, result: Classification
    ) -> None:
        lowered = email.full_text.lower()
        hit = next((k for k in self._legal_keywords if k in lowered), None)
        if hit is None:
            return
        result.intent = Intent.LEGAL_THREAT
        result.confidence_score = 1.0
        result.engine = "override"
        result.notes = (
            f"{result.notes} legal keyword {hit!r} detected; deterministic override "
            "locked the account."
        ).strip()

    def _apply_intent_side_effects(
        self, email: InboundEmail, result: Classification
    ) -> None:
        """Fill dates, amounts and addresses — always in Python, never via the LLM."""
        text = email.full_text
        anchor = email.received_date
        dates = resolve_dates(text, anchor)
        future_dates = [d for d in dates if d >= anchor]
        past_dates = [d for d in dates if d < anchor]

        match result.intent:
            case Intent.PROMISE_TO_PAY:
                if future_dates:
                    result.promised_payment_date = future_dates[0]
                else:
                    result.notes = (
                        f"{result.notes} promise with no resolvable date; "
                        "treated as unreadable."
                    ).strip()
                    result.intent = Intent.UNKNOWN
                    result.confidence_score = 0.4

            case Intent.PARTIAL_PROMISE:
                # The hold must run to the date the *balance* clears, not the part.
                if future_dates:
                    result.promised_payment_date = max(future_dates)
                    if len(future_dates) > 1:
                        result.notes = (
                            f"{result.notes} split payment offered: "
                            f"{future_dates[0].isoformat()} then "
                            f"{max(future_dates).isoformat()}."
                        ).strip()

            case Intent.CLAIMS_ALREADY_PAID | Intent.REMITTANCE_ADVICE:
                claimed = past_dates[-1] if past_dates else (dates[0] if dates else None)
                if claimed:
                    result.notes = (
                        f"{result.notes} customer states payment made/sent "
                        f"{claimed.isoformat()}; requires reconciliation."
                    ).strip()

            case Intent.DISPUTE:
                result.disputed_amount = _first_money(email.body)
                if result.disputed_amount == Decimal("0.00"):
                    result.notes = (
                        f"{result.notes} no figure quoted; whole balance treated "
                        "as disputed."
                    ).strip()

            case Intent.OUT_OF_OFFICE:
                if future_dates:
                    result.defer_until = future_dates[0]

            case Intent.BOUNCE:
                match = _FAILED_RECIPIENT_RE.search(text)
                if match:
                    result.new_email_address = None
                    result.notes = (
                        f"{result.notes} hard bounce for {match.group(1).lower()}."
                    ).strip()
                else:
                    addresses = [
                        a.lower()
                        for a in EMAIL_RE.findall(email.body)
                        if "mailer-daemon" not in a.lower()
                    ]
                    if addresses:
                        result.notes = (
                            f"{result.notes} hard bounce for {addresses[0]}."
                        ).strip()
                # A bounce identifies its customer by the dead address, not the sender.
                if result.customer_id is None:
                    for address in EMAIL_RE.findall(email.body):
                        found = self.ledger.customer_for_email(address)
                        if found:
                            result.customer_id = found
                            break

            case Intent.CONTACT_CHANGE:
                addresses = [
                    a.lower()
                    for a in EMAIL_RE.findall(email.body)
                    if a.lower() != email.sender_email
                ]
                hint = _NEW_ADDRESS_HINT_RE.search(email.body)
                result.new_email_address = (
                    hint.group(1).lower() if hint else (addresses[0] if addresses else None)
                )

            case Intent.AUTO_TICKET:
                result.notes = (
                    f"{result.notes} customer AP portal ticket; SLA window applies."
                ).strip()

        if result.customer_id is None:
            result.notes = (
                f"{result.notes} sender not in contacts; customer unresolved."
            ).strip()
        elif self.ledger.contact_for_email(email.sender_email) is None:
            # Resolved by email domain only: a person we have never dealt with.
            result.notes = (
                f"{result.notes} sender {email.sender_email} is not a known contact "
                f"for {result.customer_id}; matched on domain only, address unverified."
            ).strip()

    # -- optional LLM pass -------------------------------------------------- #

    _SYSTEM_PROMPT = (
        "You label inbound accounts-receivable email replies. Reply with JSON only: "
        '{"intent": <one of %s>, "confidence": <0-1 float>}. '
        "Label the sender's intent regarding payment of the invoice. Do not infer "
        "dates or amounts; another system handles those."
    )

    def _llm_intent(self, email: InboundEmail) -> tuple[Intent | None, float, str]:
        """Ask a model for the intent label only. Any failure is non-fatal.

        Returns ``(intent, confidence, note)``; ``intent`` is ``None`` whenever the
        endpoint is unreachable, unconfigured, or answers with something that is not
        one of our labels — in which case the rule engine's answer stands.
        """
        labels = ", ".join(str(i) for i in Intent)
        prompt = (
            f"From: {email.sender_email}\nDate: {email.received_date}\n"
            f"Subject: {email.subject}\n\n{email.body}"
        )
        result = llm.complete(
            self.llm_config,
            self._SYSTEM_PROMPT % labels,
            prompt,
            json_mode=True,
            max_tokens=200,
        )
        if not result.ok:
            return None, 0.0, result.error or "empty response"

        try:
            data = json.loads(llm.strip_code_fence(result.text))
            intent = Intent(str(data["intent"]).strip().upper())
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return None, 0.0, f"unusable response from {self.llm_config.describe()}: {exc}"

        try:
            confidence = float(data.get("confidence", 0.75))
        except (TypeError, ValueError):
            confidence = 0.75
        return intent, min(max(confidence, 0.0), 1.0), ""


# --------------------------------------------------------------------------- #
# Cache: lets an LLM-assisted run be replayed byte-for-byte with no API key
# --------------------------------------------------------------------------- #


def write_cache(path: Path, classifications: Sequence[Classification]) -> None:
    payload = {
        "schema": 1,
        "note": (
            "Cached inbound-reply classifications. Committed so the replay "
            "reproduces exactly without an API key."
        ),
        "classifications": [c.to_json_dict() for c in classifications],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_cache(path: Path) -> list[Classification]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[Classification] = []
    for row in data["classifications"]:
        out.append(
            Classification(
                source_file=row["source_file"],
                received_date=date.fromisoformat(row["received_date"]),
                sender_email=row["sender_email"],
                intent=Intent(row["intent"]),
                promised_payment_date=(
                    date.fromisoformat(row["promised_payment_date"])
                    if row.get("promised_payment_date")
                    else None
                ),
                defer_until=(
                    date.fromisoformat(row["defer_until"])
                    if row.get("defer_until")
                    else None
                ),
                disputed_amount=Decimal(str(row.get("disputed_amount", 0))).quantize(
                    Decimal("0.01")
                ),
                confidence_score=float(row.get("confidence_score", 0.0)),
                engine=row.get("engine", "rules"),
                invoice_refs=tuple(row.get("invoice_refs", ())),
                customer_id=row.get("customer_id"),
                resolved_invoice_id=row.get("resolved_invoice_id"),
                unmatched_refs=tuple(row.get("unmatched_refs", ())),
                new_email_address=row.get("new_email_address"),
                notes=row.get("notes", ""),
            )
        )
    return out


def load_or_classify(
    ledger: AsOfLedger,
    policy: Policy,
    data_dir: Path,
    use_llm: bool = False,
    refresh: bool = False,
    llm_config: llm.LLMConfig | None = None,
) -> list[Classification]:
    """Classifications for every reply in the pack.

    Uses the committed cache when present (so results are reproducible), unless
    ``refresh`` is set or the cache does not cover every reply file.
    """
    cache_path = data_dir / CACHE_FILENAME
    emails = ledger.all_emails
    if cache_path.is_file() and not refresh:
        cached = read_cache(cache_path)
        if {c.source_file for c in cached} == {e.source_file for e in emails}:
            return sorted(cached, key=lambda c: (c.received_date, c.source_file))

    classifier = EmailClassifier(
        ledger, policy, use_llm=use_llm, llm_config=llm_config
    )
    if use_llm and llm_config.is_configured:
        try:
            from tqdm import tqdm  # noqa: PLC0415

            emails = tqdm(
                emails, desc="classifying", unit="email", disable=False, colour="blue"
            )
        except ImportError:
            pass
    results = classifier.classify_all(emails)
    write_cache(cache_path, results)
    return results
