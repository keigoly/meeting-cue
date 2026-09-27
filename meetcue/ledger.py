"""月次の費用台帳 — usage.cost を積み、月上限で fail-closed(docs/REQUIREMENTS.md §5.2)。"""
from __future__ import annotations

import json
import time
from pathlib import Path


class Ledger:
    def __init__(self, path: Path, monthly_budget_usd: float):
        self.path = Path(path)
        self.budget = float(monthly_budget_usd)
        self.data = {"month": self._month(), "spent_usd": 0.0, "calls": 0}
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if d.get("month") == self._month():
                    self.data = d
            except (ValueError, OSError):
                pass

    @staticmethod
    def _month() -> str:
        return time.strftime("%Y-%m")

    def allowed(self) -> bool:
        return self.data["spent_usd"] < self.budget

    def record(self, cost_usd: float | None) -> None:
        if self.data.get("month") != self._month():
            self.data = {"month": self._month(), "spent_usd": 0.0, "calls": 0}
        self.data["spent_usd"] = round(self.data["spent_usd"] + float(cost_usd or 0.0), 6)
        self.data["calls"] += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
