"""
Unit tests for the LLM evaluation framework and Golden Dataset v1.
Verifies parsing of ground truth, metric computations (TP/TN/FP/FN/Acc/Prec/Rec/F1),
error analysis, telemetry separation, zero-cost guarantee, and mock vs real separation.
"""

import json
from unittest.mock import patch

from eval.evaluate import (
    ConfusionMatrix,
    compute_percentile,
    estimate_tokens,
    load_dataset,
    mock_classify,
    parse_ground_truth_bool,
    render_markdown_report,
    run_evaluation,
)

from src.models.schemas import GeminiTelemetry, TalentClassificationResult


def test_parse_ground_truth_bool():
    """Verify boolean parser parses true/false string variants correctly."""
    assert parse_ground_truth_bool("TRUE") is True
    assert parse_ground_truth_bool("true") is True
    assert parse_ground_truth_bool("1") is True
    assert parse_ground_truth_bool("yes") is True
    assert parse_ground_truth_bool("FALSE") is False
    assert parse_ground_truth_bool("0") is False
    assert parse_ground_truth_bool("no") is False
    assert parse_ground_truth_bool("invalid") is None


def test_estimate_tokens():
    """Character-to-token heuristic estimation for offline pre-execution."""
    assert estimate_tokens("hello world") == 2
    assert estimate_tokens("") == 1


def test_mock_classify():
    """Mock classifier detects artist keywords vs beatmaker keywords."""
    res_artist = mock_classify("Check out my new single on Spotify! Link in bio - Marcus Kane")
    assert res_artist.is_artist_promotion is True
    assert res_artist.artist_name == "Marcus Kane"

    res_producer = mock_classify("New drum kit and loops for sale. Prod by 808")
    assert res_producer.is_artist_promotion is False


def test_confusion_matrix_accuracy_precision_recall_f1():
    """Tests correct arithmetic for TP/TN/FP/FN, Accuracy, Precision, Recall, and F1."""
    # TP=10, TN=20, FP=5, FN=5 -> Total=40
    # Accuracy = (10 + 20) / 40 = 0.75
    # Precision = 10 / (10 + 5) = 0.6667
    # Recall = 10 / (10 + 5) = 0.6667
    # F1 = 2 * (0.6667 * 0.6667) / (0.6667 + 0.6667) = 0.6667
    cm = ConfusionMatrix(tp=10, tn=20, fp=5, fn=5)
    assert cm.total == 40
    assert cm.accuracy == 0.75
    assert cm.precision == 0.6667
    assert cm.recall == 0.6667
    assert cm.f1_score == 0.6667


def test_confusion_matrix_edge_cases_zero_division():
    """Confirms metric properties return 0.0 without division-by-zero crashes on empty matrix."""
    empty_cm = ConfusionMatrix(tp=0, tn=0, fp=0, fn=0)
    assert empty_cm.total == 0
    assert empty_cm.accuracy == 0.0
    assert empty_cm.precision == 0.0
    assert empty_cm.recall == 0.0
    assert empty_cm.f1_score == 0.0


def test_golden_dataset_v1_size_balance_and_schema():
    """Verifies Golden Dataset v1 exists, has 100 items, and is balanced 50 positive / 50 negative."""
    dataset_path = "eval/dataset/talent_classification_v1.jsonl"
    records = load_dataset(dataset_path)

    assert len(records) == 100
    positives = [r for r in records if r["ground_truth"] is True]
    negatives = [r for r in records if r["ground_truth"] is False]

    assert len(positives) == 50
    assert len(negatives) == 50

    for r in records:
        assert r["id"].startswith("tc_")
        assert len(r["text"]) > 0
        assert isinstance(r["ground_truth"], bool)
        assert r["category"] is not None
        assert r["difficulty"] in ("easy", "medium", "hard")


