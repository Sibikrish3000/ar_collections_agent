# NOTES

## Why the policy is shaped this way

Four tiers, on **3 / 14 / 30 / 45** days past due, because the reason an invoice is
late changes as it ages. At 3 days it is almost always an AP processing slip, so the
agent nudges the person who keys the payment. At 14 days it is no longer a slip, so
the customer's controller is copied — same ask, more accountability. At 30 days the
ask moves *off* the clerk and onto the budget holder, and our own account director
is looped in. Only at 45 days does a customer's CEO or owner hear from us, and that
is a relationship event, not a collections step — a human sends it or nobody does.

The ladder alone gets this ledger wrong in three specific ways, so the policy adds
three judgment rules, all configurable:

- **Cost to collect.** Ardley & Sons runs ~$300 invoices at a median 69 days late.
  Reply 07 is a nine-year customer threatening to leave over a **$314** chase. Any
  balance under the configured floor is capped at Tier 1 and handed to the account
  director for a statement conversation. Escalating a $314 invoice to a CEO is a
  policy bug, not diligence.
- **Predictable lateness is not distress.** Cormack pays 100% of 48 invoices late by
  a median 25 days, standard deviation 4. That is their payment rhythm. The agent
  grants grace equal to the customer's trailing median lateness (capped at 15 days,
  and only when variance is low and there are ≥3 settled invoices), so Cormack gets
  nudges instead of formal demands, while erratic payers like Perrin and Vantage —
  same average, four times the variance — get no grace at all and do reach Tier 3/4.
- **Volume discipline.** One email per customer per day (largest balance first), one
  reminder per invoice per 7 days, two per tier, weekdays only. Without this, a
  customer with six overdue invoices receives six emails and stops reading all of them.

Holds are ranked, not additive, and the highest-precedence active hold wins:
`LEGAL → DISPUTE → PAYMENT_IN_FLIGHT → PROMISE_TO_PAY → PLAN_PENDING → REISSUE →
INFO_REQUEST → PORTAL_SLA → RELATIONSHIP_RISK → DE_MINIMIS → OOO`. A hold suppresses
chasing and produces exactly **one** internal handover note per tier — a hold that
re-notifies weekly buries the humans it is meant to inform.

## What the agent may do alone, and what it may not

**Alone:** Tier 1 and Tier 2 reminders to the AP contact (controller copied at Tier 2),
to addresses already on the account. Reading inbound replies, setting and expiring
holds, reconciling balances, re-escalating when a promise lapses.

**Never without a human:** anything to a customer CEO or owner; Tier 3+ wording;
replying to a dispute or to legal language; agreeing a payment plan; emailing an
address that arrived in an inbound email (a changed remit-to contact is a fraud
vector, so reply 06's new address is logged and *not* used); more than three
auto-sends to one customer in a week; contacting anyone after a complaint about
chasing. These are asserted in code, not just configured — a policy file cannot
switch them off.

Where I drew the line: the agent may decide **timing and routing within a known
contact set**, and may never make a **commercial or relational commitment**.

## What must be true before this emails a real customer

1. A shadow period: run live for 4+ weeks with every send queued for review, and
   compare what a human would have done against the log.
2. Real transport: SMTP with a verified sending domain, DKIM/SPF, a suppression list,
   bounce and complaint webhooks, and per-tenant rate limits. There is deliberately
   **no SMTP code in this repo** — it cannot email anyone by accident.
3. Real inbound: IMAP/webhook ingestion with message-id idempotency, threading, and
   attachment handling (reply 19's remittance advice is a PDF in the real world).
4. Durable state: today's holds and counters live in memory for one run. In
   production they belong in a database with transactional writes, an append-only
   audit trail of every decision, and an outbox with exactly-once send semantics.
5. Ledger trust: `INV-2231` is exported as `open` yet was paid in full on 11 August.
   The agent already recomputes balances from payments and reports the discrepancy;
   production needs that reconciliation on a schedule, plus a stop-the-line alert.
6. Human ownership: a named approver per tier, an SLA on the approval queue, and a
   kill switch that halts all sending for an account, a customer, or the tenant.

## AI usage

**AI Usage:** Used strictly for parsing unstructured inbound customer emails into
typed JSON intents and drafting candidate email strings. Both are isolated functions
with deterministic fallbacks; the default run uses no model at all, so the replay log
in `output/` reproduces byte-for-byte without an API key. Model access is an
OpenAI-compatible endpoint configured by environment variable (`LLM_BASE_URL`,
`LLM_API_KEY`, `LLM_MODEL`) and confined to one module — no vendor SDK is imported
anywhere else, and a timeout or refusal degrades the run to rules instead of
stopping it.

**AI Override:** Hardcoded regex rules override AI intent classification whenever
legal or bankruptcy terminology is detected, instantly locking the account. The
override runs *after* the model and wins unconditionally — reply 11 is labelled
`LEGAL_THREAT` with `engine=override` even when a stubbed model insists it is a
promise to pay (`tests/test_email_classifier.py::test_legal_keywords_override_the_model`).

Two further places I overrode the AI-shaped instinct. First, **no LLM decides who is
contacted, when, or for how much** — escalation, holds and money are pure Python,
because a hallucinated tier in a financial workflow means emailing a customer's CEO
about a $314 invoice. Second, **no ML in the risk model**: with 12 customers and 432
invoices, a trailing median and a variance measure per customer beat any fitted
model, and a reviewer can check every number by hand.

## Honest limits

- Reminders were previously sent manually with no log, so the replay assumes the
  agent from day one; several replies answer reminders this agent never sent, and
  four replies concern invoices that were not yet issued on the reply's own date
  (handled explicitly: the hold is parked and applied when the invoice appears).
- The bounce-reroute path is exercised by unit tests, not by the log: `INV-2377`
  never became overdue inside the simulation window, so no rerouted send occurs.
- The risk model is calibrated on 18 months from 12 customers. It has no seasonality
  term and no macro signal, and `predicted_days_late` is a median, not a distribution.
