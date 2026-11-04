# Eval YAML schema — Phase 4 PR-5

Two YAML formats live in this directory:

## `intent_classification.yaml`

```yaml
version: 1 # bump on breaking schema change
suite: intent_classification
threshold: 0.9 # pass-rate floor for CI gate
cases:
  - id: simple-001 # unique per suite; "<tier>-<NNN>" convention
    input: "what time you open?"
    expected: simple # one of {simple, medium, hard, handoff}
    language: en # one of {en, ms, zh-cn}
```

`threshold` is the per-suite pass-rate that gates a PR touching
`services/llm.py`. Per plan decision-lock 4, the Phase 4 baseline is
**0.9** for intent classification. Promotes to ADR-058 only after 2
consecutive nightly runs hold the bar.

## `rag_queries.yaml`

```yaml
version: 1
suite: rag_queries
threshold: 0.8 # top-1 recall floor for CI gate
cases:
  - id: rag-001
    query: "store hours"
    seeded_chunks:
      - id: chunk-a
        content: "We open 9am-9pm Mon-Sun."
        source: faq.md
      - id: chunk-b
        content: "Free delivery above RM50."
        source: shipping.md
    expected_top_chunk: chunk-a
```

`expected_top_chunk` is the chunk id that should rank first after
hybrid retrieval. `seeded_chunks` are stand-in references used by
the runner when seeding a test KB (Phase 5+ integration runs) or
asserted against in mock-mode (CI). Per plan decision-lock 4, the
Phase 4 baseline is **0.8** for top-1 recall.

## Adding a case

1. Append to the appropriate YAML with a new monotonic id.
2. For RAG cases, the `seeded_chunks` list MUST include the
   `expected_top_chunk` id (else the case is unsatisfiable).
3. CI's `test_eval_regression.py` validates schema + minimum case
   count (15 intent / 10 RAG per Phase 4 exit criterion 2). It does
   NOT exercise live LLM/RAG — that's `make eval` (manual).
