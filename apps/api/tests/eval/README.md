# RAG eval harness

A small, repeatable way to **measure** the bot's retrieval quality instead of
eyeballing chunk previews. It runs the REAL tier-aware retrieval path
(`services.rag.search_hybrid`) over a hand-curated golden set and reports
**recall@k** and **MRR**, broken down by language.

This is the **gate** for every future chunking / overlap / threshold change:
run the eval, ship the change, re-run — the delta proves whether it helped.

## Files

| File                                     | What                                                                 |
| ---------------------------------------- | -------------------------------------------------------------------- |
| `golden.jsonl`                           | the golden set — one JSON object per line (hand-edited)              |
| `../../scripts/rag_eval.py`              | the runner — load → embed → `search_hybrid` → score → report         |
| `../../polaris_api/services/rag_eval.py` | pure scoring helpers (`hit_rank`, `recall_at_k`, `mrr`, `summarize`) |
| `../../tests/test_rag_eval.py`           | unit tests for the scoring helpers (no DB)                           |

## Golden set format

Each line is one question:

```json
{
  "q": "is there a free trial?",
  "expect_any": ["free", "trial"],
  "expect_page": "/pricing/",
  "lang": "en"
}
```

- `q` — the customer question (required).
- `expect_any` — keywords; **any** present (lowercased substring) in a retrieved
  chunk's `content` counts as a hit (required, non-empty).
- `expect_page` — optional. When set, the hit's `source_name` must also contain
  this fragment (an extra constraint on top of the keyword match).
- `lang` — `en` / `ms` / `zh` … drives the per-language breakdown.

**Curating keywords:** specific enough to be meaningful (a price, a feature
name) but not so exact they break on phrasing. Matching is lowercased.

## Run it

```bash
cd polaris   # repo root
uv run python apps/api/scripts/rag_eval.py --tenant <tenant-uuid> [--top-k 5] [--lang en]
```

Needs `POLARIS_DB_URL_ANON` + `OPENROUTER_API_KEY` in `.env` (run via `uv run`
so the venv + env load). With a placeholder OpenRouter key, embeddings degrade
to a zero vector and search runs on the BM25 leg only (still works, lower vector
recall). If the tenant has 0 chunks, the runner fails fast.

Sample output:

```
RAG eval — tenant <uuid> — 36 questions
  recall@5: 0.81   MRR: 0.67
  by lang:  en 0.90 (26)   ms 0.62 (5)   zh 0.50 (5)
  FAIL (6):
    [en] 'is there a free trial?' (expected any of ['free', 'trial'] — not retrieved)
    …
```

It **always exits 0** — it's a report, not a CI gate.

## Read the score

- **recall@k** — fraction of questions whose expected fact was retrieved within
  the top-k. The headline number.
- **MRR** — mean reciprocal rank; rewards retrieving the right chunk _higher_.
- **by lang** — the multilingual retrieval check. A low `ms`/`zh` recall vs `en`
  is the signal to look at chunking / embedding for those languages.
- **FAIL list** — spot-check a couple by hand; a "fail" can mean a genuinely
  missed chunk OR an `expect_any` keyword that's too strict.

## Validate the helpers (no DB)

```bash
uv run pytest apps/api/tests/test_rag_eval.py -q
uv run ruff check apps/api/scripts/rag_eval.py apps/api/polaris_api/services/rag_eval.py apps/api/tests/test_rag_eval.py
uv run mypy apps/api/polaris_api/services/rag_eval.py
```
