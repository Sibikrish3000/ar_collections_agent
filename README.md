# AR Collections Agent

A deterministic accounts-receivable collections agent. It reads the invoice, payment,
contact and inbound-email data in `data/`, decides who to chase and when according to
`config/escalation_policy.yaml`, and replays that policy day by day across the full
18-month history — writing every action it *would* have taken to `output/` instead of
sending anything.

Nothing here can email a customer: there is no SMTP client in the codebase.

## Run it

Requires Python 3.13+. With [uv](https://docs.astral.sh/uv/):

```bash
uv sync --group dev
uv run ar-collections-agent          # classify replies -> replay -> risk report
uv run pytest -q                     # 143 tests
```

With plain pip:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .                     # or: pip install -r requirements.txt
ar-collections-agent                 # or: PYTHONPATH=src python -m ar_collections_agent
```

Subcommands and options:

```bash
ar-collections-agent replay --as-of 2026-08-26
ar-collections-agent classify --llm  # optional LLM intent pass
ar-collections-agent risk
ar-collections-agent --help
```

## Optional LLM endpoint

The default run uses no model at all. `--llm` (reply intent labelling) and
`--llm-drafts` (rewording draft bodies) talk to **any OpenAI-compatible endpoint** —
the official API, a gateway, OpenRouter, vLLM, Ollama, LM Studio:

```bash
pip install 'ar-collections-agent[llm]'      # or: uv sync --extra llm

export LLM_BASE_URL=https://my-gateway.example/v1
export LLM_API_KEY=sk-...
export LLM_MODEL=my-deployed-model
ar-collections-agent classify --llm
```

`OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` work as fallbacks, and
`--llm-base-url` / `--llm-model` override the environment per run (the API key is
env-only, never a CLI argument, so it stays out of shell history). A local endpoint
that ignores auth needs only `LLM_BASE_URL`. Requests go out at `temperature=0`.

If the endpoint is unset, unreachable, slow, or answers with something that is not
one of our labels, the run continues on the rule engine and says so — a model outage
cannot stop a collections run. Classifications are cached to
`data/reply_classifications.json`, so an LLM-assisted run stays reproducible for
anyone without a key.

## What it writes

| File | What it is |
| --- | --- |
| `output/dry_run_replay_log.csv` | **The main artifact.** One row per action, with the eight required columns first (`date, invoice_id, customer_name, recipient_email, recipient_tier, action_type, hold_reason, message_body`) then the context that explains it: days past due, effective days past due, balance, template, trigger reason, reminder sequence. |
| `output/replay_summary.md` | Start here. Counts by tier, why actions were held, per-customer totals, and the reply-handling timeline for the final fortnight (where every inbound email lands). |
| `output/state_events.csv` | Every non-message decision: holds set/expired, promises broken, partial payments, bounces, legal locks, ledger discrepancies. Read this to explain any row in the log. |
| `output/risk_report.md` / `.csv` | Open-invoice late-payment risk as of the ledger date, with a plain-English reason per invoice and an exposure-weighted watchlist. |
| `output/unmatched_replies.csv` | Replies naming invoice IDs we never issued, or senders we cannot place — queued for a human. |
| `data/reply_classifications.json` | Cached intent classifications, committed so an LLM-assisted run reproduces exactly without a key. |

## How it is put together

```
config/escalation_policy.yaml   tiers, timings, holds, guardrails, risk weights
src/ar_collections_agent/
  ledger.py            as-of ledger - the single gate that makes future leakage impossible
  models.py            typed domain state (Decimal money, date-only arithmetic)
  email_classifier.py  inbound reply -> typed intent (rules first; optional LLM; legal override)
  policy_engine.py     pure state machine: holds -> tier -> cadence -> recipients -> guardrails
  replay_engine.py     day-by-day simulation, 2025-03-13 -> 2026-08-26
  risk_analyzer.py     statistical risk scoring for open invoices
  drafting.py          template rendering (optional LLM rewording, figures re-verified)
  llm.py               the only module that talks to a model: OpenAI-compatible, lazy, fail-soft
  cli.py               single entry point
```

The design rule: **an LLM may label text and reword prose; it may never decide who is
contacted, when, or for how much.** See [NOTES.md](NOTES.md) for the policy rationale,
the automation boundaries, and what must be true before this touches a real customer.

[THOUGHT_EXERCISE.md](THOUGHT_EXERCISE.md) answers Part 2.
