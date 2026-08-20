"""
regression.py — benchmark/regression workflow.

Stores an EvalRun's aggregate metrics as a baseline, and compares a
new run against it. Flags any metric that moved in the "worse"
direction beyond a threshold, so this can gate a release without a
human reading the full report every time.

CHANGE LOG
v1.0.0 — Aug 2026 | Mukesh Kund — initial version.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from core import EvalRun

# Metrics where LOWER is better (everything else assumed higher-is-better)
LOWER_IS_BETTER = {"error_rate", "mean_seconds", "p95_seconds", "max_seconds"}


def save_baseline(run: EvalRun, path: str) -> None:
    Path(path).write_text(json.dumps({
        "run_id": run.run_id,
        "model_label": run.model_label,
        "aggregate": run.aggregate,
    }, indent=2), encoding="utf-8")


def _flatten(d: dict, prefix: str = "") -> dict:
    """Flattens nested aggregate dict into dotted keys for comparison,
    skipping non-numeric leaves and the by_category breakdown."""
    out = {}
    for k, v in d.items():
        if k == "by_category":
            continue
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        elif isinstance(v, (int, float)):
            out[key] = v
    return out


@dataclass
class RegressionResult:
    passed: bool
    degraded: list[dict]
    improved: list[dict]
    unchanged: list[dict]


def compare_to_baseline(run: EvalRun, baseline_path: str, threshold: float = 0.03) -> RegressionResult:
    """threshold = fraction change considered meaningful (default 3%)."""
    baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    base_flat = _flatten(baseline["aggregate"])
    new_flat = _flatten(run.aggregate)

    degraded, improved, unchanged = [], [], []

    for key, new_val in new_flat.items():
        base_val = base_flat.get(key)
        if base_val is None:
            continue
        if base_val == 0:
            delta_pct = 0.0 if new_val == 0 else float("inf")
        else:
            delta_pct = (new_val - base_val) / abs(base_val)

        lower_is_better = any(key.endswith(suffix) for suffix in LOWER_IS_BETTER)
        is_worse = (delta_pct > threshold) if lower_is_better else (delta_pct < -threshold)
        is_better = (delta_pct < -threshold) if lower_is_better else (delta_pct > threshold)

        entry = {"metric": key, "baseline": base_val, "current": new_val, "delta_pct": round(delta_pct, 4) if delta_pct != float("inf") else "inf"}
        if is_worse:
            degraded.append(entry)
        elif is_better:
            improved.append(entry)
        else:
            unchanged.append(entry)

    return RegressionResult(passed=len(degraded) == 0, degraded=degraded, improved=improved, unchanged=unchanged)


def print_regression_summary(result: RegressionResult) -> None:
    print(f"\n{'PASSED' if result.passed else 'FAILED'} — regression check")
    if result.degraded:
        print("\nDegraded metrics:")
        for e in result.degraded:
            print(f"  - {e['metric']}: {e['baseline']} -> {e['current']} ({e['delta_pct']})")
    if result.improved:
        print("\nImproved metrics:")
        for e in result.improved:
            print(f"  - {e['metric']}: {e['baseline']} -> {e['current']} ({e['delta_pct']})")