def test_golden_dataset_v1_unique_ids():
    """Verifies all 100 items in the golden dataset have strictly unique identifiers."""
    records = load_dataset("eval/dataset/talent_classification_v1.jsonl")
    ids = [r["id"] for r in records]
    assert len(ids) == len(set(ids)) == 100


def test_error_analysis_identifies_false_positives_and_negatives(tmp_path):
    """Verifies that False Positives and False Negatives are tracked with previews and reasons."""
    test_jsonl = tmp_path / "errors_test.jsonl"
    test_jsonl.write_text(
        '{"id": "tc_err_1", "text": "Buy my loops and beats", "expected_is_artist_promotion": false}\n'
        '{"id": "tc_err_2", "text": "I sang this acoustic track", "expected_is_artist_promotion": true}\n',
        encoding="utf-8"
    )

    with patch("eval.evaluate.classify_lead_with_gemini") as mock_classify_fn:
        mock_classify_fn.side_effect = [
            TalentClassificationResult(is_artist_promotion=True, reason="Model thought it was artist", confidence=0.8),
            TalentClassificationResult(is_artist_promotion=False, reason="Model thought it was listener", confidence=0.8),
        ]
        with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat12345"}):
            metrics = run_evaluation(str(test_jsonl), use_mock=False)

    assert metrics.matrix.fp == 1
    assert metrics.matrix.fn == 1
    assert len(metrics.errors) == 2

    fp_err = next(e for e in metrics.errors if e.error_type == "FALSE_POSITIVE")
    fn_err = next(e for e in metrics.errors if e.error_type == "FALSE_NEGATIVE")

    assert fp_err.id == "tc_err_1"
    assert fp_err.ground_truth is False
    assert fp_err.predicted is True
    assert fp_err.reason == "Model thought it was artist"

    assert fn_err.id == "tc_err_2"
    assert fn_err.ground_truth is True
    assert fn_err.predicted is False
    assert fn_err.reason == "Model thought it was listener"


def test_telemetry_absent_keeps_tokens_none_without_heuristic_leak(tmp_path):
    """Ensures real telemetry keeps tokens None when not returned by provider without fallback to len//4."""
    test_jsonl = tmp_path / "telemetry_test.jsonl"
    test_jsonl.write_text(
        '{"id": "tc_t1", "text": "Check my track on Spotify", "expected_is_artist_promotion": true}\n',
        encoding="utf-8"
    )

    mock_res_without_tokens = TalentClassificationResult(
        is_artist_promotion=True,
        reason="Good track",
        confidence=0.9,
        telemetry=GeminiTelemetry(
            latency_ms=120.0,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            model_version="gemini-2.5-flash-001"
        )
    )

    with patch("eval.evaluate.classify_lead_with_gemini", return_value=mock_res_without_tokens), \
         patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat12345"}):
        metrics = run_evaluation(str(test_jsonl), use_mock=False)

    assert metrics.telemetry.mean_latency_ms == 120.0
    assert metrics.telemetry.mean_input_tokens is None
    assert metrics.telemetry.mean_output_tokens is None
    assert metrics.telemetry.mean_total_tokens is None
    assert metrics.telemetry.missing_usage_metadata_count == 1
    # Offline heuristic tokens are computed separately and explicitly
    assert metrics.telemetry.mean_estimated_input_tokens is not None


def test_evaluation_without_api_key_halts_honestly():
    """Confirms live evaluation halts safely and honestly without inventing fake metrics when key is missing."""
    with patch.dict("os.environ", {"GEMINI_API_KEY": ""}, clear=True):
        metrics = run_evaluation("eval/dataset/talent_classification_v1.jsonl", use_mock=False)
        assert metrics.mode == "UNAVAILABLE_NO_KEY"
        assert metrics.total_evaluated == 0
        assert "GEMINI_API_KEY is missing or unconfigured" in metrics.status_message

        report = render_markdown_report(metrics, "eval/dataset/talent_classification_v1.jsonl")
        assert "UNAVAILABLE (API Key Missing)" in report
        assert "Model Quality Claims:** `NONE`" in report
        assert "no performance scores are reported" in report


