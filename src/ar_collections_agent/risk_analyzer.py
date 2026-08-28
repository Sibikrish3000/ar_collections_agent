"""Late-payment risk for invoices open as of the ledger date.

Deliberately statistical, not machine-learned. With twelve customers and 432
invoices, a customer's own payment history is a far stronger and far more
explainable signal than anything a model could fit here; a reviewer can check
every number in this file by hand, which is the point.

Score = weighted sum of normalised factors (weights in ``escalation_policy.yaml``):

* mean lateness and share of invoices paid late — how they behave
* volatility — how reliable that behaviour is
* current aging — where this invoice already is
* active dispute / legal hold — a known blocker
* unresponsive or undeliverable contact — nobody is reading our email
* exposure share — how much of the customer's balance sits on this invoice
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from .config import Policy
from .ledger import AsOfLedger, CustomerStats
from .models import Classification, Intent, RiskAssessment
from .replay_engine import ReplayResult

RISK_CSV_FILENAME = "risk_report.csv"
RISK_MD_FILENAME = "risk_report.md"

# Intents that mean this invoice is blocked on something, not merely late.
_BLOCKING_INTENTS: dict[Intent, str] = {
    Intent.LEGAL_THREAT: "account referred to legal counsel",
    Intent.DISPUTE: "open dispute on the invoice",
    Intent.PO_MISMATCH: "rejected on PO mismatch, needs reissue",
    Intent.INVOICE_NOT_RECEIVED: "customer says the invoice never arrived",
    Intent.UNRECOGNIZED_INVOICE: "customer cannot find the invoice in their system",
    Intent.PAYMENT_PLAN_REQUEST: "customer asked for a payment plan",
    Intent.RELATIONSHIP_RISK: "customer escalated a complaint about chasing",
    Intent.INFO_REQUEST: "customer is waiting on information from us",
    Intent.AUTO_TICKET: "sitting in the customer's AP portal queue",
    Intent.BOUNCE: "our contact address is dead",
    Intent.CONTACT_CHANGE: "AP contact has left; new address unverified",
}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass(slots=True)
class _CustomerContext:
    stats: CustomerStats
    total_open: Decimal
    replies: int
    blockers: dict[str, str]  # invoice_id -> reason
    account_blockers: list[str]
    undeliverable: bool


class RiskAnalyzer:
    def __init__(
        self,
        ledger: AsOfLedger,
        policy: Policy,
        classifications: Sequence[Classification] = (),
        replay: ReplayResult | None = None,
    ) -> None:
        self.ledger = ledger
        self.policy = policy
        self.classifications = list(classifications)
        self.replay = replay

    # -- public API --------------------------------------------------------- #

    def assess(self, as_of: date) -> list[RiskAssessment]:
        view = self.ledger.view(as_of)
        window = int(self.policy.risk_model.get("trailing_window_days", 365))
        open_invoices = view.open_invoices()

        contexts: dict[str, _CustomerContext] = {}
        for customer_id in {i.customer_id for i in open_invoices}:
            contexts[customer_id] = self._context(customer_id, view, window, as_of)

        assessments = [
            self._assess_invoice(invoice, view, contexts[invoice.customer_id], as_of)
            for invoice in open_invoices
        ]
        band_order = {"High": 0, "Medium": 1, "Low": 2}
        assessments.sort(
            key=lambda a: (band_order[a.risk_band], -a.risk_score, a.invoice_id)
        )
        return assessments

    # -- context ------------------------------------------------------------ #

    def _context(
        self, customer_id: str, view, window: int, as_of: date
    ) -> _CustomerContext:
        stats = view.customer_stats(customer_id, window)
        total_open = sum(
            (view.balance(i) for i in view.open_invoices() if i.customer_id == customer_id),
            start=Decimal("0"),
        )

        blockers: dict[str, str] = {}
        account_blockers: list[str] = []
        replies = 0
        undeliverable = False
        for c in self.classifications:
            if c.customer_id != customer_id or c.received_date > as_of:
                continue
            replies += 1
            reason = _BLOCKING_INTENTS.get(c.intent)
            if reason is None:
                continue
            if c.intent in (Intent.LEGAL_THREAT, Intent.RELATIONSHIP_RISK):
                account_blockers.append(reason)
            elif c.intent in (Intent.BOUNCE, Intent.CONTACT_CHANGE):
                undeliverable = True
                account_blockers.append(reason)
            elif c.resolved_invoice_id:
                blockers[c.resolved_invoice_id] = reason

        return _CustomerContext(
            stats=stats,
            total_open=total_open,
            replies=replies,
            blockers=blockers,
            account_blockers=account_blockers,
            undeliverable=undeliverable,
        )

    # -- scoring ------------------------------------------------------------ #

    def _assess_invoice(
        self, invoice, view, ctx: _CustomerContext, as_of: date
    ) -> RiskAssessment:
        model = self.policy.risk_model
        weights = model.get("weights", {})
        norm = model.get("normalisation", {})
        bands = model.get("bands", {})

        balance = view.balance(invoice)
        dpd = max(0, (as_of - invoice.due_date).days)
        stats = ctx.stats

        mean_cap = float(norm.get("mean_days_late_cap", 60))
        aging_cap = float(norm.get("aging_days_cap", 60))
        vol_cap = float(norm.get("volatility_cap", 25))

        blocked_reason = ctx.blockers.get(invoice.invoice_id)
        account_reason = ctx.account_blockers[0] if ctx.account_blockers else None

        factors = {
            "historical_mean_days_late": _clamp01(
                max(stats.mean_days_late, 0.0) / mean_cap
            ),
            "pct_invoices_paid_late": _clamp01(stats.pct_paid_late),
            "payment_volatility": _clamp01(stats.stdev_days_late / vol_cap),
            "current_aging": _clamp01(dpd / aging_cap),
            "active_dispute_or_legal": 1.0 if (blocked_reason or account_reason) else 0.0,
            "unresponsive_or_undeliverable": self._unresponsive_factor(ctx),
            "exposure_share": _clamp01(
                float(balance / ctx.total_open) if ctx.total_open > 0 else 0.0
            ),
        }
        score = sum(float(weights.get(k, 0.0)) * v for k, v in factors.items())
        score = _clamp01(score)

        band = (
            "High"
            if score >= float(bands.get("high", 0.55))
            else "Medium"
            if score >= float(bands.get("medium", 0.30))
            else "Low"
        )

        predicted_days_late = self._predicted_days_late(
            stats, dpd, blocked_reason or account_reason
        )
        predicted_date = max(
            invoice.due_date + timedelta(days=predicted_days_late),
            as_of + timedelta(days=1) if dpd > 0 else invoice.due_date,
        )

        return RiskAssessment(
            invoice_id=invoice.invoice_id,
            customer_id=invoice.customer_id,
            customer_name=self.ledger.customer_name(invoice.customer_id),
            due_date=invoice.due_date,
            balance=balance,
            days_past_due=dpd,
            risk_band=band,
            risk_score=score,
            predicted_days_late=predicted_days_late,
            predicted_payment_date=predicted_date,
            reason=self._reason(
                invoice,
                stats,
                dpd,
                balance,
                blocked_reason,
                account_reason,
                predicted_days_late,
                predicted_date,
                ctx,
            ),
        )

    def _unresponsive_factor(self, ctx: _CustomerContext) -> float:
        if ctx.undeliverable:
            return 1.0
        if ctx.replies == 0 and ctx.stats.pct_paid_late > 0.5:
            return 0.6
        return 0.0

    def _predicted_days_late(
        self, stats: CustomerStats, dpd: int, blocked_reason: str | None
    ) -> int:
        """Trailing median lateness, floored by where the invoice already is."""
        base = int(round(stats.median_days_late)) if stats.has_history else 0
        if blocked_reason:
            # A blocker adds at least a review cycle on top of normal behaviour.
            base = max(base, dpd) + int(
                self.policy.hold_days("dispute_review_days", 10)
            )
        return max(base, dpd + 1 if dpd > 0 else 0)

    def _reason(
        self,
        invoice,
        stats: CustomerStats,
        dpd: int,
        balance: Decimal,
        blocked_reason: str | None,
        account_reason: str | None,
        predicted_days_late: int,
        predicted_date: date,
        ctx: _CustomerContext,
    ) -> str:
        name = self.ledger.customer_name(invoice.customer_id)
        parts: list[str] = []

        if stats.has_history:
            parts.append(
                f"{name} has paid {stats.pct_paid_late:.0%} of "
                f"{stats.invoices_settled} settled invoices late, median "
                f"{stats.median_days_late:.0f}d (sd {stats.stdev_days_late:.0f}d, "
                f"worst {stats.max_days_late}d)"
            )
        else:
            parts.append(f"{name} has no settled invoices in the trailing window")

        parts.append(
            f"this invoice is {dpd}d past due with {balance:,.2f} outstanding"
            if dpd > 0
            else f"this invoice is not yet due ({invoice.due_date.isoformat()})"
        )

        if blocked_reason:
            parts.append(f"blocked: {blocked_reason}")
        if account_reason:
            parts.append(f"account-level: {account_reason}")
        if ctx.replies == 0:
            parts.append("no inbound reply on this account")
        threshold = self.policy.de_minimis_threshold()
        if threshold > 0 and balance < threshold:
            parts.append(
                f"balance is below the {threshold:,.0f} cost-to-collect floor, so it "
                "is worked by statement rather than escalation"
            )

        parts.append(
            f"expected payment around {predicted_date.isoformat()} "
            f"(~{predicted_days_late}d past terms)"
        )
        return "; ".join(parts) + "."


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def write_outputs(
    assessments: Sequence[RiskAssessment], out_dir: Path, as_of: date
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / RISK_CSV_FILENAME
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(RiskAssessment.FIELDS))
        writer.writeheader()
        for assessment in assessments:
            writer.writerow(assessment.to_row())

    md_path = out_dir / RISK_MD_FILENAME
    md_path.write_text(render_markdown(assessments, as_of), encoding="utf-8")
    return {"risk_csv": csv_path, "risk_md": md_path}


def render_markdown(assessments: Sequence[RiskAssessment], as_of: date) -> str:
    total = sum((a.balance for a in assessments), start=Decimal("0"))
    lines = [
        f"# Open invoice risk - as of {as_of.isoformat()}",
        "",
        f"{len(assessments)} open invoices, {total:,.2f} outstanding.",
        "",
    ]
    # Bands answer "will this go late?". Money answers "what should a human do
    # first?" - they are not the same question, so both are reported.
    weighted = sorted(
        assessments,
        key=lambda a: (-(a.balance * Decimal(str(round(a.risk_score, 3)))), a.invoice_id),
    )[:10]
    lines += [
        "## Watchlist - exposure weighted by risk",
        "",
        "Risk bands rank likelihood; this table ranks money. A near-certain $300 "
        "slip matters less than a $72k invoice at even odds.",
        "",
        "| Invoice | Customer | Balance | Score | Balance x score | DPD |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for a in weighted:
        lines.append(
            f"| {a.invoice_id} | {a.customer_name} | {a.balance:,.2f} "
            f"| {a.risk_score:.2f} | {float(a.balance) * a.risk_score:,.0f} "
            f"| {a.days_past_due} |"
        )
    lines.append("")

    for band in ("High", "Medium", "Low"):
        rows = [a for a in assessments if a.risk_band == band]
        if not rows:
            continue
        exposure = sum((a.balance for a in rows), start=Decimal("0"))
        lines += [
            f"## {band} risk - {len(rows)} invoices, {exposure:,.2f}",
            "",
            "| Invoice | Customer | Due | Balance | DPD | Score | Expected | Why |",
            "| --- | --- | --- | --- | ---: | ---: | --- | --- |",
        ]
        for a in rows:
            lines.append(
                f"| {a.invoice_id} | {a.customer_name} | {a.due_date.isoformat()} "
                f"| {a.balance:,.2f} | {a.days_past_due} | {a.risk_score:.2f} "
                f"| {a.predicted_payment_date.isoformat()} | {a.reason} |"
            )
        lines.append("")
    return "\n".join(lines)


def format_table(assessments: Sequence[RiskAssessment], limit: int | None = None) -> str:
    """Compact console table."""
    rows = list(assessments)[:limit] if limit else list(assessments)
    header = (
        f"{'BAND':<7}{'SCORE':>6}  {'INVOICE':<10}{'CUSTOMER':<22}"
        f"{'DUE':<12}{'BALANCE':>12}{'DPD':>5}  EXPECTED"
    )
    out = [header, "-" * len(header)]
    for a in rows:
        out.append(
            f"{a.risk_band:<7}{a.risk_score:>6.2f}  {a.invoice_id:<10}"
            f"{a.customer_name[:21]:<22}{a.due_date.isoformat():<12}"
            f"{a.balance:>12,.2f}{a.days_past_due:>5}  "
            f"{a.predicted_payment_date.isoformat()}"
        )
    return "\n".join(out)


__all__ = [
    "RISK_CSV_FILENAME",
    "RiskAnalyzer",
    "format_table",
    "render_markdown",
    "write_outputs",
]
