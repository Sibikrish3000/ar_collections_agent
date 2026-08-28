"""Deterministic accounts-receivable collections agent.

All escalation timing, state tracking, hold logic and money arithmetic is plain
Python. An LLM is optional and confined to two isolated helpers: labelling the
intent of an inbound reply (:mod:`email_classifier`) and rewording a draft
(:mod:`drafting`). Neither can change who is contacted, when, or for how much.
"""

from __future__ import annotations

import sys

__version__ = "0.1.0"
__all__ = ["main"]


def main() -> None:
    """Console-script entry point (``ar-collections-agent``)."""
    from .cli import main as cli_main

    raise SystemExit(cli_main(sys.argv[1:]))
