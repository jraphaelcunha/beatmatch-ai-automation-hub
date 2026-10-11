# LLM Evaluation Benchmark Report (Pipeline Validation Only)

> [!NOTE]
> **Execution Mode:** `OFFLINE_MOCK` (Deterministic Synthetic Engine)
> **Purpose:** Pipeline & Contract Verification Only
> **Model Quality Claims:** `NONE` (Does NOT represent Gemini API production performance)

**Dataset:** `eval/dataset/talent_classification_v1.jsonl` (Version: `v1`)
**Evaluated Items:** 100

## 1. Pipeline Contract Verification (Synthetic Heuristic)
*The metrics below validate the evaluation harness logic and confusion matrix arithmetic using a deterministic mock heuristic. They do NOT reflect Gemini model quality.*

| Metric | Value | Category | Description |
| :--- | :--- | :--- | :--- |
| **Synthetic Accuracy** | 79.00% | Synthetic Mock | Validation of harness pipeline |
| **Synthetic Precision** | 89.19% | Synthetic Mock | Validation of harness pipeline |
| **Synthetic Recall** | 66.00% | Synthetic Mock | Validation of harness pipeline |
| **Synthetic F1-Score** | 75.86% | Synthetic Mock | Validation of harness pipeline |

### Synthetic Confusion Matrix
| Metric | Count | Description |
| :--- | :--- | :--- |
| **Mock True Positives (TP)** | 33 | Mock assertions passed |
| **Mock True Negatives (TN)** | 46 | Mock assertions passed |
| **Mock False Positives (FP)** | 4 | Mock assertions passed |
| **Mock False Negatives (FN)** | 17 | Mock assertions passed |

## 2. Resource Telemetry
| Measurement | Value | Status |
| :--- | :--- | :--- |
| **Prompt Tokens / Call** | Unavailable (No live API calls) | Unavailable |
| **Output Tokens / Call** | Unavailable (No live API calls) | Unavailable |
| **Total Tokens / Call** | Unavailable (No live API calls) | Unavailable |
| **HTTP Latency** | Unavailable (Offline Mock mode) | Unavailable |
| **Missing Usage Metadata** | 0 | Unavailable |
| **Model Versions Seen** | None (Offline Mock) | Unavailable |

## 3. Synthetic Error Inspection (21 test fixture misclassifications)
- **Mock False Positives:** 4
- **Mock False Negatives:** 17
