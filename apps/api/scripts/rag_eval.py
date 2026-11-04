"""RAG eval harness — golden Q&A → retrieval recall@k / MRR.

Runs the REAL tier-aware retrieval path (``services.rag.search_hybrid``) over a
hand-curated golden set (``apps/api/tests/eval/golden.jsonl``) and reports
**recall@k** and **MRR**, broken down by language. This is the keystone the
chunk-quality gap review flagged: measurement first, then tuning. Re-run it
after any chunking / overlap / threshold change and keep what raises recall.

Usage::

    uv run python apps/api/scripts/rag_eval.py --tenant <uuid> [--top-k 5] \
        [--lang en] [--golden apps/api/tests/eval/golden.jsonl]

What it does, per the plan:

  * Opens ONE anon-pool connection (``POLARIS_DB_URL_ANON``) and runs everything
    inside a single transaction so ``set_config('app.current_tenant_id', …,
    true)`` (= ``SET LOCAL``) holds for every query — RLS scopes the search to
    the tenant.
  * For each golden row: ``emb = await embed_query(q)`` →
    ``rag.search_hybrid(conn=conn, tenant_id=…, query_text=q,
    query_embedding=emb, top_k=k)`` → ``hit_rank(hits, expect_any,
    expect_page)`` (pure scorer in ``services.rag_eval``).
  * Prints ``summarize(...)`` (overall + per-lang recall@k / MRR) and the FAIL
    list. **Always exits 0** — it's a report, not a CI gate.

Requirements (run via ``uv run`` so the venv + ``.env`` load):
  * ``POLARIS_DB_URL_ANON`` — the NOBYPASSRLS DSN (RLS is the tenant scope).
  * ``OPENROUTER_API_KEY`` — ``embed_query`` calls OpenRouter; with a placeholder
    key it returns a zero vector and search degrades to the BM25 leg (still
    runnable, lower vector recall).

If the tenant has 0 ``knowledge_chunks``, the script fails fast with a clear
message rather than reporting a meaningless recall of 0.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from uuid import UUID

import asyncpg

# Direct-script invocation needs the repo root on sys.path so
# `from polaris_api.services ...` resolves outside of pytest.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from polaris_api.config import get_settings  # noqa: E402 — after sys.path tweak
from polaris_api.db.pools import _ssl_for_dsn  # noqa: E402
from polaris_api.services import rag, rag_eval  # noqa: E402
from polaris_api.services.embed import embed_query  # noqa: E402

logger = logging.getLogger(__name__)

_DEFAULT_GOLDEN = (
    _REPO_ROOT / "apps" / "api" / "tests" / "eval" / "golden.jsonl"
)


def _load_golden(path: Path, lang_filter: str | None) -> list[dict[str, object]]:
    """Parse the golden ``.jsonl`` → list of rows, optionally filtered by lang.

    Each non-blank line must be a JSON object with at least ``q`` and a non-empty
    ``expect_any`` list. Malformed lines fail loud (a bad golden file is an
    operator error, not something to silently skip).
    """
    rows: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(
                    f"{path}:{lineno}: invalid JSON — {exc}"
                ) from exc
            if "q" not in row or not row.get("expect_any"):
                raise SystemExit(
                    f"{path}:{lineno}: each golden row needs 'q' + non-empty "
                    f"'expect_any'"
                )
            if lang_filter and str(row.get("lang") or "") != lang_filter:
                continue
            rows.append(row)
    if not rows:
        raise SystemExit(
            f"{path}: no golden rows"
            + (f" for lang={lang_filter!r}" if lang_filter else "")
        )
    return rows


async def _connect(dsn: str) -> asyncpg.Connection:
    """Open one anon-pool connection with the JSON codec registered.

    Mirrors ``db.pools._init_conn`` (json/jsonb decode to dict) + the
    ``_ssl_for_dsn`` TLS handling so the script behaves like the request path
    against Supabase or local Docker.
    """
    conn = await asyncpg.connect(dsn, ssl=_ssl_for_dsn(dsn))
    for pg_type in ("json", "jsonb"):
        await conn.set_type_codec(
            pg_type,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )
    return conn


async def _tenant_chunk_count(conn: asyncpg.Connection, tenant_id: UUID) -> int:
    """Count the tenant's knowledge_chunks (under the active RLS GUC)."""
    val = await conn.fetchval(
        "SELECT count(*) FROM knowledge_chunks WHERE tenant_id = $1",
        tenant_id,
    )
    return int(val or 0)


