# Dry-run replay summary

Simulated 532 days, 2025-03-13 to 2026-08-26. Nothing was sent; every row below is an action the agent would have taken.

- **469 actions**: 393 auto-send, 76 held for human sign-off
- 466 state events (holds, payments, bounces, data quality)
- 2 replies needing a human to resolve a reference

## Actions by tier

| Tier | Actions |
| --- | ---: |
| T1 Operational nudge | 282 |
| T1 internal / Operational nudge | 26 |
| T2 Firm reminder, controller visible | 116 |
| T2 internal / Firm reminder, controller visible | 5 |
| T3 Formal demand to controller | 33 |
| T4 Executive escalation | 7 |

## Why actions were held

| Reason | Actions |
| --- | ---: |
| AWAITING_HUMAN_SIGNOFF | 45 |
| DE_MINIMIS | 15 |
| PAYMENT_IN_FLIGHT | 6 |
| LEGAL_HOLD | 4 |
| DISPUTE | 2 |
| RELATIONSHIP_RISK | 2 |
| PAYMENT_PLAN_PENDING | 1 |
| PROMISE_TO_PAY | 1 |

## Actions by customer

| Customer | Actions | Auto-sent | Held | Highest tier |
| --- | ---: | ---: | ---: | --- |
| Thackeray Advisory | 111 | 105 | 6 | T3 Formal demand to controller |
| Cormack Retail Group | 86 | 84 | 2 | T2 Firm reminder, controller visible |
| Woodvale Systems | 77 | 72 | 5 | T3 Formal demand to controller |
| Perrin Life Sciences | 58 | 36 | 22 | T4 Executive escalation |
| Ardley & Sons | 47 | 30 | 17 | T1 Operational nudge |
| Vantage Metalworks | 43 | 26 | 17 | T4 Executive escalation |
| Saltmarsh Media | 36 | 29 | 7 | T2 Firm reminder, controller visible |
| Kesteven Industrial | 11 | 11 | 0 | T2 Firm reminder, controller visible |

## Reply handling, 2026-08-12 to 2026-08-26

