# apps/ingestion-worker — SOFT-RETIRED (kept as forward-compat stub)

> **Deprecated.** The real ingestion pipeline (RAG corpus, embeddings) lives in
> a separate project, not in this monorepo — per the maintainer's ADR-057
> (private decision log).
>
> This `apps/ingestion-worker/` folder is kept in the monorepo as a
> forward-compatibility stub:
>
> - The workspace member entry in root `pyproject.toml` remains so
>   `make verify` / CI / mypy keep working without surprise removals.
> - The `polaris-ingest --help` entry-point still works (argparse stub; does
>   nothing useful).
> - If a future need arises for a monorepo-resident worker that is genuinely
>   API-side, this slot is available.
>
> **Do not put the desktop ingestion code here.**

## Usage (stub only)

```bash
uv run polaris-ingest --help
uv run polaris-ingest noop          # prints + exits 0
uv run polaris-ingest version
```
