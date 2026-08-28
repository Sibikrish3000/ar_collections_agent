"""Policy configuration loading and access.

The YAML file is the single source of truth for thresholds, timings, recipients,
hold windows, guardrails and risk weights. This module validates it and exposes
typed accessors; it contains no collections logic of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config/escalation_policy.yaml")


class PolicyError(ValueError):
    """The policy file is missing something the engine requires."""


@dataclass(frozen=True, slots=True)
class Tier:
    tier: int
    name: str
    days_past_due: int
    to: tuple[str, ...]
    cc: tuple[str, ...]
    auto_send: bool
    template: str
    rationale: str = ""


class Policy:
    """Validated view over ``escalation_policy.yaml``."""

    def __init__(self, raw: dict[str, Any], source: Path | None = None) -> None:
        self.raw = raw
        self.source = source
        self.tiers: tuple[Tier, ...] = self._load_tiers()
        self._validate()

    # -- construction ------------------------------------------------------- #

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> Policy:
        path = Path(path)
        if not path.is_file():
            raise PolicyError(f"policy file not found: {path}")
        with path.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        if not isinstance(raw, dict):
            raise PolicyError(f"{path} did not parse to a mapping")
        return cls(raw, source=path)

    def _load_tiers(self) -> tuple[Tier, ...]:
        entries = self.raw.get("escalation_tiers") or []
        if not entries:
            raise PolicyError("escalation_tiers is empty; the agent would never act")
        tiers = [
            Tier(
                tier=int(e["tier"]),
                name=str(e.get("name", f"Tier {e['tier']}")),
                days_past_due=int(e["days_past_due"]),
                to=tuple(e.get("to") or ()),
                cc=tuple(e.get("cc") or ()),
                auto_send=bool(e.get("auto_send", False)),
                template=str(e["template"]),
                rationale=str(e.get("rationale", "")).strip(),
            )
            for e in entries
        ]
        return tuple(sorted(tiers, key=lambda t: t.days_past_due))

    def _validate(self) -> None:
        thresholds = [t.days_past_due for t in self.tiers]
        if len(set(thresholds)) != len(thresholds):
            raise PolicyError(f"duplicate tier thresholds: {thresholds}")
        for tier in self.tiers:
            if not tier.to:
                raise PolicyError(f"tier {tier.tier} has no recipients")
            if tier.template not in self.templates:
                raise PolicyError(
                    f"tier {tier.tier} references unknown template {tier.template!r}"
                )
        for name in ("internal_handover",):
            if name not in self.templates:
                raise PolicyError(f"required template {name!r} missing")

    # -- sections ----------------------------------------------------------- #

    @property
    def policy_name(self) -> str:
        return str(self.raw.get("policy_name", "unnamed policy"))

    @property
    def templates(self) -> dict[str, dict[str, str]]:
        return self.raw.get("templates") or {}

    @property
    def judgment(self) -> dict[str, Any]:
        return self.raw.get("judgment_rules") or {}

    @property
    def guardrails(self) -> dict[str, Any]:
        return self.raw.get("guardrails") or {}

    @property
    def risk_model(self) -> dict[str, Any]:
        return self.raw.get("risk_model") or {}

    @property
    def holds(self) -> dict[str, int]:
        return self.judgment.get("holds") or {}

    @property
    def cadence(self) -> dict[str, Any]:
        return self.judgment.get("cadence") or {}

    @property
    def de_minimis(self) -> dict[str, Any]:
        return self.judgment.get("de_minimis") or {}

    @property
    def grace(self) -> dict[str, Any]:
        return self.judgment.get("grace_periods") or {}

    @property
    def legal_lock(self) -> dict[str, Any]:
        return self.judgment.get("legal_lock") or {}

    @property
    def deliverability(self) -> dict[str, Any]:
        return self.judgment.get("deliverability") or {}

    # -- convenience -------------------------------------------------------- #

    def hold_days(self, key: str, default: int = 0) -> int:
        return int(self.holds.get(key, default))

    def de_minimis_threshold(self) -> Decimal:
        if not self.de_minimis.get("enabled", False):
            return Decimal("0")
        return Decimal(str(self.de_minimis.get("min_balance_threshold", 0)))

    def tier_for(self, effective_days_past_due: int) -> Tier | None:
        """Highest tier whose threshold is met. ``None`` when nothing is due yet."""
        match = None
        for tier in self.tiers:
            if effective_days_past_due >= tier.days_past_due:
                match = tier
        return match

    def tier_by_number(self, number: int) -> Tier | None:
        return next((t for t in self.tiers if t.tier == number), None)

    def template(self, name: str) -> dict[str, str]:
        try:
            return self.templates[name]
        except KeyError as exc:  # pragma: no cover - guarded by _validate
            raise PolicyError(f"unknown template {name!r}") from exc

    def legal_keywords(self) -> tuple[str, ...]:
        return tuple(str(k).lower() for k in (self.legal_lock.get("keywords") or ()))

    def simulation_window(self) -> tuple[date | None, date | None]:
        sim = self.raw.get("simulation") or {}
        start = sim.get("start_date")
        end = sim.get("end_date")
        return (
            date.fromisoformat(str(start)) if start else None,
            date.fromisoformat(str(end)) if end else None,
        )
