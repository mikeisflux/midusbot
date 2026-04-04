"""
A/B testing framework — safely test new signals on a fraction of trades.

Usage:
    from src.ab_test import ABTest
    ab = ABTest(experiment="new_threshold", control_pct=0.5)

    # In strategy:
    if ab.use_treatment():
        sig_thresh = new_threshold
    else:
        sig_thresh = old_threshold

    # Record outcomes:
    ab.record(group="treatment", win=True)
    ab.record(group="control",   win=True)

    # Get results:
    print(ab.summary())

All results persisted to data/ab_test_{experiment}.json
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

DATA_DIR = Path("data")


@dataclass
class ABTestResults:
    group:   str
    wins:    int   = 0
    losses:  int   = 0

    @property
    def total(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        return self.wins / self.total if self.total else 0.0


@dataclass
class ABTest:
    experiment:   str
    control_pct:  float = 0.5   # fraction of trades using the OLD logic
    _control:     ABTestResults = field(default_factory=lambda: ABTestResults("control"))
    _treatment:   ABTestResults = field(default_factory=lambda: ABTestResults("treatment"))
    _created_at:  float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self._file = DATA_DIR / f"ab_test_{self.experiment}.json"
        self._load()

    def _load(self) -> None:
        if self._file.exists():
            try:
                d = json.loads(self._file.read_text())
                c = d.get("control", {})
                t = d.get("treatment", {})
                self._control   = ABTestResults("control",   c.get("wins", 0), c.get("losses", 0))
                self._treatment = ABTestResults("treatment", t.get("wins", 0), t.get("losses", 0))
                self._created_at = d.get("created_at", self._created_at)
            except Exception:
                pass

    def _save(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self._file.write_text(json.dumps({
                "experiment":  self.experiment,
                "control_pct": self.control_pct,
                "created_at":  self._created_at,
                "control":     {"wins": self._control.wins, "losses": self._control.losses},
                "treatment":   {"wins": self._treatment.wins, "losses": self._treatment.losses},
            }))
        except Exception:
            pass

    def use_treatment(self) -> bool:
        """Returns True if this trade should use the treatment (new logic)."""
        return random.random() > self.control_pct

    def record(self, group: str, win: bool) -> None:
        """Record a trade outcome."""
        target = self._treatment if group == "treatment" else self._control
        if win:
            target.wins  += 1
        else:
            target.losses += 1
        self._save()

    def summary(self) -> str:
        c, t = self._control, self._treatment
        sig = self._significance()
        return (
            f"A/B [{self.experiment}]  "
            f"control={c.win_rate:.1%}({c.total}t)  "
            f"treatment={t.win_rate:.1%}({t.total}t)  "
            f"lift={t.win_rate - c.win_rate:+.1%}  "
            f"p≈{sig:.3f}"
        )

    def _significance(self) -> float:
        """
        Compute approximate p-value using two-proportion z-test.
        Returns p-value (< 0.05 = statistically significant).
        """
        import math
        c, t = self._control, self._treatment
        if c.total < 5 or t.total < 5:
            return 1.0
        p_c = c.win_rate
        p_t = t.win_rate
        p_pool = (c.wins + t.wins) / (c.total + t.total)
        if p_pool <= 0 or p_pool >= 1:
            return 1.0
        se = math.sqrt(p_pool * (1 - p_pool) * (1/c.total + 1/t.total))
        if se == 0:
            return 1.0
        z = abs(p_t - p_c) / se
        # Approximate p-value from z (two-tailed)
        # p ≈ 2 * (1 - Φ(|z|)) using simple approximation
        p = math.erfc(z / math.sqrt(2))
        return round(p, 4)

    def to_dict(self) -> dict:
        return {
            "experiment":  self.experiment,
            "control_wr":  round(self._control.win_rate, 3),
            "control_n":   self._control.total,
            "treatment_wr": round(self._treatment.win_rate, 3),
            "treatment_n": self._treatment.total,
            "lift":        round(self._treatment.win_rate - self._control.win_rate, 3),
            "p_value":     self._significance(),
        }


# ---------------------------------------------------------------------------
# Module-level registry
# ---------------------------------------------------------------------------

_tests: dict[str, ABTest] = {}


def get_test(name: str, control_pct: float = 0.5) -> ABTest:
    """Get or create an A/B test by name."""
    if name not in _tests:
        _tests[name] = ABTest(name, control_pct)
    return _tests[name]


def all_results() -> list[dict]:
    """Return results for all active A/B tests."""
    return [t.to_dict() for t in _tests.values()]
