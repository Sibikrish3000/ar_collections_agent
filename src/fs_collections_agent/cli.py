"""Single CLI entry point.

    ar-collections-agent                 # classify -> replay -> risk
    ar-collections-agent replay
    ar-collections-agent classify [--llm]
    ar-collections-agent risk

Nothing here sends email; there is no SMTP client anywhere in this package. The
only writes are inside ``--out-dir`` (plus the reply-classification cache in
``--data-dir``).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from . import email_classifier, llm, replay_engine, risk_analyzer
from .config import DEFAULT_CONFIG_PATH, Policy, PolicyError
from .ledger import AsOfLedger

DEFAULT_DATA_DIR = Path("data")
DEFAULT_OUT_DIR = Path("output")


def _repo_root() -> Path:
    """Project root, so the CLI works from anywhere (installed or in-tree)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config" / "escalation_policy.yaml").is_file():
            return parent
    return Path.cwd()


def _resolve(path: Path, root: Path) -> Path:
    return path if path.is_absolute() else (root / path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ar-collections-agent",
        description=(
            "Deterministic AR collections agent: dry-run replay of the escalation "
            "policy over the full invoice history, plus open-invoice risk flagging."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["all", "replay", "classify", "risk"],
        help="what to run (default: all)",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="ledger date for the risk report and the last simulated day "
        "(default: policy simulation.end_date)",
    )
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=None,
        help="first simulated day (default: policy simulation.start_date)",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="use an LLM for reply intent labelling; results are cached so later "
        "runs stay reproducible without an endpoint",
    )
    parser.add_argument(
        "--llm-drafts",
        action="store_true",
        help="let an LLM reword outbound draft bodies (figures are re-checked)",
    )
    parser.add_argument(
        "--llm-base-url",
        default=None,
        metavar="URL",
        help="OpenAI-compatible endpoint, e.g. https://gateway.example/v1 "
        "(default: $LLM_BASE_URL, else $OPENAI_BASE_URL, else the official API)",
    )
    parser.add_argument(
        "--llm-model",
        default=None,
        metavar="NAME",
        help=f"model name to send (default: $LLM_MODEL, else $OPENAI_MODEL, else "
        f"{llm.DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--refresh-classifications",
        action="store_true",
        help="ignore the cached classifications and re-run the classifier",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress the console summary"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = _repo_root()
    config_path = _resolve(args.config, root)
    data_dir = _resolve(args.data_dir, root)
    out_dir = _resolve(args.out_dir, root)

    try:
        policy = Policy.load(config_path)
    except PolicyError as exc:
        print(f"policy error: {exc}", file=sys.stderr)
        return 2

    if not (data_dir / "invoices.csv").is_file():
        print(f"no data pack found in {data_dir}", file=sys.stderr)
        return 2

    ledger = AsOfLedger.from_data_dir(data_dir)
    _, cfg_end = policy.simulation_window()
    as_of = args.as_of or cfg_end or ledger.date_bounds()[1]
    say = (lambda *a: None) if args.quiet else print

    say(f"policy      : {config_path.relative_to(root)} ({policy.policy_name})")
    say(
        f"ledger      : {len(ledger.all_invoices)} invoices, "
        f"{len(ledger.customers)} customers, {len(ledger.all_emails)} inbound replies"
    )
    say(f"as of       : {as_of.isoformat()}")

    llm_config = llm.LLMConfig.from_env(
        model=args.llm_model, base_url=args.llm_base_url
    )
    if args.llm or args.llm_drafts:
        say(f"llm         : {llm_config.describe()}")
        if not llm_config.is_configured:
            print(f"warning: {llm_config.missing_reason()}", file=sys.stderr)
            print(
                "         continuing with the deterministic rule engine",
                file=sys.stderr,
            )

    # -- classify ----------------------------------------------------------- #
    classifications = email_classifier.load_or_classify(
        ledger,
        policy,
        data_dir,
        use_llm=args.llm,
        refresh=args.refresh_classifications or args.llm,
        llm_config=llm_config,
    )
    if args.command in ("all", "classify"):
        _print_classifications(say, classifications, data_dir / email_classifier.CACHE_FILENAME, root)
    if args.command == "classify":
        return 0

    # -- replay ------------------------------------------------------------- #
    result = None
    if args.command in ("all", "replay"):
        result = replay_engine.simulate(
            ledger,
            policy,
            classifications,
            start=args.start,
            end=as_of,
            polish_with_llm=args.llm_drafts,
            llm_config=llm_config,
        )
        paths = replay_engine.write_outputs(result, out_dir)
        _print_replay(say, result, paths, root)
    if args.command == "replay":
        return 0

    # -- risk --------------------------------------------------------------- #
    analyzer = risk_analyzer.RiskAnalyzer(ledger, policy, classifications, result)
    assessments = analyzer.assess(as_of)
    risk_paths = risk_analyzer.write_outputs(assessments, out_dir, as_of)
    _print_risk(say, assessments, risk_paths, root)
    return 0


# --------------------------------------------------------------------------- #
# Console output
# --------------------------------------------------------------------------- #


def _print_classifications(say, classifications, cache_path: Path, root: Path) -> None:
    counts: dict[str, int] = {}
    engines: dict[str, int] = {}
    for c in classifications:
        counts[str(c.intent)] = counts.get(str(c.intent), 0) + 1
        engines[c.engine] = engines.get(c.engine, 0) + 1
    say("")
    say(f"--- inbound replies ({len(classifications)}) ---")
    for intent, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        say(f"  {count:>3}  {intent}")
    say(f"  engines: {dict(sorted(engines.items()))}")
    say(f"  cached  : {_rel(cache_path, root)}")


def _print_replay(say, result, paths: dict[str, Path], root: Path) -> None:
    say("")
    say(
        f"--- dry-run replay: {result.start_date} -> {result.end_date} "
        f"({result.days_simulated} days) ---"
    )
    say(f"  actions logged      : {len(result.actions)}")
    say(f"    AUTO_SEND         : {result.auto_sends}")
    say(f"    HELD_FOR_APPROVAL : {result.held}")
    say(f"  state events        : {len(result.events)}")
    say(f"  unmatched replies   : {len(result.unmatched)}")
    say("  by tier:")
    for tier, count in result.by_tier().items():
        say(f"    {count:>4}  {tier}")
    holds = result.by_hold_reason()
    if holds:
        say("  held/blocked by reason:")
        for reason, count in holds.items():
            say(f"    {count:>4}  {reason}")
    for name, path in paths.items():
        say(f"  wrote {name:<18} {_rel(path, root)}")


def _print_risk(say, assessments, paths: dict[str, Path], root: Path) -> None:
    total = sum((a.balance for a in assessments), start=Decimal("0"))
    say("")
    say(f"--- open invoice risk ({len(assessments)} invoices, {total:,.2f}) ---")
    say(risk_analyzer.format_table(assessments))
    say("")
    for band in ("High", "Medium", "Low"):
        rows = [a for a in assessments if a.risk_band == band]
        if rows:
            exposure = sum((a.balance for a in rows), start=Decimal("0"))
            say(f"  {band:<6} {len(rows):>3} invoices  {exposure:>12,.2f}")
    say("")
    say("  top reasons:")
    for a in [x for x in assessments if x.risk_band == "High"][:5]:
        say(f"    {a.invoice_id}: {a.reason}")
    for name, path in paths.items():
        say(f"  wrote {name:<18} {_rel(path, root)}")


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
