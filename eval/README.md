# LLM Evaluation Framework & Benchmark

Framework de validação quantitativa da extração semântica e classificação de talentos via Gemini 2.5 Flash no pipeline BeatMatchAI.

---

## 1. Estrutura dos Arquivos
```text
eval/
├── README.md                   # Este guia de rotulação e execução
├── dataset_template.csv        # Template CSV para rotular até ~200 exemplos
├── evaluate.py                 # Motor de avaliação e cálculo de métricas
└── prompts/
    └── talent_scout_v1.txt     # Template do prompt versionado
```

---

## 2. Como Rotular o Dataset (`dataset_template.csv`)
Para garantir ground truth fidedigno sem viés artificial, o dataset deve ser rotulado com dados reais coletados de comentários do YouTube ou tweets.

### Colunas Obrigatórias:
* `id`: Identificador incremental único (ex: `1, 2, 3...`).
* `text`: Texto bruto exato do comentário ou tweet social.
* `source`: Origem do dado (`youtube_comment` ou `twitter_search`).
* `ground_truth_is_artist`: **`TRUE`** se for um vocalista/rapper/artista musical promovendo o próprio trabalho; **`FALSE`** caso contrário (produtores, beatmakers, loopmakers, spammers, elogios de ouvintes).
* `ground_truth_artist_name`: Nome artístico extraído, se identificável; deixe vazio caso contrário.
* `notes`: Contexto opcional ou motivo da classificação.

---

## 3. Como Executar a Avaliação

### Modo Simulado / Testes Rápidos (Sem consumir cota da API):
```bash
python eval/evaluate.py --dataset eval/dataset_template.csv --mock
```

### Modo Benchmark Real (Utiliza `GEMINI_API_KEY` do `.env`):
```bash
python eval/evaluate.py \
  --dataset eval/dataset_template.csv \
  --prompt-file eval/prompts/talent_scout_v1.txt \
  --output-json eval/results.json \
  --output-markdown eval/REPORT.md
```

---

## 4. Métricas Computadas
1. **Precisão (Precision):** $\frac{TP}{TP + FP}$ — Evita falsos positivos na base do CRM.
2. **Revocação (Recall):** $\frac{TP}{TP + FN}$ — Garante que artistas promissores não sejam perdidos.
3. **F1-Score:** Média harmônica entre precisão e revocação.
4. **Taxa de Falha:** % de chamadas abortadas por limites de rede/HTTP.
5. **Taxa de Fallback:** % de chamadas que acionaram retorno determinístico de contingência.
6. **Custo Estimado por 1.000 Inferências:** Baseado na tabela oficial de precificação do Gemini 2.5 Flash ($0.075 / 1M tokens de entrada e $0.30 / 1M tokens de saída).
