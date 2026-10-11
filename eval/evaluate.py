"""
Evaluation framework for LLM-based talent classification.
Measures Accuracy, Precision, Recall, F1-Score, Confusion Matrix, Error Analysis,
and real provider telemetry (latency, tokens, attempts).
Strictly separates live measured metrics from synthetic mock verification and refuses
to fabricate metrics when API keys are absent.
"""

import argparse
import csv
import json
import logging
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

# Ensure repository root is on sys.path for standalone CLI execution
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.models.schemas import TalentClassificationResult
from src.utils.gemini_classifier import classify_lead_with_gemini
from src.utils.system import configure_utf8_stdout

configure_utf8_stdout()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("evaluator")


def estimate_tokens(text: str) -> int:
    """
    Offline character-based heuristic approximation: ~4 characters per token.
    NOTE: This is a character-based heuristic for offline verification only,
    NOT real provider telemetry from the Gemini API (which is captured via usageMetadata).
    """
    return max(1, len(text) // 4)


def parse_ground_truth_bool(value: Any) -> bool | None:
    """Parses various boolean representations from CSV cells."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        val = value.strip().upper()
        if val in ("TRUE", "1", "T", "YES", "Y"):
            return True
        if val in ("FALSE", "0", "F", "NO", "N"):
            return False
    return None


def compute_percentile(values: list[float], percentile: float) -> float | None:
    """Computes percentile using linear interpolation on sorted list."""
    if not values:
        return None
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * (percentile / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return round(sorted_vals[int(k)], 2)
    d0 = sorted_vals[int(f)] * (c - k)
    d1 = sorted_vals[int(c)] * (k - f)
    return round(d0 + d1, 2)


@dataclass
class ConfusionMatrix:
    tp: int = 0
    tn: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def total(self) -> int:
        return self.tp + self.tn + self.fp + self.fn

    @property
    def accuracy(self) -> float:
        return round((self.tp + self.tn) / self.total, 4) if self.total > 0 else 0.0

    @property
    def precision(self) -> float:
        denom = self.tp + self.fp
        return round(self.tp / denom, 4) if denom > 0 else 0.0

    @property
    def recall(self) -> float:
        denom = self.tp + self.fn
        return round(self.tp / denom, 4) if denom > 0 else 0.0

    @property
    def f1_score(self) -> float:
        p, r = self.precision, self.recall
        return round((2 * p * r) / (p + r), 4) if (p + r) > 0 else 0.0


@dataclass
class ClassificationError:
    id: str
    error_type: str  # "FALSE_POSITIVE" | "FALSE_NEGATIVE"
    text_preview: str
    ground_truth: bool
    predicted: bool
    reason: str
    expected_artist_name: str | None = None
    predicted_artist_name: str | None = None


@dataclass
class AggregatedTelemetry:
    total_calls: int = 0
    calls_with_telemetry: int = 0
    calls_with_tokens: int = 0
    total_retries: int = 0
    missing_usage_metadata_count: int = 0
    mean_latency_ms: float | None = None
    p50_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    p99_latency_ms: float | None = None
    mean_input_tokens: float | None = None
    mean_output_tokens: float | None = None
    mean_total_tokens: float | None = None
    mean_estimated_input_tokens: float | None = None
    mean_estimated_output_tokens: float | None = None
    model_versions_seen: list[str] = field(default_factory=list)


@dataclass
class EvaluationMetrics:
    dataset_path: str
    dataset_version: str
    mode: str  # "LIVE_PROVIDER" | "OFFLINE_MOCK" | "UNAVAILABLE_NO_KEY"
    matrix: ConfusionMatrix
    errors: list[ClassificationError]
    telemetry: AggregatedTelemetry
    total_samples: int = 0
    failures: int = 0
    fallbacks: int = 0
    status_message: str = ""

    # Backwards compatibility and quality properties
    @property
    def total_evaluated(self) -> int:
        if self.mode == "UNAVAILABLE_NO_KEY":
            return 0
        return self.matrix.total

    @property
    def valid_predictions(self) -> int:
        return self.matrix.total

    @property
    def true_positives(self) -> int:
        return self.matrix.tp

    @property
    def true_negatives(self) -> int:
        return self.matrix.tn

    @property
    def false_positives(self) -> int:
        return self.matrix.fp

    @property
    def false_negatives(self) -> int:
        return self.matrix.fn

    @property
    def precision(self) -> float:
        return self.matrix.precision

    @property
    def recall(self) -> float:
        return self.matrix.recall

    @property
    def f1_score(self) -> float:
        return self.matrix.f1_score

    @property
    def accuracy(self) -> float:
        return self.matrix.accuracy

    @property
    def failure_rate(self) -> float:
        denom = self.total_samples if self.total_samples > 0 else self.total_evaluated
        return round(self.failures / denom, 4) if denom > 0 else 0.0

    @property
    def fallback_rate(self) -> float:
        denom = self.total_samples if self.total_samples > 0 else self.total_evaluated
        return round(self.fallbacks / denom, 4) if denom > 0 else 0.0

    @property
    def avg_input_tokens(self) -> float:
        if self.telemetry.mean_input_tokens is not None:
            return self.telemetry.mean_input_tokens
        return self.telemetry.mean_estimated_input_tokens or 0.0

    @property
    def avg_output_tokens(self) -> float:
        if self.telemetry.mean_output_tokens is not None:
            return self.telemetry.mean_output_tokens
        return self.telemetry.mean_estimated_output_tokens or 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_path": self.dataset_path,
            "dataset_version": self.dataset_version,
            "mode": self.mode,
            "status_message": self.status_message,
            "model_quality_claims": "NONE" if (self.mode in ("OFFLINE_MOCK", "UNAVAILABLE_NO_KEY") or self.valid_predictions == 0) else "MEASURED_PROVIDER",
            "confusion_matrix": asdict(self.matrix),
            "summary_metrics": {
                "total_samples": self.total_samples,
                "valid_predictions": self.valid_predictions,
                "total_evaluated": self.total_evaluated,
                "accuracy": self.accuracy,
                "precision": self.precision,
                "recall": self.recall,
                "f1_score": self.f1_score,
                "failures": self.failures,
                "failure_rate": self.failure_rate,
                "fallbacks": self.fallbacks,
                "fallback_rate": self.fallback_rate,
            },
            "telemetry": asdict(self.telemetry),
            "errors": [asdict(e) for e in self.errors],
        }


def mock_classify(text: str) -> TalentClassificationResult:
    """Deterministic mock classifier for offline validation and CI test runs."""
    lower_text = text.lower()
    is_artist = (
        "single" in lower_text
        or "album" in lower_text
        or "check out my" in lower_text
        or "link in bio" in lower_text
        or "vocal" in lower_text
        or "som novo" in lower_text
        or "spotify" in lower_text
        or "cantor" in lower_text
        or "compositor" in lower_text
        or "rimando" in lower_text
    ) and not (
        "drum kit" in lower_text
        or "prod by" in lower_text
        or "loops" in lower_text
        or "beats" in lower_text
        or "mixing" in lower_text
        or "playlist" in lower_text
        or "pedrada" in lower_text
        or "reminds me" in lower_text
    )

    artist_name = "Marcus Kane" if "marcus kane" in lower_text else None
    return TalentClassificationResult(
        is_artist_promotion=is_artist,
        artist_name=artist_name,
        reason="Mock deterministic classification",
        confidence=0.95 if is_artist else 0.85
    )


def load_dataset(dataset_path: str) -> list[dict[str, Any]]:
    """Loads benchmark dataset from either .jsonl or .csv format."""
    if not os.path.exists(dataset_path):
        raise FileNotFoundError(f"Evaluation dataset not found: {dataset_path}")

    records: list[dict[str, Any]] = []
    if dataset_path.endswith(".jsonl"):
        with open(dataset_path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                item = json.loads(line)
                rec_id = str(item.get("id", f"tc_{line_no:03d}"))
                text = str(item.get("text", "")).strip()
                gt = bool(item.get("expected_is_artist_promotion"))
                records.append({
                    "id": rec_id,
                    "text": text,
                    "ground_truth": gt,
                    "ground_truth_name": item.get("expected_artist_name"),
                    "category": item.get("category"),
                    "difficulty": item.get("difficulty")
                })
    else:
        with open(dataset_path, encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                text = row.get("text", "").strip()
                if not text or text.startswith("[PREENCHER]"):
                    continue
                gt_val = parse_ground_truth_bool(row.get("ground_truth_is_artist", ""))
                if gt_val is None:
                    continue
                records.append({
                    "id": str(row.get("id", "")),
                    "text": text,
                    "ground_truth": gt_val,
                    "ground_truth_name": row.get("ground_truth_artist_name", "").strip() or None,
                    "category": None,
                    "difficulty": None
                })
    return records


def run_evaluation(
    dataset_path: str,
    prompt_template_path: str | None = None,
    use_mock: bool = False
) -> EvaluationMetrics:
    """Executes evaluation across benchmark dataset and computes statistical quality metrics."""
    rows_to_evaluate = load_dataset(dataset_path)
    version = "v1" if "v1" in dataset_path else "unknown"

    if not rows_to_evaluate:
        logger.warning("No labeled evaluation records found in dataset: %s", dataset_path)
        return EvaluationMetrics(
            dataset_path=dataset_path,
            dataset_version=version,
            mode="EMPTY_DATASET",
            matrix=ConfusionMatrix(),
            errors=[],
            telemetry=AggregatedTelemetry(),
            status_message="No records found in dataset."
        )

    # Check API key if live mode requested
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    is_key_configured = bool(gemini_key and "your_gemini" not in gemini_key and "AIzaSy" in gemini_key)

    if not use_mock and not is_key_configured:
        logger.warning(
            "GEMINI_API_KEY is not configured. Live benchmark cannot execute. "
            "Halting execution to prevent fabricated or fallback metrics."
        )
        return EvaluationMetrics(
            dataset_path=dataset_path,
            dataset_version=version,
            mode="UNAVAILABLE_NO_KEY",
            matrix=ConfusionMatrix(),
            errors=[],
            telemetry=AggregatedTelemetry(),
            status_message="GEMINI_API_KEY is missing or unconfigured. Live benchmark cannot execute. "
                           "Set GEMINI_API_KEY in .env or run with --mock for offline simulation."
        )

    exec_mode = "OFFLINE_MOCK" if use_mock else "LIVE_PROVIDER"

    prompt_template = ""
    if prompt_template_path and os.path.exists(prompt_template_path):
        with open(prompt_template_path, encoding="utf-8") as f:
            prompt_template = f.read()

    tp = tn = fp = fn = 0
    failures = fallbacks = 0
    errors: list[ClassificationError] = []

    # Telemetry collectors
    latencies: list[float] = []
    input_tokens_list: list[int] = []
    output_tokens_list: list[int] = []
    total_tokens_list: list[int] = []
    total_retries = 0
    missing_usage_metadata = 0
    model_versions: set[str] = set()
    telemetry_count = 0

    # Heuristic collectors (for comparison)
    total_input_chars = 0
    total_output_chars = 0

    for item in rows_to_evaluate:
        text = item["text"]
        gt = item["ground_truth"]
        prompt_text = prompt_template.replace("{text}", text) if "{text}" in prompt_template else text
        total_input_chars += len(prompt_text)

        try:
            if use_mock:
                pred = mock_classify(text)
            else:
                pred = classify_lead_with_gemini(text)

            total_output_chars += len(pred.model_dump_json())

            # Identify and exclude fallbacks from model quality metrics
            is_fallback = (
                getattr(pred, "status", None) == "FALLBACK"
                or pred.reason.startswith("Unconfigured GEMINI_API_KEY")
                or pred.reason.startswith("Fallback")
            )
            if is_fallback:
                fallbacks += 1
                errors.append(ClassificationError(
                    id=item["id"],
                    error_type="FALLBACK_EXCLUDED",
                    text_preview=text[:80] + ("..." if len(text) > 80 else ""),
                    ground_truth=gt,
                    predicted=bool(pred.is_artist_promotion),
                    reason=pred.reason or "Fallback response excluded from model quality metrics",
                    expected_artist_name=item.get("ground_truth_name"),
                    predicted_artist_name=pred.artist_name
                ))
                # Do NOT increment confusion matrix with fallback
                continue

            # Isolate operational failures (network errors, timeouts, malformed responses)
            if getattr(pred, "status", None) == "ERROR":
                failures += 1
                errors.append(ClassificationError(
                    id=item["id"],
                    error_type="OPERATIONAL_FAILURE",
                    text_preview=text[:80] + ("..." if len(text) > 80 else ""),
                    ground_truth=gt,
                    predicted=False,
                    reason=pred.reason or (pred.error_message or "Operational failure"),
                    expected_artist_name=item.get("ground_truth_name"),
                    predicted_artist_name=None
                ))
                # Do NOT increment confusion matrix with operational failure
                continue

            # Confusion matrix for valid model predictions
            predicted_bool = bool(pred.is_artist_promotion)
            if predicted_bool and gt:
                tp += 1
            elif not predicted_bool and not gt:
                tn += 1
            elif predicted_bool and not gt:
                fp += 1
                errors.append(ClassificationError(
                    id=item["id"],
                    error_type="FALSE_POSITIVE",
                    text_preview=text[:80] + ("..." if len(text) > 80 else ""),
                    ground_truth=gt,
                    predicted=predicted_bool,
                    reason=pred.reason,
                    expected_artist_name=item.get("ground_truth_name"),
                    predicted_artist_name=pred.artist_name
                ))
            else:
                fn += 1
                errors.append(ClassificationError(
                    id=item["id"],
                    error_type="FALSE_NEGATIVE",
                    text_preview=text[:80] + ("..." if len(text) > 80 else ""),
                    ground_truth=gt,
                    predicted=predicted_bool,
                    reason=pred.reason,
                    expected_artist_name=item.get("ground_truth_name"),
                    predicted_artist_name=pred.artist_name
                ))

            # Telemetry collection (Strictly from pred.telemetry)
            if pred.telemetry is not None:
                telemetry_count += 1
                if pred.telemetry.latency_ms is not None:
                    latencies.append(pred.telemetry.latency_ms)
                if pred.telemetry.attempts > 1:
                    total_retries += (pred.telemetry.attempts - 1)
                if pred.telemetry.model_version:
                    model_versions.add(pred.telemetry.model_version)

                if (pred.telemetry.input_tokens is not None
                        and pred.telemetry.output_tokens is not None
                        and pred.telemetry.total_tokens is not None):
                    input_tokens_list.append(pred.telemetry.input_tokens)
                    output_tokens_list.append(pred.telemetry.output_tokens)
                    total_tokens_list.append(pred.telemetry.total_tokens)
                else:
                    missing_usage_metadata += 1

        except Exception as err:
            logger.error("Error during evaluation of record ID %s: %s", item.get("id"), err)
            failures += 1
            errors.append(ClassificationError(
                id=item.get("id", "unknown"),
                error_type="OPERATIONAL_FAILURE",
                text_preview=text[:80] + ("..." if len(text) > 80 else ""),
                ground_truth=gt,
                predicted=False,
                reason=f"Harness exception: {err}",
                expected_artist_name=item.get("ground_truth_name"),
                predicted_artist_name=None
            ))

    total_records = len(rows_to_evaluate)
    matrix = ConfusionMatrix(tp=tp, tn=tn, fp=fp, fn=fn)

    # Compute aggregated telemetry
    mean_lat = round(sum(latencies) / len(latencies), 2) if latencies else None
    p50_lat = compute_percentile(latencies, 50.0)
    p95_lat = compute_percentile(latencies, 95.0)
    p99_lat = compute_percentile(latencies, 99.0) if len(latencies) >= 20 else None

    mean_in_tokens = round(sum(input_tokens_list) / len(input_tokens_list), 1) if input_tokens_list else None
    mean_out_tokens = round(sum(output_tokens_list) / len(output_tokens_list), 1) if output_tokens_list else None
    mean_tot_tokens = round(sum(total_tokens_list) / len(total_tokens_list), 1) if total_tokens_list else None

    # Offline heuristic tokens
    avg_est_input = round(estimate_tokens(" " * (total_input_chars // max(1, total_records))), 1)
    avg_est_output = round(estimate_tokens(" " * (total_output_chars // max(1, total_records))), 1)

    agg_telemetry = AggregatedTelemetry(
        total_calls=total_records,
        calls_with_telemetry=telemetry_count,
        calls_with_tokens=len(input_tokens_list),
        total_retries=total_retries,
        missing_usage_metadata_count=missing_usage_metadata,
        mean_latency_ms=mean_lat,
        p50_latency_ms=p50_lat,
        p95_latency_ms=p95_lat,
        p99_latency_ms=p99_lat,
        mean_input_tokens=mean_in_tokens,
        mean_output_tokens=mean_out_tokens,
        mean_total_tokens=mean_tot_tokens,
        mean_estimated_input_tokens=avg_est_input,
        mean_estimated_output_tokens=avg_est_output,
        model_versions_seen=sorted(model_versions)
    )

    return EvaluationMetrics(
        dataset_path=dataset_path,
        dataset_version=version,
        mode=exec_mode,
        matrix=matrix,
        errors=errors,
        telemetry=agg_telemetry,
        total_samples=total_records,
        failures=failures,
        fallbacks=fallbacks,
        status_message=f"Evaluation completed in mode: {exec_mode}"
    )


def render_markdown_report(metrics: EvaluationMetrics, dataset_name: str) -> str:
    """Generates structured GitHub Flavored Markdown report strictly separating Mock from Live."""
    if metrics.mode == "UNAVAILABLE_NO_KEY":
        return f"""# LLM Evaluation Benchmark Report

**Dataset:** `{dataset_name}` (Version: `{metrics.dataset_version}`)
**Status:** `UNAVAILABLE (API Key Missing)`
**Model Quality Claims:** `NONE`

> [!WARNING]
> Live evaluation against Gemini API could not be executed because `GEMINI_API_KEY` is not configured or is a placeholder.
> To prevent fabricated metrics or misleading benchmarks, no performance scores are reported.
>
> **How to execute:**
> - To run a live benchmark: configure `GEMINI_API_KEY` in `.env` and run `python eval/evaluate.py --dataset {dataset_name}`.
> - To run an offline contract test: run `python eval/evaluate.py --dataset {dataset_name} --mock`.
"""

    if metrics.mode == "OFFLINE_MOCK":
        fps = [e for e in metrics.errors if e.error_type == "FALSE_POSITIVE"]
        fns = [e for e in metrics.errors if e.error_type == "FALSE_NEGATIVE"]
        error_section = f"""
## 3. Synthetic Error Inspection ({len(metrics.errors)} test fixture misclassifications)
- **Mock False Positives:** {len(fps)}
- **Mock False Negatives:** {len(fns)}
"""
        return f"""# LLM Evaluation Benchmark Report (Pipeline Validation Only)

> [!NOTE]
> **Execution Mode:** `OFFLINE_MOCK` (Deterministic Synthetic Engine)
> **Purpose:** Pipeline & Contract Verification Only
> **Model Quality Claims:** `NONE` (Does NOT represent Gemini API production performance)

**Dataset:** `{dataset_name}` (Version: `{metrics.dataset_version}`)
**Evaluated Items:** {metrics.total_evaluated}

## 1. Pipeline Contract Verification (Synthetic Heuristic)
*The metrics below validate the evaluation harness logic and confusion matrix arithmetic using a deterministic mock heuristic. They do NOT reflect Gemini model quality.*

| Metric | Value | Category | Description |
| :--- | :--- | :--- | :--- |
| **Synthetic Accuracy** | {metrics.accuracy * 100:.2f}% | Synthetic Mock | Validation of harness pipeline |
| **Synthetic Precision** | {metrics.precision * 100:.2f}% | Synthetic Mock | Validation of harness pipeline |
| **Synthetic Recall** | {metrics.recall * 100:.2f}% | Synthetic Mock | Validation of harness pipeline |
| **Synthetic F1-Score** | {metrics.f1_score * 100:.2f}% | Synthetic Mock | Validation of harness pipeline |

### Synthetic Confusion Matrix
| Metric | Count | Description |
| :--- | :--- | :--- |
| **Mock True Positives (TP)** | {metrics.true_positives} | Mock assertions passed |
| **Mock True Negatives (TN)** | {metrics.true_negatives} | Mock assertions passed |
| **Mock False Positives (FP)** | {metrics.false_positives} | Mock assertions passed |
| **Mock False Negatives (FN)** | {metrics.false_negatives} | Mock assertions passed |

## 2. Resource Telemetry
| Measurement | Value | Status |
| :--- | :--- | :--- |
| **Prompt Tokens / Call** | Unavailable (No live API calls) | Unavailable |
| **Output Tokens / Call** | Unavailable (No live API calls) | Unavailable |
| **Total Tokens / Call** | Unavailable (No live API calls) | Unavailable |
| **HTTP Latency** | Unavailable (Offline Mock mode) | Unavailable |
| **Missing Usage Metadata** | 0 | Unavailable |
| **Model Versions Seen** | None (Offline Mock) | Unavailable |
{error_section}"""

    # Live Provider Report
    if metrics.telemetry.calls_with_tokens > 0:
        in_tok_str = f"{metrics.telemetry.mean_input_tokens} tokens"
        out_tok_str = f"{metrics.telemetry.mean_output_tokens} tokens"
        tot_tok_str = f"{metrics.telemetry.mean_total_tokens} tokens"
        tok_status = "Measured"
    else:
        in_tok_str = "Unavailable"
        out_tok_str = "Unavailable"
        tot_tok_str = "Unavailable"
        tok_status = "Unavailable"

    if metrics.telemetry.mean_latency_ms is not None:
        lat_str = (f"Mean: {metrics.telemetry.mean_latency_ms} ms | "
                   f"p50: {metrics.telemetry.p50_latency_ms} ms | "
                   f"p95: {metrics.telemetry.p95_latency_ms} ms")
        lat_status = "Measured"
    else:
        lat_str = "Unavailable"
        lat_status = "Unavailable"

    fps = [e for e in metrics.errors if e.error_type == "FALSE_POSITIVE"]
    fns = [e for e in metrics.errors if e.error_type == "FALSE_NEGATIVE"]
    ops = [e for e in metrics.errors if e.error_type == "OPERATIONAL_FAILURE"]
    fbs = [e for e in metrics.errors if e.error_type == "FALLBACK_EXCLUDED"]

    warnings_list = []
    if metrics.failures > 0:
        warnings_list.append(
            f"> [!WARNING]\n"
            f"> **Operational Failures Encountered:** {metrics.failures} candidate calls suffered operational errors "
            f"(network, timeout or provider failure) and were excluded from the confusion matrix to avoid distorting classification quality metrics."
        )
    if metrics.fallbacks > 0:
        warnings_list.append(
            f"> [!WARNING]\n"
            f"> **Fallbacks Encountered:** {metrics.fallbacks} candidate calls returned unconfigured or offline fallback responses "
            f"and were excluded from model quality metrics to prevent biased predictions."
        )
    failure_warning = ("\n" + "\n".join(warnings_list) + "\n") if warnings_list else ""

    if metrics.valid_predictions == 0:
        perf_section = """## 1. Classification Performance
> [!NOTE]
> **No Valid Model Predictions:** Zero model predictions were successfully evaluated (all samples resulted in fallbacks or operational failures). Model quality metrics cannot be established.

### Confusion Matrix
| Metric | Count | Description |
| :--- | :--- | :--- |
| **True Positives (TP)** | 0 | Real emerging artists correctly identified |
| **True Negatives (TN)** | 0 | Producers/listeners/spammers correctly filtered |
| **False Positives (FP)** | 0 | Non-artists incorrectly accepted as leads |
| **False Negatives (FN)** | 0 | Real artists incorrectly missed |"""
    else:
        perf_section = f"""## 1. Classification Performance
| Metric | Value | Category | Description |
| :--- | :--- | :--- | :--- |
| **Accuracy** | {metrics.accuracy * 100:.2f}% | Derived | $(TP + TN) / Total$ |
| **Precision** | {metrics.precision * 100:.2f}% | Derived | $TP / (TP + FP)$ - Avoids false lead ingestion |
| **Recall** | {metrics.recall * 100:.2f}% | Derived | $TP / (TP + FN)$ - Captures real emerging artists |
| **F1-Score** | {metrics.f1_score * 100:.2f}% | Derived | Harmonic mean of Precision and Recall |

### Confusion Matrix
| Metric | Count | Description |
| :--- | :--- | :--- |
| **True Positives (TP)** | {metrics.true_positives} | Real emerging artists correctly identified |
| **True Negatives (TN)** | {metrics.true_negatives} | Producers/listeners/spammers correctly filtered |
| **False Positives (FP)** | {metrics.false_positives} | Non-artists incorrectly accepted as leads |
| **False Negatives (FN)** | {metrics.false_negatives} | Real artists incorrectly missed |"""

    error_section = ""
    if ops or fbs or fps or fns:
        error_section += f"""
## 4. Error & Operational Analysis ({len(metrics.errors)} issues recorded)
"""
        if ops:
            error_section += f"""
### Operational Failures ({len(ops)})
*Requests that failed network calls, timeouts, or returned invalid responses (excluded from accuracy/confusion matrix):*
"""
            for err in ops[:5]:
                error_section += f"- **[{err.id}]** `{err.text_preview}` — Error: *{err.reason}*\n"
            if len(ops) > 5:
                error_section += f"*... and {len(ops) - 5} more operational failures (see JSON export).*\n"

        if fbs:
            error_section += f"""
### Excluded Fallbacks ({len(fbs)})
*Requests that returned unconfigured or offline fallback responses (excluded from accuracy/confusion matrix):*
"""
            for err in fbs[:5]:
                error_section += f"- **[{err.id}]** `{err.text_preview}` — Reason: *{err.reason}*\n"
            if len(fbs) > 5:
                error_section += f"*... and {len(fbs) - 5} more fallbacks (see JSON export).*\n"

        if fps:
            error_section += f"""
### False Positives (FP: {len(fps)})
*Expected Negative (Non-promo/producer/listener), Model Predicted Positive:*
"""
            for err in fps[:5]:
                error_section += f"- **[{err.id}]** `{err.text_preview}` — Reason: *{err.reason}*\n"
            if len(fps) > 5:
                error_section += f"*... and {len(fps) - 5} more false positives (see JSON export).*\n"

        if fns:
            error_section += f"""
### False Negatives (FN: {len(fns)})
*Expected Positive (Artist promotion), Model Predicted Negative:*
"""
            for err in fns[:5]:
                error_section += f"- **[{err.id}]** `{err.text_preview}` — Reason: *{err.reason}*\n"
            if len(fns) > 5:
                error_section += f"*... and {len(fns) - 5} more false negatives (see JSON export).*\n"
    else:
        error_section = "\n## 4. Error Analysis\n*Zero classification errors recorded across dataset.*\n"

    return f"""# LLM Evaluation Benchmark Report

> [!IMPORTANT]
> **Execution Mode:** `LIVE_PROVIDER` (Production Gemini API)
> **Model Requested:** `gemini-2.5-flash`
{failure_warning}
**Dataset:** `{dataset_name}` (Version: `{metrics.dataset_version}`)
**Total Dataset Samples:** {metrics.total_samples}
**Valid Model Predictions:** {metrics.valid_predictions}
**Excluded Fallbacks:** {metrics.fallbacks}
**Excluded Operational Failures:** {metrics.failures}

{perf_section}

## 2. Operational Reliability
- **Execution Failures:** {metrics.failures} ({metrics.failure_rate * 100:.2f}%)
- **Fallbacks Triggered:** {metrics.fallbacks} ({metrics.fallback_rate * 100:.2f}%)
- **Network Retries:** {metrics.telemetry.total_retries}

## 3. Telemetry & Resource Usage
| Measurement | Value | Status |
| :--- | :--- | :--- |
| **Prompt Tokens / Call** | {in_tok_str} | {tok_status} |
| **Output Tokens / Call** | {out_tok_str} | {tok_status} |
| **Total Tokens / Call** | {tot_tok_str} | {tok_status} |
| **HTTP Latency** | {lat_str} | {lat_status} |
| **Missing Usage Metadata** | {metrics.telemetry.missing_usage_metadata_count} | Measured |
| **Model Versions Seen** | {', '.join(metrics.telemetry.model_versions_seen) or 'None'} | Measured |
{error_section}"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate LLM Talent Scouting Performance")
    parser.add_argument("--dataset", type=str, default="eval/dataset/talent_classification_v1.jsonl", help="Path to labeled dataset (.jsonl or .csv)")
    parser.add_argument("--prompt-file", type=str, default="eval/prompts/talent_scout_v1.txt", help="Prompt template")
    parser.add_argument("--mock", action="store_true", help="Use mock evaluation without live API keys")
    parser.add_argument("--output-json", type=str, default=None, help="Optional path to export JSON metrics")
    parser.add_argument("--output-markdown", type=str, default=None, help="Optional path to export Markdown report")
    args = parser.parse_args()

    metrics = run_evaluation(
        dataset_path=args.dataset,
        prompt_template_path=args.prompt_file,
        use_mock=args.mock
    )

    report_md = render_markdown_report(metrics, args.dataset)
    print(report_md)

    if args.output_json:
        os.makedirs(os.path.dirname(os.path.abspath(args.output_json)), exist_ok=True)
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(metrics.to_dict(), f, indent=2)
        logger.info("Saved JSON metrics to: %s", args.output_json)

    if args.output_markdown:
        os.makedirs(os.path.dirname(os.path.abspath(args.output_markdown)), exist_ok=True)
        with open(args.output_markdown, "w", encoding="utf-8") as f:
            f.write(report_md)
        logger.info("Saved Markdown report to: %s", args.output_markdown)


if __name__ == "__main__":
    main()
