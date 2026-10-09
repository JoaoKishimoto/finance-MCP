"""Your personal finance rules, stored as YAML on the server and editable by Claude.

Every change is validated against the schema below, written atomically, and the
previous version is kept in DATA_DIR/backups so nothing is ever lost.
"""

from __future__ import annotations

import copy
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

MAX_BACKUPS = 30


class _Model(BaseModel):
    # Reject unknown keys so a typo from Claude fails loudly instead of being ignored.
    model_config = ConfigDict(extra="forbid")


class Income(_Model):
    monthly_net: float = Field(0.0, ge=0, description="Net monthly income (after taxes), BRL.")
    payday: int = Field(5, ge=1, le=28, description="Day of the month the salary arrives. Budget cycles run payday→payday.")
    notes: str = ""


class Investing(_Model):
    monthly_target: float = Field(0.0, ge=0, description="Amount you invest every month. Never counted as spendable.")
    emergency_fund_target: float = Field(0.0, ge=0, description="Desired emergency reserve, BRL.")
    emergency_fund_investment_names: list[str] = Field(
        default_factory=list,
        description="Substrings of investment names that make up the emergency fund (e.g. 'Tesouro Selic', 'CDB Liquidez').",
    )


class FixedCost(_Model):
    """A recurring cost that does NOT show up in Pluggy (cash, Pix to a person, boleto from another bank...)."""

    name: str
    amount: float = Field(gt=0)
    essential: bool = True
    subscription: bool = False
    notes: str = ""


class Classification(_Model):
    """How transactions are classified.

    Category entries may be Pluggy category IDs ("18000000" or the short prefix "18")
    or (part of) a category name ("Groceries"). Merchant entries are case-insensitive
    substrings matched against the merchant name / transaction description.
    """

    essential_categories: list[str] = Field(
        default_factory=lambda: [
            "18",  # Housing
            "19",  # Utilities
            "11",  # Groceries
            "20",  # Healthcare
            "21",  # Transportation
            "23",  # Insurance
            "16",  # Taxes
            "07",  # Legal obligations
        ]
    )
    subscription_categories: list[str] = Field(default_factory=lambda: ["10"])  # Digital services
    excluded_categories: list[str] = Field(
        default_factory=lambda: [
            "03",  # Investments (tracked separately against the investment target)
            "04",  # Same person transfer (moving money between your own accounts)
            "credit card payment",  # paying the card bill would double count the card purchases
        ]
    )
    essential_merchants: list[str] = Field(default_factory=list)
    not_essential_merchants: list[str] = Field(default_factory=list)
    subscription_merchants: list[str] = Field(default_factory=list)
    not_subscription_merchants: list[str] = Field(default_factory=list)
    ignored_merchants: list[str] = Field(default_factory=list, description="Never counted as spending.")


class Analysis(_Model):
    lookback_months: int = Field(3, ge=1, le=12, description="Full months used to compute monthly averages.")
    tight_threshold_pct: float = Field(10, ge=0, le=100, description="'Tight' when less than this % of the free budget remains.")


class FinanceConfig(_Model):
    version: int = 1
    income: Income = Field(default_factory=Income)
    investing: Investing = Field(default_factory=Investing)
    fixed_costs: list[FixedCost] = Field(default_factory=list)
    classification: Classification = Field(default_factory=Classification)
    analysis: Analysis = Field(default_factory=Analysis)
    notes: list[str] = Field(default_factory=list, description="Free-form facts worth remembering (goals, plans, constraints).")


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge dicts recursively. Lists and scalars in `patch` replace the old value."""
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _diff(old: Any, new: Any, path: str = "") -> list[str]:
    if isinstance(old, dict) and isinstance(new, dict):
        lines: list[str] = []
        for key in sorted(set(old) | set(new)):
            lines += _diff(old.get(key), new.get(key), f"{path}.{key}" if path else key)
        return lines
    return [] if old == new else [f"{path}: {old!r} -> {new!r}"]


class ConfigStore:
    def __init__(self, path: Path, backup_dir: Path):
        self.path = path
        self.backup_dir = backup_dir

    def load(self) -> FinanceConfig:
        if not self.path.exists():
            return FinanceConfig()
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        return FinanceConfig.model_validate(data)

    def update(self, patch: dict[str, Any]) -> tuple[FinanceConfig, list[str]]:
        """Validate and apply a partial update. Returns the new config and a human-readable diff."""
        current = self.load()
        old = current.model_dump(mode="json")
        new_config = FinanceConfig.model_validate(deep_merge(old, patch))  # raises on invalid input
        new = new_config.model_dump(mode="json")
        changes = _diff(old, new)
        if changes:
            self._backup()
            self._write(new)
        return new_config, changes

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        # Write to a temp file then rename: the config is never left half-written.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".config-", suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, self.path)

    def _backup(self) -> None:
        if not self.path.exists():
            return
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        (self.backup_dir / f"config-{stamp}.yaml").write_bytes(self.path.read_bytes())
        backups = sorted(self.backup_dir.glob("config-*.yaml"))
        for old in backups[:-MAX_BACKUPS]:
            old.unlink()
