"""Benchmark amplification — Phase 4 PR-5.

Reads the hand-written archetype corpus
(`benchmarks/cost_archetypes.md` from Phase 0 §12.3) and uses Sonnet
4.6 to expand each archetype into 20 natural variants, producing the
1k-prompt benchmark corpus used by `benchmarks/run.py`.

Usage:

  uv run python benchmarks/expand.py \\
      --archetypes benchmarks/cost_archetypes.md \\
      --output benchmarks/prompts/ \\
      [--limit 5]   # for smoke runs

Output:
  benchmarks/prompts/<archetype-id>.jsonl  — one variant per line

Each variant line is a JSON record:
  {"id": "simple-001-v05", "input": "...", "expected": "simple",
   "language": "en", "source_archetype": "simple-001"}

Cost note: Sonnet 4.6 at ~$3/MTok output, 20 variants × ~30 tokens =
600 tokens per archetype = ~30k tokens for the full 50-archetype
sweep ≈ $0.10. Cheap; safe to run weekly. `--limit` for cheaper
smoke runs.

This is a **manual one-shot tool**, NOT exercised by CI:
  - `test_eval_regression.py` validates the YAML eval suites
    (`evals/*.yaml`), not the amplified benchmark corpus.
  - PR-5 ships the script + the archetype-parsing shape; the
    operator runs it manually when archetypes change.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Direct-script invocation needs the repo root on sys.path so
# `from polaris_api.services import llm` resolves outside of pytest.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from polaris_api.services import llm  # noqa: E402 — after sys.path tweak
from polaris_api.services._llm_models import LLMConfigError  # noqa: E402

logger = logging.getLogger(__name__)

# Markdown heading marker for archetype sections in cost_archetypes.md.
# The Phase 0 §12.3 file uses `### [tier/lang] short-title` headers
# per archetype, with the prompt body following.
_ARCHETYPE_HEADER_RE = re.compile(
    r"^###\s+\[(?P<tier>simple|medium|hard)/(?P<lang>en|ms|zh-cn)\]\s+(?P<title>.+)$"
)

_DEFAULT_VARIANT_COUNT = 20

# Loaded lazily so the doc-only `import benchmarks.expand` from
# test_eval_regression doesn't require the full prompt file.
_EXPANDER_PROMPT_PATH = Path(__file__).parent / "_expander_prompt.md"


@dataclass(slots=True)
class Archetype:
    """One row from cost_archetypes.md."""
    id: str           # e.g. "simple-001"
    tier: str         # simple / medium / hard
    language: str     # en / ms / zh-cn
    title: str        # short human-readable label
    prompt: str       # the actual archetype text


def parse_archetypes(markdown_path: Path) -> list[Archetype]:
    """Parse cost_archetypes.md into Archetype rows.

    The convention from Phase 0 §12.3 is:

      ### [tier/lang] title
      The archetype prompt body, free text, possibly multi-line until
      the next ### header.

    Lines outside any ### section are ignored (file header, intro
    paragraphs). Archetype ids are auto-numbered per tier in document
    order — first simple = simple-001, second = simple-002, etc.
    """
    text = markdown_path.read_text(encoding="utf-8")
    archetypes: list[Archetype] = []
    counters: dict[str, int] = {"simple": 0, "medium": 0, "hard": 0}

    current_header: re.Match[str] | None = None
    current_body: list[str] = []

    def _flush() -> None:
        if current_header is None:
            return
        tier = current_header.group("tier")
        counters[tier] += 1
        archetype_id = f"{tier}-{counters[tier]:03d}"
        archetypes.append(Archetype(
            id=archetype_id,
            tier=tier,
            language=current_header.group("lang"),
            title=current_header.group("title").strip(),
            prompt="\n".join(line.strip() for line in current_body).strip(),
        ))

    for line in text.splitlines():
        m = _ARCHETYPE_HEADER_RE.match(line)
        if m:
            _flush()
            current_header = m
            current_body = []
        elif current_header is not None:
            current_body.append(line)
    _flush()
    return archetypes


def _load_expander_prompt() -> str:
    return _EXPANDER_PROMPT_PATH.read_text(encoding="utf-8")


async def expand_archetype(
    archetype: Archetype,
    *,
    variant_count: int = _DEFAULT_VARIANT_COUNT,
) -> list[str]:
    """Call Sonnet to generate ``variant_count`` natural variants.

    Returns the variant strings as a list. On underflow (Sonnet
    emitted fewer than requested), pads with the original archetype
    so the output has the requested count — this keeps the corpus
    size deterministic across archetypes even when Sonnet is terse.
    """
    # services/llm.py owns the system prompt — we can't inject a custom
    # one. So fold the expander instructions into the user_text instead.
    # The prompt file is the single source of the wording so the operator
    # can tune amplification without code changes.
    expander_prompt = _load_expander_prompt()
    user_text = (
        f"{expander_prompt}\n\n---\n\n"
        f"Archetype: `[{archetype.tier}/{archetype.language}] {archetype.title}`\n\n"
        f"Body: {archetype.prompt}\n\n"
        f"Emit {variant_count} variants, one per line."
    )
    envelope = await llm.generate_response(
        tenant_id=archetype.id,  # type: ignore[arg-type]  # uuid not needed here
        rag_context="",
        conversation_history=[],
        user_message=user_text,
        intent="hard",  # Sonnet path for richer variant generation
    )
    raw_lines = [
        ln.strip() for ln in envelope.content.splitlines() if ln.strip()
    ]
    if len(raw_lines) < variant_count:
        # Pad with the original prompt so downstream sees a stable
        # row count per archetype.
        raw_lines.extend([archetype.prompt] * (variant_count - len(raw_lines)))
    return raw_lines[:variant_count]


def write_jsonl(
    archetype: Archetype, variants: list[str], output_dir: Path,
) -> Path:
    """Write one .jsonl file per archetype, one variant per line."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{archetype.id}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for idx, variant in enumerate(variants, start=1):
            record = {
                "id": f"{archetype.id}-v{idx:02d}",
                "input": variant,
                "expected": archetype.tier,
                "language": archetype.language,
                "source_archetype": archetype.id,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


async def _amain(args: argparse.Namespace) -> int:
    archetypes_path = Path(args.archetypes)
    output_dir = Path(args.output)

    # CLI tool — sync I/O on the event loop is fine; this is one
    # blocking syscall before the await-heavy main loop starts.
    if not archetypes_path.is_file():  # noqa: ASYNC240
        logger.error("archetypes file not found: %s", archetypes_path)
        return 2

    archetypes = parse_archetypes(archetypes_path)
    if args.limit:
        archetypes = archetypes[: args.limit]
    if not archetypes:
        logger.error("no archetypes parsed from %s", archetypes_path)
        return 2

    logger.info("expanding %d archetypes → %s/", len(archetypes), output_dir)
    failures = 0
    for archetype in archetypes:
        try:
            variants = await expand_archetype(
                archetype, variant_count=args.variants,
            )
        except LLMConfigError as exc:
            logger.error(
                "expand failed (LLM config): %s — paste a real "
                "ANTHROPIC_API_KEY in .env before running expand",
                exc,
            )
            return 3
        except Exception as exc:  # noqa: BLE001
            logger.error("expand failed for %s: %s", archetype.id, exc)
            failures += 1
            continue
        path = write_jsonl(archetype, variants, output_dir)
        logger.info("wrote %d variants → %s", len(variants), path.name)

    if failures:
        logger.warning("%d archetype(s) failed; partial corpus produced", failures)
        return 1
    logger.info("expand complete: %d archetypes × %d variants = %d total prompts",
                len(archetypes), args.variants, len(archetypes) * args.variants)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archetypes",
        default="benchmarks/cost_archetypes.md",
        help="Path to the hand-written archetype markdown",
    )
    parser.add_argument(
        "--output",
        default="benchmarks/prompts",
        help="Output directory for per-archetype .jsonl files",
    )
    parser.add_argument(
        "--variants", type=int, default=_DEFAULT_VARIANT_COUNT,
        help=f"Variants per archetype (default {_DEFAULT_VARIANT_COUNT})",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Only expand the first N archetypes (cheap smoke runs)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if not args.verbose else logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
