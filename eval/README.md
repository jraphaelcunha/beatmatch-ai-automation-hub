# LLM Evaluation Framework & Synthetic Evaluation Dataset v1

> **[ 🇧🇷 Ler em Português ](README.pt-br.md)**

Framework for quantitative validation and observable telemetry of semantic extraction and talent classification in the BeatMatch AI pipeline.

---

## 1. Directory Structure
```text
eval/
├── README.md                                 # This methodology, schema, and execution guide
├── README.pt-br.md                           # Portuguese version of this guide
├── dataset/
│   └── talent_classification_v1.jsonl        # Synthetic Evaluation Dataset v1 (100 annotated synthetic cases)
├── dataset_template.csv                      # Legacy CSV template for external annotation
├── evaluate.py                               # Benchmark engine, metric calculation, and error analysis
├── prompts/
│   └── talent_scout_v1.txt                   # Versioned prompt template
└── reports/                                  # Generated report directory (Markdown and JSON)
    ├── mock_benchmark_report.md
    └── mock_benchmark_report.json
```

---

## 2. What the Benchmark Evaluates
The benchmark evaluates the classifier's ability to distinguish **emerging vocalists and musical artists promoting their own work** from other common profiles in comments and social networks:
* **Positive Class (`TRUE`):** Singers, rappers, songwriters, and vocalists promoting singles, teasers, EPs, or requesting musical feedback.
* **Negative Class (`FALSE`):** Music producers, beatmakers promoting beats/drum kits, playlist spammers, general listeners reacting to videos, and audio engineers.

---

## 3. Synthetic Evaluation Dataset v1 (`eval/dataset/talent_classification_v1.jsonl`)

The Synthetic Evaluation Dataset v1 contains **100 annotated and balanced synthetic cases**, created to validate evaluator execution, data contracts, and offline metric calculation, without constituting a real-world reference benchmark to prove Gemini model quality in production:
* **50 synthetic positive cases:** Subgenre diversity (R&B, Trap, Drill, Indie Pop, Boom Bap, Soul), varied formats (short, long, teasers, collaboration requests) in English and Portuguese.
* **50 synthetic negative cases:** Beatmakers, playlist spammers, casual praise, listener discussions, mixing services, and noisy textual reactions.

### Dataset Schema:
```json
{
  "id": "tc_001",
  "text": "Raw text of the comment or post",
  "expected_is_artist_promotion": true,
  "expected_artist_name": "Luna Ray",
  "category": "vocalist_self_promo_direct",
  "difficulty": "easy"
}
```

* `id`: Unique identifier (`tc_001` to `tc_100`).
* `text`: Evaluated raw input text.
* `expected_is_artist_promotion`: Deterministic boolean (`true` or `false`).
* `expected_artist_name`: Expected artist stage name or `null` when not identifiable in text.
* `category`: Business category (e.g., `vocalist_self_promo_direct`, `beatmaker_producer_promo`, `general_listener_praise`, etc.).
* `difficulty`: Ambiguity level (`easy`, `medium`, `hard`).

---

## 4. Strict Separation: Mock vs. Live Telemetry

The benchmark enforces strict layer separation and does not implement financial cost calculations:

1. **Ground Truth:** Pre-annotated ground truth within the dataset. Never inferred from model outputs.
2. **Mock Mode (`OFFLINE_MOCK`):**
   * Exclusive purpose: Pipeline and software contract verification in CI/CD and offline environments.
   * Deterministic synthetic classification based on local heuristics.
   * **Model Quality Claims:** `NONE` (`Model Quality Claims: NONE`).
   * In no way represents Gemini accuracy, precision, or recall in production.
   * Network telemetry and provider tokens are categorized as `Unavailable`.
3. **Live Telemetry (`LIVE_PROVIDER`):**
   * Collected exclusively from the provider via the `GeminiTelemetry` contract implemented in `src/utils/gemini_classifier.py`.
   * Includes timed latency (`time.perf_counter()`), actual token counts reported by the provider (`usageMetadata: promptTokenCount, candidatesTokenCount, totalTokenCount`), actual served model version (`modelVersion`), and retry attempts (`attempts`).
   * Missing values or data omitted by the API remain `None` and are labeled as `Unavailable`.
   * **Zero silent estimations:** Character-count heuristics are never used to synthesize provider token metrics.

---

## 5. How to Run

### A. Mock Simulation / Contract Testing Mode (Offline, no API quota consumed):
```bash
python eval/evaluate.py --dataset eval/dataset/talent_classification_v1.jsonl --mock --output-json eval/reports/mock_benchmark_report.json --output-markdown eval/reports/mock_benchmark_report.md
```

### B. Live Benchmark Mode (Requires `GEMINI_API_KEY` configured in `.env`):
```bash
python eval/evaluate.py --dataset eval/dataset/talent_classification_v1.jsonl --output-json eval/reports/live_benchmark_v1.json --output-markdown eval/reports/live_benchmark_v1.md
```

> [!NOTE]
> When executed in live mode without a valid `GEMINI_API_KEY` configured, the benchmark aborts execution with status `UNAVAILABLE_NO_KEY`, refusing to publish synthesized or mocked numbers as live performance metrics.

---

## 6. Computed Metrics
1. **Accuracy:** $\frac{TP + TN}{Total}$ — Overall proportion of correct classifications.
2. **Precision:** $\frac{TP}{TP + FP}$ — Proportion of accepted leads that are genuine artists (prevents CRM pollution).
3. **Recall:** $\frac{TP}{TP + FN}$ — Proportion of genuine artists successfully captured (prevents lost opportunities).
4. **F1-Score:** Harmonic mean of precision and recall.
5. **Confusion Matrix:** Explicit count of $TP$, $TN$, $FP$, and $FN$.
6. **Error Analysis:** Structured log of all False Positives and False Negatives with model reasoning and analyzed text.
7. **HTTP Latency:** Mean, median (p50), p95, and p99 (when sample size is sufficient in live mode).

---

## 7. Known Limitations
* The benchmark evaluates semantic classification on static text; it does not query YouTube channel metadata or Spotify artist catalog pages.
* Live token telemetry depends on `usageMetadata` returned by the Google Generative Language API during online requests.
* Financial cost estimates are not calculated at this stage: focus is strictly on classification quality, latency, and observable infrastructure telemetry.
