# packages/shared-types

Pydantic models are the Single Source of Truth for cross-runtime shapes. Python
consumes them directly; TypeScript consumes the generated `.ts` produced by
`scripts/export.py`.

## Pipeline

```
python/polaris_shared_types/*.py          # Pydantic models (SSOT)
        |
        v
scripts/export.py                          # imports each model
        |
        v
typescript/schema.json                     # intermediate JSON Schema
        |
        v
typescript/polaris-shared-types.ts         # consumed by Next.js apps
```

## Generator choice — ADR-011

`json-schema-to-typescript` (v15.x, JSON Schema → TS) is the canonical generator.
Invoked via `pnpm dlx` from `scripts/export.py`. The original ADR-011 had picked
`ts-json-schema-generator` by mistake — that tool transforms the opposite way
(TS → JSON Schema) and is not a substitute. See ADR-011 in
`../../docs/Polaris V2/09_DECISION_LOG.md` for the correction trail.

## Run

```bash
# From repo root
make export-types
# Or directly
uv run python packages/shared-types/scripts/export.py
```

## Tests

```bash
uv run pytest packages/shared-types/python/tests -v
```

CI (PR-4) asserts `git diff --exit-code` after running export — no drift between
Pydantic models and committed TS file.
