"""
ML signal classifier — logistic regression trained on session log features.

Learns which combination of window_return, time-of-day, trade_count,
and regime features actually predict win probability.

Once trained (needs 50+ labeled windows), outputs a probability override
that can be compared against the fair_value from strategy.py.

Usage:
    from src.ml_classifier import MLClassifier
    clf = MLClassifier()
    clf.train()                              # retrain from session_log
    prob = clf.predict(window_return=0.002, secs_in=45, hour_utc=14)
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from loguru import logger

DATA_DIR   = Path("data")
_SESSION_LOG = DATA_DIR / "session_log.jsonl"
_MODEL_FILE  = DATA_DIR / "ml_classifier.json"   # persisted coefficients

MIN_SAMPLES = 50   # minimum labeled windows to train


class MLClassifier:
    """
    Lightweight logistic regression trained on session_log.jsonl.

    Features per window:
        x0: |pct_change| (magnitude of window return)
        x1: secs_in / 300 (fraction of window elapsed, 0–1)
        x2: hour_utc / 24 (time of day, 0–1)
        x3: is_uptrend (1 if prior 3 windows were UP, else 0)

    Label: 1 if signal_correct else 0 (only windows with signals)
    """

    def __init__(self) -> None:
        self._weights: list[float] = [0.0, 0.0, 0.0, 0.0, 0.0]  # w0=bias + 4 features
        self._trained  = False
        self._n_samples = 0
        self._load()

    def _load(self) -> None:
        if _MODEL_FILE.exists():
            try:
                d = json.loads(_MODEL_FILE.read_text())
                self._weights   = d.get("weights", self._weights)
                self._trained   = d.get("trained", False)
                self._n_samples = d.get("n_samples", 0)
            except Exception:
                pass

    def _save(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _MODEL_FILE.write_text(json.dumps({
                "weights":   self._weights,
                "trained":   self._trained,
                "n_samples": self._n_samples,
                "trained_at": int(time.time()),
            }))
        except Exception as exc:
            logger.debug(f"[ML] Save failed: {exc}")

    @staticmethod
    def _sigmoid(x: float) -> float:
        import math
        return 1.0 / (1.0 + math.exp(-max(-20, min(20, x))))

    def _extract_features(
        self,
        pct_change: float,
        secs_in: float = 120.0,
        hour_utc: int = 12,
        is_uptrend: int = 0,
    ) -> list[float]:
        return [
            1.0,                         # bias
            min(abs(pct_change), 0.02),  # magnitude (cap at 2% to avoid outlier dominance)
            min(secs_in, 300) / 300,     # time fraction
            hour_utc / 24,               # time of day
            float(is_uptrend),           # prior trend
        ]

    def train(self, lookback_days: float = 14.0) -> bool:
        """
        Train on recent session_log.jsonl data.
        Returns True if training succeeded with enough data.
        """
        if not _SESSION_LOG.exists():
            logger.debug("[ML] No session_log.jsonl — training skipped")
            return False

        cutoff = time.time() - lookback_days * 86400
        X: list[list[float]] = []
        y: list[float] = []

        entries: list[dict] = []
        try:
            with _SESSION_LOG.open() as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                        if e.get("window_start", 0) >= cutoff and e.get("signal_direction"):
                            entries.append(e)
                    except Exception:
                        pass
        except Exception:
            return False

        for i, e in enumerate(entries):
            label = 1.0 if e.get("signal_correct") else 0.0
            pct   = e.get("pct_change", 0)
            secs  = e.get("secs_in", 120)
            ts    = e.get("window_start", 0)
            hour  = int(time.gmtime(ts).tm_hour) if ts > 0 else 12

            # Compute is_uptrend from prior 3 entries for this symbol
            sym = e.get("symbol", "")
            prior = [x for x in entries[max(0, i-5):i] if x.get("symbol") == sym]
            n_up = sum(1 for p in prior[-3:] if p.get("actual_direction") == "UP")
            is_up = 1 if n_up >= 2 else 0

            X.append(self._extract_features(pct, secs or 120, hour, is_up))
            y.append(label)

        if len(X) < MIN_SAMPLES:
            logger.debug(f"[ML] Only {len(X)} samples — need {MIN_SAMPLES} to train")
            return False

        # Logistic regression via gradient descent
        import numpy as np
        X_arr = np.array(X)
        y_arr = np.array(y)
        w     = np.array(self._weights if len(self._weights) == 5 else [0.0] * 5)

        lr, epochs, lam = 0.1, 500, 0.01
        for _ in range(epochs):
            logits = X_arr @ w
            preds  = 1 / (1 + np.exp(-np.clip(logits, -20, 20)))
            errors = preds - y_arr
            grad   = X_arr.T @ errors / len(y_arr) + lam * w
            w     -= lr * grad

        self._weights   = w.tolist()
        self._trained   = True
        self._n_samples = len(X)
        self._save()

        # Compute training accuracy
        preds_bin = (1 / (1 + np.exp(-np.clip(X_arr @ w, -20, 20)))) >= 0.5
        acc = float(np.mean(preds_bin == y_arr))
        logger.info(
            f"[ML] Trained on {len(X)} samples — accuracy={acc:.1%}  "
            f"weights={[round(x, 4) for x in w.tolist()]}"
        )
        return True

    def predict(
        self,
        pct_change: float,
        secs_in: float = 120.0,
        hour_utc: int = 12,
        is_uptrend: int = 0,
    ) -> float | None:
        """
        Returns predicted win probability (0.0–1.0), or None if not trained.
        """
        if not self._trained or self._n_samples < MIN_SAMPLES:
            return None
        features = self._extract_features(pct_change, secs_in, hour_utc, is_uptrend)
        logit = sum(w * x for w, x in zip(self._weights, features))
        return self._sigmoid(logit)

    @property
    def is_ready(self) -> bool:
        return self._trained and self._n_samples >= MIN_SAMPLES


# Module-level singleton — retrain every 24h in background
_clf: MLClassifier | None = None
_last_trained: float = 0.0
_RETRAIN_INTERVAL = 24 * 3600


def get_classifier() -> MLClassifier:
    """Return the module-level classifier, retraining if stale."""
    global _clf, _last_trained
    if _clf is None:
        _clf = MLClassifier()
    if time.time() - _last_trained > _RETRAIN_INTERVAL:
        import threading
        def _retrain():
            global _last_trained
            _clf.train()
            _last_trained = time.time()
        threading.Thread(target=_retrain, daemon=True, name="ml-retrain").start()
        _last_trained = time.time()  # prevent hammering
    return _clf
