from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services import llm

GOLDEN_PATH = Path(__file__).resolve().parent.parent / "data" / "golden_postings.json"


def _role_match(expected_role: str, predicted_role: str) -> bool:
    e = (expected_role or "").strip().lower()
    p = (predicted_role or "").strip().lower()
    if not e or not p:
        return False
    return e in p or p in e


def _within(a: float | None, b: float | None, tol: float = 0.10) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol


def run_extraction_evals() -> tuple[list[dict], dict]:
    items = json.loads(GOLDEN_PATH.read_text())
    results: list[dict[str, Any]] = []
    confidences: list[float] = []

    for item in items:
        gid = item.get("id", "")
        employer = item.get("employer", "")
        raw_title = item.get("raw_title", "")
        expected = item.get("expected", {})

        entry: dict[str, Any] = {
            "id": gid,
            "employer": employer,
            "raw_title": raw_title,
            "expected": expected,
            "predicted": {},
            "wage_low_correct": False,
            "wage_high_correct": False,
            "role_match": False,
            "error": None,
        }

        try:
            result = llm.extract_wage(item.get("html", ""), raw_title)
            predicted = result.parsed
            entry["predicted"] = predicted
            entry["wage_low_correct"] = _within(predicted.get("wage_low"), expected.get("wage_low"))
            entry["wage_high_correct"] = _within(predicted.get("wage_high"), expected.get("wage_high"))
            entry["role_match"] = _role_match(expected.get("role", ""), predicted.get("role", ""))
            conf = predicted.get("confidence")
            if isinstance(conf, (int, float)):
                confidences.append(float(conf))
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"

        results.append(entry)

    n = len(results)
    def _pct(flag_key: str) -> float:
        if not n:
            return 0.0
        return round(100.0 * sum(1 for r in results if r[flag_key]) / n, 1)

    summary = {
        "n": n,
        "wage_low_accuracy_pct": _pct("wage_low_correct"),
        "wage_high_accuracy_pct": _pct("wage_high_correct"),
        "role_match_pct": _pct("role_match"),
        "avg_confidence": round(sum(confidences) / len(confidences), 3) if confidences else 0.0,
    }
    return results, summary