def _print_report(summary: dict[str, object], tenant_id: UUID) -> None:
    """Pretty-print the summary + FAIL list to stdout."""
    k = summary["k"]
    count = summary["count"]
    print(f"RAG eval — tenant {tenant_id} — {count} questions")
    print(
        f"  recall@{k}: {summary['recall_at_k']:.2f}   "
        f"MRR: {summary['mrr']:.2f}"
    )
    by_lang = summary["by_lang"]
    if isinstance(by_lang, dict) and by_lang:
        parts = [
            f"{lang} {stats['recall_at_k']:.2f} ({stats['count']})"
            for lang, stats in by_lang.items()
        ]
        print("  by lang:  " + "   ".join(parts))
    failures = summary["failures"]
    if isinstance(failures, list) and failures:
        print(f"  FAIL ({len(failures)}):")
        for fail in failures:
            page = fail.get("expect_page")
            page_str = f" page {page!r}" if page else ""
            print(
                f"    [{fail['lang']}] {fail['q']!r} "
                f"(expected any of {fail['expect_any']}{page_str} — "
                f"not retrieved)"
            )


async def _run(
    *, tenant_id: UUID, top_k: int, golden: list[dict[str, object]]
) -> dict[str, object]:
    """Score the golden set against the live retrieval path. Returns the summary."""
    settings = get_settings()
    dsn = settings.polaris_db_url_anon
    if not dsn:
        raise SystemExit(
            "POLARIS_DB_URL_ANON not set — the eval runs the RLS-scoped anon "
            "retrieval path. Set it in `.env` and run via `uv run`."
        )

    conn = await _connect(dsn)
    try:
        # ONE transaction so set_config(..., true) (= SET LOCAL) holds for every
        # query below — RLS scopes the search to this tenant.
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)",
                str(tenant_id),
            )

            n_chunks = await _tenant_chunk_count(conn, tenant_id)
            if n_chunks == 0:
                raise SystemExit(
                    f"tenant {tenant_id} has 0 knowledge_chunks — nothing to "
                    f"retrieve. Seed/ingest chunks first (e.g. the ingestion "
                    f"app or scripts/seed_demo_knowledge.py)."
                )
            logger.info("tenant has %d knowledge_chunks", n_chunks)

            scored: list[dict[str, object]] = []
            for row in golden:
                q = str(row["q"])
                raw_expect = row.get("expect_any", [])
                expect_any = list(raw_expect) if isinstance(raw_expect, list) else []
                expect_page = row.get("expect_page")
                emb = await embed_query(q)
                hits = await rag.search_hybrid(
                    conn=conn,
                    tenant_id=tenant_id,
                    query_text=q,
                    query_embedding=emb,
                    top_k=top_k,
                )
                rank = rag_eval.hit_rank(
                    hits,
                    [str(kw) for kw in expect_any],
                    str(expect_page) if expect_page else None,
                )
                scored.append(
                    {
                        "q": q,
                        "lang": row.get("lang", "unknown"),
                        "rank": rank,
                        "expect_any": expect_any,
                        "expect_page": expect_page,
                    }
                )
        finally:
            # Read-only eval — roll back to release the transaction cleanly.
            await tx.rollback()
    finally:
        await conn.close()

    return rag_eval.summarize(scored, k=top_k)


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Score the bot's retrieval against a golden Q&A set "
        "(recall@k / MRR, per-language)."
    )
    parser.add_argument(
        "--tenant", required=True, help="tenant UUID to evaluate"
    )
    parser.add_argument(
        "--top-k", type=int, default=5, help="retrieval depth (default 5)"
    )
    parser.add_argument(
        "--lang", default=None, help="only score rows with this lang code"
    )
    parser.add_argument(
        "--golden",
        type=Path,
        default=_DEFAULT_GOLDEN,
        help=f"path to the golden .jsonl (default {_DEFAULT_GOLDEN})",
    )
    args = parser.parse_args()

    try:
        tenant_id = UUID(str(args.tenant))
    except ValueError:
        raise SystemExit(f"--tenant {args.tenant!r} is not a valid UUID") from None

    golden = _load_golden(args.golden, args.lang)
    summary = await _run(tenant_id=tenant_id, top_k=args.top_k, golden=golden)
    _print_report(summary, tenant_id)
    # Always 0 — it's a report, not a gate.
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