def test_run_evaluation_mock_mode(tmp_path):
    """Run evaluation over a test CSV using mock mode."""
    test_csv = tmp_path / "test_dataset.csv"
    test_csv.write_text(
        "id,text,source,ground_truth_is_artist,ground_truth_artist_name,notes\n"
        "1,Check out my new single 'Sunset',youtube_comment,TRUE,Artist One,Test\n"
        "2,Buy my drum kit,twitter_search,FALSE,,Test\n",
        encoding="utf-8"
    )

    metrics = run_evaluation(str(test_csv), use_mock=True)
    assert metrics.total_evaluated == 2
    assert metrics.true_positives == 1
    assert metrics.true_negatives == 1
    assert metrics.precision == 1.0
    assert metrics.recall == 1.0
    assert metrics.f1_score == 1.0
    assert metrics.failures == 0


def test_run_evaluation_golden_dataset_mock_mode():
    """Runs full golden dataset v1 through mock classifier, verifying 100 evaluated items."""
    metrics = run_evaluation("eval/dataset/talent_classification_v1.jsonl", use_mock=True)
    assert metrics.total_evaluated == 100
    assert metrics.mode == "OFFLINE_MOCK"
    assert metrics.accuracy > 0.0
    assert metrics.precision > 0.0
    assert metrics.recall > 0.0
    assert metrics.f1_score > 0.0
    # Telemetry is unavailable in mock mode
    assert metrics.telemetry.mean_latency_ms is None
    assert metrics.telemetry.mean_input_tokens is None

    report = render_markdown_report(metrics, "talent_classification_v1.jsonl")
    assert "LLM Evaluation Benchmark Report (Pipeline Validation Only)" in report
    assert "Execution Mode:** `OFFLINE_MOCK`" in report
    assert "Model Quality Claims:** `NONE`" in report
    assert "Synthetic Confusion Matrix" in report
    assert "Synthetic Error Inspection" in report


def test_no_cost_structure_in_benchmark_result():
    """Confirms zero financial cost, price parameters, or cost projections exist in benchmark result or reports."""
    metrics = run_evaluation("eval/dataset/talent_classification_v1.jsonl", use_mock=True)
    exported_dict = metrics.to_dict()

    # Recursively check for cost and price keys in dictionary
    def check_no_cost_keys(d):
        for k, v in d.items():
            assert "cost" not in k.lower(), f"Unexpected cost key found: {k}"
            assert "price" not in k.lower(), f"Unexpected price key found: {k}"
            if isinstance(v, dict):
                check_no_cost_keys(v)

    check_no_cost_keys(exported_dict)
    assert not hasattr(metrics, "estimated_cost_per_1k_calls_usd")

    report = render_markdown_report(metrics, "talent_classification_v1.jsonl")
    assert "USD" not in report
    assert "$" not in report
    assert "cost" not in report.lower()
    assert "price" not in report.lower()


def test_mock_results_never_labeled_as_measured():
    """Verifies that mock mode results are never labeled as 'Measured'."""
    metrics = run_evaluation("eval/dataset/talent_classification_v1.jsonl", use_mock=True)
    report = render_markdown_report(metrics, "talent_classification_v1.jsonl")

    # In mock mode, resource telemetry status must be Unavailable, never Measured
    lines_with_status = [line for line in report.splitlines() if "|" in line]
    for line in lines_with_status:
        if "Prompt Tokens" in line or "Output Tokens" in line or "HTTP Latency" in line:
            assert "Measured" not in line, f"Mock metric incorrectly labeled as Measured: {line}"


def test_percentile_computation():
    """Verifies linear interpolation percentile computation."""
    assert compute_percentile([], 50.0) is None
    vals = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert compute_percentile(vals, 50.0) == 30.0
    assert compute_percentile(vals, 95.0) == 48.0


