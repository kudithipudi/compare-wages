from tests.eval_harness import run_extraction_evals


def test_eval_harness_smoke(db_session):
    results, summary = run_extraction_evals()
    assert len(results) >= 5
    for key in ("n", "wage_low_accuracy_pct", "wage_high_accuracy_pct", "role_match_pct", "avg_confidence"):
        assert key in summary


def test_eval_harness_regex_baseline(db_session):
    _results, summary = run_extraction_evals()
    assert summary["n"] == 10
    assert summary["wage_low_accuracy_pct"] >= 70.0
