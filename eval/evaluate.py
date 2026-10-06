"""
Evaluation framework for LLM-based talent classification.
Measures Precision, Recall, F1-Score, Failure Rate, Fallback Rate, and Estimated Token Costs.
"""

import argparse
import csv
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
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

# Official Gemini 2.5 Flash pricing parameters (USD per 1M tokens)
PRICE_INPUT_PER_1M_USD = 0.075
PRICE_OUTPUT_PER_1M_USD = 0.30


@dataclass
class EvaluationMetrics:
    total_evaluated: int
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int
    precision: float
    recall: float
    f1_score: float
    accuracy: float
    failures: int
    failure_rate: float
    fallbacks: int
    fallback_rate: float
    avg_input_tokens: float
    avg_output_tokens: float
    estimated_cost_per_1k_calls_usd: float


def estimate_tokens(text: str) -> int:
    """Heuristic approximation: ~4 characters per token for English/multilingual text."""
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


def mock_classify(text: str) -> TalentClassificationResult:
    """Deterministic mock classifier for offline validation and CI test runs."""
    lower_text = text.lower()
    is_artist = (
        "single" in lower_text
        or "album" in lower_text
        or "check out my" in lower_text
        or "link in bio" in lower_text
    ) and not ("drum kit" in lower_text or "prod by" in lower_text or "loops" in lower_text)

    artist_name = "Marcus Kane" if "marcus kane" in lower_text else None
    return TalentClassificationResult(
        is_artist_promotion=is_artist,
        artist_name=artist_name,
        reason="Mock deterministic classification",
        confidence=0.95 if is_artist else 0.85
    )


def run_evaluation(
    dataset_path: str,
    prompt_template_path: str | None = None,
    use_mock: bool = False
) -> EvaluationMetrics:
    """Executes evaluation across benchmark dataset and computes statistical quality metrics."""
    if not os.path.exists(dataset_path):
        raise FileNotFoundError(f"Evaluation dataset not found: {dataset_path}")

    prompt_template = ""
    if prompt_template_path and os.path.exists(prompt_template_path):
        with open(prompt_template_path, encoding="utf-8") as f:
            prompt_template = f.read()

    rows_to_evaluate: list[dict[str, Any]] = []
    with open(dataset_path, encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            # Skip template instruction placeholders
            text = row.get("text", "").strip()
            if not text or text.startswith("[PREENCHER]"):
                continue
            gt_val = parse_ground_truth_bool(row.get("ground_truth_is_artist", ""))
            if gt_val is None:
                continue
            rows_to_evaluate.append({
                "id": row.get("id", ""),
                "text": text,
                "ground_truth": gt_val,
                "ground_truth_name": row.get("ground_truth_artist_name", "").strip()
            })

    if not rows_to_evaluate:
        logger.warning("No labeled evaluation records found in dataset: %s", dataset_path)
        return EvaluationMetrics(
            total_evaluated=0, true_positives=0, false_positives=0,
            true_negatives=0, false_negatives=0, precision=0.0,
            recall=0.0, f1_score=0.0, accuracy=0.0, failures=0,
            failure_rate=0.0, fallbacks=0, fallback_rate=0.0,
            avg_input_tokens=0.0, avg_output_tokens=0.0,
            estimated_cost_per_1k_calls_usd=0.0
        )

    tp = fp = tn = fn = failures = fallbacks = 0
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

            if pred.reason.startswith("Unconfigured GEMINI_API_KEY") or pred.reason.startswith("Fallback"):
                fallbacks += 1

            if pred.is_artist_promotion and gt:
                tp += 1
            elif pred.is_artist_promotion and not gt:
                fp += 1
            elif not pred.is_artist_promotion and not gt:
                tn += 1
            else:
                fn += 1

        except Exception as err:
            logger.error("Error during evaluation of record ID %s: %s", item.get("id"), err)
            failures += 1

    total = len(rows_to_evaluate)
    precision = (tp / (tp + fp)) if (tp + fp) > 0 else 0.0
    recall = (tp / (tp + fn)) if (tp + fn) > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    accuracy = ((tp + tn) / total) if total > 0 else 0.0

    avg_input_tokens = estimate_tokens(" " * (total_input_chars // max(1, total)))
    avg_output_tokens = estimate_tokens(" " * (total_output_chars // max(1, total)))

    cost_input_1k = 1000 * (avg_input_tokens / 1_000_000) * PRICE_INPUT_PER_1M_USD
    cost_output_1k = 1000 * (avg_output_tokens / 1_000_000) * PRICE_OUTPUT_PER_1M_USD
    cost_per_1k = cost_input_1k + cost_output_1k

    return EvaluationMetrics(
        total_evaluated=total,
        true_positives=tp,
        false_positives=fp,
        true_negatives=tn,
        false_negatives=fn,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1_score=round(f1, 4),
        accuracy=round(accuracy, 4),
        failures=failures,
        failure_rate=round(failures / total if total > 0 else 0.0, 4),
        fallbacks=fallbacks,
        fallback_rate=round(fallbacks / total if total > 0 else 0.0, 4),
        avg_input_tokens=round(avg_input_tokens, 1),
        avg_output_tokens=round(avg_output_tokens, 1),
        estimated_cost_per_1k_calls_usd=round(cost_per_1k, 6)
    )


def render_markdown_report(metrics: EvaluationMetrics, dataset_name: str) -> str:
    """Generates GitHub Flavored Markdown report summarizing LLM benchmark performance."""
    return f"""# LLM Evaluation Benchmark Report

**Dataset:** `{dataset_name}`
**Evaluated Items:** {metrics.total_evaluated}

## 1. Classification Performance
| Metric | Value | Description |
| :--- | :--- | :--- |
| **Precision** | {metrics.precision * 100:.2f}% | $TP / (TP + FP)$ - Avoids false lead ingestion |
| **Recall** | {metrics.recall * 100:.2f}% | $TP / (TP + FN)$ - Captures real emerging artists |
| **F1-Score** | {metrics.f1_score * 100:.2f}% | Harmonic mean of Precision and Recall |
| **Accuracy** | {metrics.accuracy * 100:.2f}% | Overall correct classification ratio |

### Confusion Matrix
- **True Positives (TP):** {metrics.true_positives}
- **False Positives (FP):** {metrics.false_positives}
- **True Negatives (TN):** {metrics.true_negatives}
- **False Negatives (FN):** {metrics.false_negatives}

## 2. Operational Reliability
- **Failures:** {metrics.failures} ({metrics.failure_rate * 100:.2f}%)
- **Fallbacks:** {metrics.fallbacks} ({metrics.fallback_rate * 100:.2f}%)

## 3. Cost & Latency Projections (Gemini 2.5 Flash)
- **Average Prompt Tokens:** ~{metrics.avg_input_tokens} tokens / call
- **Average Output Tokens:** ~{metrics.avg_output_tokens} tokens / call
- **Estimated Cost per 1,000 Inferences:** `${metrics.estimated_cost_per_1k_calls_usd:.6f} USD`
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate LLM Talent Scouting Performance")
    parser.add_argument("--dataset", type=str, default="eval/dataset_template.csv", help="Path to labeled CSV")
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
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(asdict(metrics), f, indent=2)
        logger.info("Saved JSON metrics to: %s", args.output_json)

    if args.output_markdown:
        with open(args.output_markdown, "w", encoding="utf-8") as f:
            f.write(report_md)
        logger.info("Saved Markdown report to: %s", args.output_markdown)


if __name__ == "__main__":
    main()