# ---------------------------------------------------------------------------
# P1-E-02: Operational Failure Isolation & Metric Integrity Tests
# ---------------------------------------------------------------------------
def test_evaluation_excludes_operational_failures_from_confusion_matrix(tmp_path):
    """Proves network failures and 500 errors do NOT become false True Negatives or False Negatives."""
    test_jsonl = tmp_path / "test_failures.jsonl"
    records = [
        {"id": "t1", "text": "Listen to my song", "expected_is_artist_promotion": True},
        {"id": "t2", "text": "Buy my drum kit", "expected_is_artist_promotion": False},
        {"id": "t3", "text": "Network down comment", "expected_is_artist_promotion": False},
    ]
    with open(test_jsonl, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    def mock_classifier_side_effect(text):
        if "Network down" in text:
            return TalentClassificationResult(
                is_artist_promotion=False,
                artist_name=None,
                reason="Evaluation exhausted retries or failed network call",
                confidence=0.0,
                status="ERROR",
                error_message="Connection timed out"
            )
        elif "Listen" in text:
            return TalentClassificationResult(
                is_artist_promotion=True,
                artist_name="Artist",
                reason="Promo detected",
                confidence=0.9,
                status="SUCCESS"
            )
        else:
            return TalentClassificationResult(
                is_artist_promotion=False,
                artist_name=None,
                reason="Beat producer",
                confidence=0.85,
                status="SUCCESS"
            )

    with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat12345"}), \
         patch("eval.evaluate.classify_lead_with_gemini", side_effect=mock_classifier_side_effect):
        metrics = run_evaluation(str(test_jsonl), use_mock=False)

        assert metrics.total_samples == 3
        assert metrics.failures == 1
        assert metrics.valid_predictions == 2
        # Crucial check: The failed negative comment (t3) was NOT counted as a True Negative!
        assert metrics.true_positives == 1
        assert metrics.true_negatives == 1
        assert metrics.false_positives == 0
        assert metrics.false_negatives == 0
        assert metrics.accuracy == 1.0  # 2/2 valid predictions
        assert any(e.error_type == "OPERATIONAL_FAILURE" and e.id == "t3" for e in metrics.errors)


def test_evaluation_partial_failure_metrics_consistency(tmp_path):
    """Verifies precision, recall, and accuracy denominator use valid predictions, not corrupted total."""
    test_jsonl = tmp_path / "test_partial.jsonl"
    records = [
        {"id": "p1", "text": "Real artist", "expected_is_artist_promotion": True},
        {"id": "p2", "text": "Listener", "expected_is_artist_promotion": False},
        {"id": "p3", "text": "Failed call", "expected_is_artist_promotion": True},
    ]
    with open(test_jsonl, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    def mock_classifier(text):
        if "Failed" in text:
            return TalentClassificationResult(
                is_artist_promotion=False,
                confidence=0.0,
                status="ERROR",
                error_message="HTTP 503 Service Unavailable"
            )
        elif "Real" in text:
            return TalentClassificationResult(
                is_artist_promotion=True,
                confidence=0.9,
                status="SUCCESS"
            )
        else:
            return TalentClassificationResult(
                is_artist_promotion=False,
                confidence=0.9,
                status="SUCCESS"
            )

    with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat12345"}), \
         patch("eval.evaluate.classify_lead_with_gemini", side_effect=mock_classifier):
        metrics = run_evaluation(str(test_jsonl), use_mock=False)

        assert metrics.total_samples == 3
        assert metrics.failures == 1
        assert metrics.valid_predictions == 2
        assert metrics.true_positives == 1
        assert metrics.true_negatives == 1
        assert metrics.accuracy == 1.0
        assert metrics.failure_rate == round(1 / 3, 4)


# ---------------------------------------------------------------------------
# P1-E FIX-3: Fallback Exclusion & Strict Evaluation Semantics Tests
# ---------------------------------------------------------------------------
def test_evaluation_fallback_with_positive_ground_truth(tmp_path):
    """Fallback with positive ground truth must NOT become a False Negative."""
    test_jsonl = tmp_path / "fb_pos.jsonl"
    record = {"id": "fb1", "text": "Listen to my song", "expected_is_artist_promotion": True}
    with open(test_jsonl, "w", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    fallback_res = TalentClassificationResult(
        is_artist_promotion=False,
        reason="Unconfigured GEMINI_API_KEY",
        confidence=0.0,
        status="FALLBACK"
    )

    with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat12345"}), \
         patch("eval.evaluate.classify_lead_with_gemini", return_value=fallback_res):
        metrics = run_evaluation(str(test_jsonl), use_mock=False)

        assert metrics.total_samples == 1
        assert metrics.valid_predictions == 0
        assert metrics.fallbacks == 1
        assert metrics.failures == 0
        assert metrics.false_negatives == 0
        assert metrics.true_positives == 0
        assert metrics.matrix.total == 0
        assert any(e.error_type == "FALLBACK_EXCLUDED" and e.id == "fb1" for e in metrics.errors)


def test_evaluation_fallback_with_negative_ground_truth(tmp_path):
    """Fallback with negative ground truth must NOT become a false True Negative."""
    test_jsonl = tmp_path / "fb_neg.jsonl"
    record = {"id": "fb2", "text": "Buy my drum kit", "expected_is_artist_promotion": False}
    with open(test_jsonl, "w", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    fallback_res = TalentClassificationResult(
        is_artist_promotion=False,
        reason="Unconfigured GEMINI_API_KEY",
        confidence=0.0,
        status="FALLBACK"
    )

    with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat12345"}), \
         patch("eval.evaluate.classify_lead_with_gemini", return_value=fallback_res):
        metrics = run_evaluation(str(test_jsonl), use_mock=False)

        assert metrics.total_samples == 1
        assert metrics.valid_predictions == 0
        assert metrics.fallbacks == 1
        assert metrics.failures == 0
        assert metrics.true_negatives == 0
        assert metrics.false_positives == 0
        assert metrics.matrix.total == 0
        assert any(e.error_type == "FALLBACK_EXCLUDED" and e.id == "fb2" for e in metrics.errors)


def test_evaluation_combination_success_fallback_and_error(tmp_path):
    """Proves SUCCESS, FALLBACK, and ERROR are strictly segregated in metrics."""
    test_jsonl = tmp_path / "combo.jsonl"
    records = [
        {"id": "c1", "text": "Real artist track", "expected_is_artist_promotion": True},
        {"id": "c2", "text": "Drum kit sale", "expected_is_artist_promotion": False},
        {"id": "c3", "text": "Positive lead on fallback", "expected_is_artist_promotion": True},
        {"id": "c4", "text": "Negative lead on fallback", "expected_is_artist_promotion": False},
        {"id": "c5", "text": "Network down lead", "expected_is_artist_promotion": False},
    ]
    with open(test_jsonl, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    def mock_classifier(text):
        if "fallback" in text:
            return TalentClassificationResult(
                is_artist_promotion=False,
                reason="Unconfigured GEMINI_API_KEY",
                confidence=0.0,
                status="FALLBACK"
            )
        elif "down" in text:
            return TalentClassificationResult(
                is_artist_promotion=False,
                reason="Network timeout",
                confidence=0.0,
                status="ERROR",
                error_message="Connection timed out"
            )
        elif "Real" in text:
            return TalentClassificationResult(
                is_artist_promotion=True,
                reason="Artist single promo",
                confidence=0.9,
                status="SUCCESS"
            )
        else:
            return TalentClassificationResult(
                is_artist_promotion=False,
                reason="Drum kit producer",
                confidence=0.9,
                status="SUCCESS"
            )

    with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat12345"}), \
         patch("eval.evaluate.classify_lead_with_gemini", side_effect=mock_classifier):
        metrics = run_evaluation(str(test_jsonl), use_mock=False)

        assert metrics.total_samples == 5
        assert metrics.valid_predictions == 2
        assert metrics.fallbacks == 2
        assert metrics.failures == 1
        assert metrics.true_positives == 1
        assert metrics.true_negatives == 1
        assert metrics.false_positives == 0
        assert metrics.false_negatives == 0
        assert metrics.accuracy == 1.0  # (1 + 1) / 2
        assert metrics.precision == 1.0
        assert metrics.recall == 1.0
        assert metrics.f1_score == 1.0
        assert metrics.fallback_rate == 0.4
        assert metrics.failure_rate == 0.2


def test_evaluation_exclusively_fallbacks(tmp_path):
    """When all samples return fallback, no quality metrics are established."""
    test_jsonl = tmp_path / "all_fallbacks.jsonl"
    records = [
        {"id": "f1", "text": "Artist A", "expected_is_artist_promotion": True},
        {"id": "f2", "text": "Producer B", "expected_is_artist_promotion": False},
        {"id": "f3", "text": "Artist C", "expected_is_artist_promotion": True},
    ]
    with open(test_jsonl, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    fallback_res = TalentClassificationResult(
        is_artist_promotion=False,
        reason="Unconfigured GEMINI_API_KEY",
        confidence=0.0,
        status="FALLBACK"
    )

    with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat12345"}), \
         patch("eval.evaluate.classify_lead_with_gemini", return_value=fallback_res):
        metrics = run_evaluation(str(test_jsonl), use_mock=False)

        assert metrics.total_samples == 3
        assert metrics.valid_predictions == 0
        assert metrics.fallbacks == 3
        assert metrics.failures == 0
        assert metrics.matrix.total == 0
        assert metrics.accuracy == 0.0
        assert metrics.precision == 0.0
        assert metrics.recall == 0.0
        assert metrics.f1_score == 0.0
        assert metrics.to_dict()["model_quality_claims"] == "NONE"

        report = render_markdown_report(metrics, "all_fallbacks.jsonl")
        assert "No Valid Model Predictions" in report
        assert "**Excluded Fallbacks:** 3" in report
        assert "Fallbacks Encountered" in report


def test_evaluation_zero_valid_predictions_mix_of_fallback_and_error(tmp_path):
    """When all samples are either fallback or error, quality claims are strictly NONE."""
    test_jsonl = tmp_path / "zero_valid.jsonl"
    records = [
        {"id": "z1", "text": "Fallback 1", "expected_is_artist_promotion": True},
        {"id": "z2", "text": "Error 1", "expected_is_artist_promotion": False},
        {"id": "z3", "text": "Fallback 2", "expected_is_artist_promotion": False},
        {"id": "z4", "text": "Error 2", "expected_is_artist_promotion": True},
    ]
    with open(test_jsonl, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    def mock_classifier(text):
        if "Fallback" in text:
            return TalentClassificationResult(
                is_artist_promotion=False,
                reason="Fallback trigger",
                confidence=0.0,
                status="FALLBACK"
            )
        return TalentClassificationResult(
            is_artist_promotion=False,
            reason="Network 500",
            confidence=0.0,
            status="ERROR",
            error_message="HTTP 500"
        )

    with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat12345"}), \
         patch("eval.evaluate.classify_lead_with_gemini", side_effect=mock_classifier):
        metrics = run_evaluation(str(test_jsonl), use_mock=False)

        assert metrics.total_samples == 4
        assert metrics.valid_predictions == 0
        assert metrics.fallbacks == 2
        assert metrics.failures == 2
        assert metrics.matrix.total == 0
        assert metrics.to_dict()["model_quality_claims"] == "NONE"

        report = render_markdown_report(metrics, "zero_valid.jsonl")
        assert "No Valid Model Predictions" in report
        assert "**Excluded Fallbacks:** 2" in report
        assert "**Excluded Operational Failures:** 2" in report
        assert "Fallbacks Encountered" in report
        assert "Operational Failures Encountered" in report