| Date | Customer | Invoice | Event | Detail |
| --- | --- | --- | --- | --- |
| 2026-08-12 | C-03 | INV-2121 | REPLY_RECEIVED | 15_reply.txt -> PROMISE_TO_PAY (conf 0.97, engine llm) |
| 2026-08-12 | C-03 | INV-2121 | HOLD_SET | PROMISE_TO_PAY until 2026-08-31 - promised 2026-08-29 (+2d grace) |
| 2026-08-13 | C-05 | INV-2178 | REPLY_RECEIVED | 14_reply.txt -> PAYMENT_PLAN_REQUEST (conf 0.97, engine llm) |
| 2026-08-13 | C-05 | INV-2178 | HOLD_SET | PAYMENT_PLAN_PENDING until 2026-08-23 - customer requested a payment plan; agreement needs a human |
| 2026-08-14 | C-04 | INV-2162 | REPLY_RECEIVED | 06_reply.txt -> CONTACT_CHANGE (conf 0.95, engine llm) |
| 2026-08-14 | C-04 | INV-2162 | CONTACT_CHANGE_PROPOSED | inbound mail asks us to use ap-team@vantage.com; NOT applied automatically - a new remit-to contact is a human decision; old address marked undeliverable |
| 2026-08-15 | C-11 | INV-2377 | REPLY_RECEIVED | 09_reply.txt -> BOUNCE (conf 0.99, engine llm) |
| 2026-08-15 | C-11 | INV-2377 | ADDRESS_UNDELIVERABLE | sam.ito@ingleby.com hard-bounced; future contact reroutes to controller |
| 2026-08-16 | C-08 | INV-2287 | REPLY_RECEIVED | 10_reply.txt -> AUTO_TICKET (conf 0.97, engine llm) |
| 2026-08-16 | C-08 | INV-2287 | HOLD_SET | PORTAL_SLA until 2026-08-28 - 10 business days |
| 2026-08-17 | C-10 | INV-2356 | REPLY_RECEIVED | 04_reply.txt -> DISPUTE (conf 0.95, engine llm) |
| 2026-08-17 | C-10 | INV-2356 | HOLD_SET | DISPUTE until 2026-08-27 - customer disputes whole balance; chasing suspended pending review |
| 2026-08-17 | C-04 | INV-2161 | REPLY_RECEIVED | 20_reply.txt -> PO_MISMATCH (conf 0.98, engine llm) |
| 2026-08-17 | C-04 | INV-2161 | HOLD_DEFERRED | REISSUE_REQUIRED: INV-2161 is not issued until 2026-08-23; hold parked until then |
| 2026-08-18 | C-09 | INV-2324 | REPLY_RECEIVED | 01_reply.txt -> OUT_OF_OFFICE (conf 0.99, engine llm) |
| 2026-08-18 | C-09 | INV-2324 | HOLD_SET | OOO_DEFER until 2026-09-01 - contact away until 2026-09-01 |
| 2026-08-18 | C-02 | INV-2085 | REPLY_RECEIVED | 13_reply.txt -> INVOICE_NOT_RECEIVED (conf 0.75, engine llm) |
| 2026-08-18 | C-02 | INV-2085 | HOLD_SET | REISSUE_REQUIRED until 2026-08-25 - invoice cannot be processed as issued (PO/delivery problem); reissue is our action, not theirs |
| 2026-08-19 | C-06 | INV-2231 | REPLY_RECEIVED | 02_reply.txt -> CLAIMS_ALREADY_PAID (conf 0.97, engine llm) |
| 2026-08-19 | C-06 | INV-2231 | REPLY_ALREADY_RECONCILED | PAYMENT_IN_FLIGHT not applied: invoice was settled on 2026-08-11; the payment ledger already agrees with the customer |
| 2026-08-19 | C-12 | INV-2431 | REPLY_RECEIVED | 08_reply.txt -> ACKNOWLEDGEMENT (conf 0.92, engine llm) |
| 2026-08-19 | C-12 | INV-2431 | ACKNOWLEDGED | customer acknowledged without committing; cadence clock reset |
| 2026-08-19 | C-06 | INV-2229 | REPLY_RECEIVED | 16_reply.txt -> INFO_REQUEST (conf 0.95, engine llm) |
| 2026-08-19 | C-06 | INV-2229 | HOLD_SET | INFO_REQUEST until 2026-08-24 - customer asked us for information before paying |
| 2026-08-20 | C-02 | INV-2087 | REPLY_RECEIVED | 03_reply.txt -> CLAIMS_ALREADY_PAID (conf 0.95, engine llm) |
| 2026-08-20 | C-02 | INV-2087 | HOLD_SET | PAYMENT_IN_FLIGHT until 2026-08-25 - customer states payment made/sent; awaiting bank reconciliation. |
| 2026-08-20 | C-01 | INV-2033 | REPLY_RECEIVED | 12_reply.txt -> UNRECOGNIZED_INVOICE (conf 0.92, engine llm) |
| 2026-08-20 | C-01 | INV-2033 | HOLD_SET | REISSUE_REQUIRED until 2026-08-27 - invoice cannot be processed as issued (PO/delivery problem); reissue is our action, not theirs |
| 2026-08-20 | C-10 | INV-2357 | REPLY_RECEIVED | 17_reply.txt -> INVOICE_NOT_RECEIVED (conf 0.85, engine llm) |
| 2026-08-20 | C-10 | INV-2357 | HOLD_SET | REISSUE_REQUIRED until 2026-08-27 - invoice cannot be processed as issued (PO/delivery problem); reissue is our action, not theirs |
| 2026-08-21 | C-07 | INV-2267 | REPLY_RECEIVED | 05_reply.txt -> PAYMENT_PLAN_REQUEST (conf 0.90, engine llm) |
| 2026-08-21 | C-07 | INV-2267 | HOLD_SET | PAYMENT_PLAN_PENDING until 2026-08-31 - customer requested a payment plan; agreement needs a human |
| 2026-08-21 | C-03 | INV-2122 | REPLY_RECEIVED | 11_reply.txt -> LEGAL_THREAT (conf 1.00, engine override) |
| 2026-08-21 | C-03 | INV-2122 | LEGAL_LOCK | deterministic keyword override; every invoice on this account is frozen until a human releases it |
| 2026-08-21 | C-03 | INV-2122 | HOLD_SET | LEGAL_HOLD until human release - rules=DISPUTE llm=LEGAL_THREAT; llm label used. legal keyword 'legal counsel' detected; deterministic override locked the account. |
| 2026-08-22 | C-05 | INV-2177 | REPLY_RECEIVED | 07_reply.txt -> RELATIONSHIP_RISK (conf 0.95, engine llm) |
| 2026-08-22 | C-05 | INV-2177 | RELATIONSHIP_RISK | customer threatened the relationship; automated chasing frozen for 30d and handed to the account director |
| 2026-08-22 | C-07 | INV-2268 | REPLY_RECEIVED | 18_reply.txt -> UNKNOWN (conf 0.82, engine llm) |
| 2026-08-22 | C-07 | INV-2268 | HOLD_SET | UNREADABLE_REPLY until 2026-08-25 - reply could not be classified with confidence (0.82); a human should read it |
| 2026-08-23 | C-04 | INV-2161 | HOLD_SET | REISSUE_REQUIRED until 2026-08-30 - deferred from the reply received 2026-08-17, which predated the invoice |
| 2026-08-24 | C-12 | INV-2430 | REPLY_RECEIVED | 19_reply.txt -> REMITTANCE_ADVICE (conf 0.99, engine llm) |
| 2026-08-24 | C-12 | INV-2430 | HOLD_SET | PAYMENT_IN_FLIGHT until 2026-08-29 - customer states payment made/sent; awaiting bank reconciliation. customer states payment made/sent 2026-08-24; requires reconciliation. |
| 2026-08-26 | C-06 | INV-2231 | DATA_QUALITY | accounting export says status=open, payments say paid; the agent used the payment ledger |

## Where to look

- `dry_run_replay_log.csv` - every action with its full drafted message body
- `state_events.csv` - the decisions behind those actions
- `unmatched_replies.csv` - references the agent refused to guess at
