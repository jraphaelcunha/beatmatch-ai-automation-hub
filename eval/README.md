# LLM Evaluation Framework & Synthetic Evaluation Dataset v1

Framework de validação quantitativa e telemetria observável da extração semântica e classificação de talentos no pipeline BeatMatchAI.

---

## 1. Estrutura dos Arquivos
```text
eval/
├── README.md                                 # Este guia de metodologia, schema e execução
├── dataset/
│   └── talent_classification_v1.jsonl        # Synthetic Evaluation Dataset v1 (100 casos sintéticos anotados)
├── dataset_template.csv                      # Template CSV legado para anotação externa
├── evaluate.py                               # Motor de benchmark, cálculo de métricas e error analysis
├── prompts/
│   └── talent_scout_v1.txt                   # Template do prompt versionado
└── reports/                                  # Diretório de relatórios gerados (Markdown e JSON)
    ├── mock_benchmark_report.md
    └── mock_benchmark_report.json
```

---

## 2. O Que o Benchmark Mede
O benchmark avalia a capacidade do classificador de distinguir **vocalistas e artistas musicais emergentes promovendo o próprio trabalho** de outros perfis comuns em comentários e redes sociais:
* **Classe Positiva (`TRUE`):** Cantores, rappers, compositores, vocalistas divulgando singles, prévias, EPs ou solicitando feedback musical.
* **Classe Negativa (`FALSE`):** Produtores musicais, beatmakers divulgando beats/drum kits, spammers de playlists, ouvintes comuns reagindo ao vídeo e engenheiros de áudio.

---

## 3. Synthetic Evaluation Dataset v1 (`eval/dataset/talent_classification_v1.jsonl`)

O Synthetic Evaluation Dataset v1 contém **100 casos sintéticos anotados e balanceados**, criados para validar a execução do avaliador, os contratos de dados e o cálculo das métricas offline, não constituindo um conjunto de referência real validado para comprovar a qualidade do Gemini em produção:
* **50 casos positivos sintéticos:** Diversidade de subgêneros (R&B, Trap, Drill, Indie Pop, Boom Bap, Soul), formatos (curtos, longos, prévias, pedidos de feat) em inglês e português.
* **50 casos negativos sintéticos:** Beatmakers, spammers de playlists, elogios casuais, debates de ouvintes, serviços de mixagem e reações com ruído textual.

### Schema do Dataset:
```json
{
  "id": "tc_001",
  "text": "Texto bruto do comentário ou post",
  "expected_is_artist_promotion": true,
  "expected_artist_name": "Luna Ray",
  "category": "vocalist_self_promo_direct",
  "difficulty": "easy"
}
```

* `id`: Identificador único (`tc_001` a `tc_100`).
* `text`: Texto de entrada avaliado.
* `expected_is_artist_promotion`: Booleano determinístico (`true` ou `false`).
* `expected_artist_name`: Nome artístico esperado ou `null` quando não identificável no texto.
* `category`: Categoria de negócio (ex: `vocalist_self_promo_direct`, `beatmaker_producer_promo`, `general_listener_praise`, etc.).
* `difficulty`: Nível de ambiguidade (`easy`, `medium`, `hard`).

---

## 4. Separação Estrita: Mock vs. Telemetria Real

O benchmark aplica separação rigorosa de camadas e não implementa cálculos financeiros de custo:

1. **Ground Truth:** Fonte de verdade anotada previamente no dataset. Nunca é inferida da saída do modelo.
2. **Modo Simulado (`OFFLINE_MOCK`):**
   * Finalidade exclusiva: Verificação de pipeline e contratos de software em CI/CD e ambientes offline.
   * Classificação determinística sintética baseada em regras locais.
   * **Reivindicações sobre qualidade do modelo:** `NENHUMA` (`Model Quality Claims: NONE`).
   * Não representa de forma alguma a acurácia, precisão ou recall do Gemini em produção.
   * Telemetria de rede e tokens do provider são categorizados como `Unavailable`.
3. **Telemetria Real (`LIVE_PROVIDER`):**
   * Coletada exclusivamente do provider através do contrato `GeminiTelemetry` implementado em `src/utils/gemini_classifier.py`.
   * Inclui latência cronometrada (`time.perf_counter()`), tokens reais (`usageMetadata: promptTokenCount, candidatesTokenCount, totalTokenCount`), versão real servida (`modelVersion`) e número de tentativas (`attempts`).
   * Valores ausentes ou não retornados pela API permanecem `None` e são rotulados como `Unavailable`.
   * **Zero estimativas silenciosas:** Heurísticas de caracteres nunca são usadas para preencher tokens de provider.

---

## 5. Como Executar

### A. Modo Simulação / Teste de Contrato (Offline, sem consumir API):
```bash
python eval/evaluate.py --dataset eval/dataset/talent_classification_v1.jsonl --mock --output-json eval/reports/mock_benchmark_report.json --output-markdown eval/reports/mock_benchmark_report.md
```

### B. Modo Benchmark Real (Requer `GEMINI_API_KEY` configurada no `.env`):
```bash
python eval/evaluate.py --dataset eval/dataset/talent_classification_v1.jsonl --output-json eval/reports/live_benchmark_v1.json --output-markdown eval/reports/live_benchmark_v1.md
```

> [!NOTE]
> Se o comando for executado no modo real sem uma `GEMINI_API_KEY` válida configurada, o benchmark interrompe a execução com status `UNAVAILABLE_NO_KEY`, recusando-se a publicar métricas inventadas ou simuladas como se fossem reais.

---

## 6. Métricas Computadas
1. **Acurácia (Accuracy):** $\frac{TP + TN}{Total}$ — Proporção geral de classificações corretas.
2. **Precisão (Precision):** $\frac{TP}{TP + FP}$ — Proporção de leads aceitos que são de fato artistas (evita poluição do CRM).
3. **Revocação (Recall):** $\frac{TP}{TP + FN}$ — Proporção de artistas reais capturados (evita perda de oportunidades).
4. **F1-Score:** Média harmônica entre precisão e revocação.
5. **Matriz de Confusão:** Contagem explícita de $TP$, $TN$, $FP$ e $FN$.
6. **Error Analysis:** Registro estruturado de todos os Falsos Positivos e Falsos Negativos com justificativa do modelo e texto analisado.
7. **Latência HTTP:** Média, mediana (p50), p95 e p99 (quando amostragem for suficiente em modo online).

---

## 7. Limitações Conhecidas
* O benchmark mede qualidade semântica sobre textos estáticos; não avalia o canal do YouTube ou a página do artista no Spotify.
* A telemetria real de tokens depende do retorno de `usageMetadata` pela Google Generative Language API em chamadas online.
* Não há cálculo de custo financeiro nesta etapa: o foco é estritamente qualidade, latência e observabilidade observável de infraestrutura.
