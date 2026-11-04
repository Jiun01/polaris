"""Pydantic -> JSON Schema -> TypeScript export pipeline (PR-4 hardened).

Two stages, separable so CI doesn't need pnpm for the drift check:

  Stage 1 (pure Python):
      build_schema()  ->  dict   (deterministic, sorted)
      write_schema_json(path)    writes typescript/schema.json
      --check                    write to a temp, compare to committed file,
                                 exit 0 if equal, 1 if drift (prints diff hint)

  Stage 2 (Node):
      invoke_generator()         runs `pnpm dlx json-schema-to-typescript`
                                 to produce typescript/polaris-shared-types.ts

PR-4 commits typescript/schema.json so `git diff --exit-code` after running
`--check` (or after a real export) is the drift gate. The .ts is still
gitignored at GA — portal + admin don't import it yet (hello-world only); a
later phase un-ignores it after the first real generator run lands.

ADR-011: ts-json-schema-generator is the long-term TS generator; for the
PR-1/PR-4 baseline we route through json-schema-to-typescript because the
JSON-Schema-first pipeline is simpler. ADR-011 stands; the generator choice
can be swapped without touching Pydantic source.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Allow direct invocation (`python packages/shared-types/scripts/export.py`)
# without requiring the package to be uv-installed. CI uses `uv sync` so it
# would resolve via site-packages — this shim makes local runs frictionless.
_PYTHON_PKG = Path(__file__).resolve().parents[1] / "python"
if str(_PYTHON_PKG) not in sys.path:
    sys.path.insert(0, str(_PYTHON_PKG))

from polaris_shared_types import (  # noqa: E402 — see sys.path shim above
    Attachment,
    AuditChainVerification,
    BroadcastDTO,
    BroadcastRecipientDTO,
    ChunkingConfig,
    CohereOverrideCost,
    ConversationDetail,
    ConversationListItem,
    ConversationListResponse,
    CreateBroadcastRequest,
    CSVImportResult,
    DashboardSummary,
    HandoffInitiatedEventPayload,
    HandoffResult,
    HealthSnapshot,
    HealthStatus,
    IncomingMessage,
    IngestionRequestResponse,
    IngestionRequestSubmit,
    IssueInvoiceRequest,
    KnowledgeChunkHit,
    KnowledgeGapExample,
    KnowledgeGapExamplesResponse,
    KnowledgeGapsSummary,
    KnowledgeGapTheme,
    KnowledgeSourceCreate,
    KnowledgeSourceResponse,
    MessageDetail,
    MessengerProfileBundle,
    MessengerProfilePushResult,
    Notification,
    NotificationListResponse,
    NotificationMarkAllReadResponse,
    NotificationMarkReadRequest,
    OptOutViewerPage,
    PlanChangeApprovalRequest,
    PlanChangeRejectionRequest,
    PlanChangeRequest,
    PlanChangeRequestCreate,
    PlatformAdmin,
    PlatformCostSummary,
    PlatformFixedCost,
    ProductHit,
    ProductImage,
    QuoteGenerateRequest,
    QuotePdfMeta,
    SalesQuote,
    SenderRef,
    TenantBranch,
    TenantBranchUpsert,
    TenantCostMonthly,
    TenantInvoice,
    TenantProduct,
    TenantProductUpsert,
    TenantPromotion,
    TenantPromotionUpsert,
    TenantTemplateDTO,
    TenantTemplateSubmit,
)
from pydantic import BaseModel  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TS_DIR = ROOT / "typescript"
SCHEMA_JSON = TS_DIR / "schema.json"
OUTPUT_TS = TS_DIR / "polaris-shared-types.ts"

# Order matters for $defs determinism: list models alphabetically so a
# future addition lands in a stable position (avoids reviewer churn
# when schema.json regenerates).
MODELS: list[type[BaseModel]] = [
    Attachment,
    AuditChainVerification,
    BroadcastDTO,
    BroadcastRecipientDTO,
    CSVImportResult,
    ChunkingConfig,
    CohereOverrideCost,
    ConversationDetail,
    ConversationListItem,
    ConversationListResponse,
    CreateBroadcastRequest,
    DashboardSummary,
    HandoffInitiatedEventPayload,
    HandoffResult,
    HealthSnapshot,
    HealthStatus,
    IncomingMessage,
    IngestionRequestResponse,
    IngestionRequestSubmit,
    IssueInvoiceRequest,
    KnowledgeChunkHit,
    KnowledgeGapExample,
    KnowledgeGapExamplesResponse,
    KnowledgeGapsSummary,
    KnowledgeGapTheme,
    KnowledgeSourceCreate,
    KnowledgeSourceResponse,
    MessageDetail,
    MessengerProfileBundle,
    MessengerProfilePushResult,
    Notification,
    NotificationListResponse,
    NotificationMarkAllReadResponse,
    NotificationMarkReadRequest,
    OptOutViewerPage,
    PlanChangeApprovalRequest,
    PlanChangeRejectionRequest,
    PlanChangeRequest,
    PlanChangeRequestCreate,
    PlatformAdmin,
    PlatformCostSummary,
    PlatformFixedCost,
    ProductHit,
    ProductImage,
    QuoteGenerateRequest,
    QuotePdfMeta,
    SalesQuote,
    SenderRef,
    TenantBranch,
    TenantBranchUpsert,
    TenantCostMonthly,
    TenantInvoice,
    TenantProduct,
    TenantProductUpsert,
    TenantPromotion,
    TenantPromotionUpsert,
    TenantTemplateDTO,
    TenantTemplateSubmit,
]


# ---------- Stage 1: pure Python ----------


def build_schema() -> dict[str, object]:
    """Bundle every Pydantic model into one JSON Schema with $defs.

    Pydantic's own `model_json_schema()` is per-model: nested types (enums,
    sub-models) are emitted as a LOCAL `$defs` block nested inside that
    model's own schema, with `$ref: "#/$defs/X"` pointers that are relative
    to the model's own document root. Bundling N such per-model schemas
    naively under `top["$defs"][model.__name__] = model.model_json_schema()`
    (the original approach) buries every nested type at
    `$defs.<Model>.$defs.<Nested>` while the `$ref` strings inside still say
    `#/$defs/<Nested>` (now resolving against the COMBINED document root,
    where no such top-level key exists) — every model with an enum or a
    sub-model field produces dangling refs once combined. `json-schema-to-
    typescript` correctly errors on this (`MissingPointerError`) rather than
    silently producing wrong output.

    Fix: hoist each model's nested `$defs` entries up to the shared
    top-level `$defs` dict (their `$ref` strings need no rewriting — they
    already read `#/$defs/<Nested>`, which is exactly right once `<Nested>`
    lives at the top level) and strip the now-empty nested `$defs` key from
    the model's own schema. Nested-def names are shared vocabulary (e.g.
    `BillingCycle`, `PlanTier` appear under multiple models) — later models
    simply overwrite with an identical dict, which is safe since Pydantic
    always emits the same shape for the same type.

    Deterministic: callers must json.dumps with sort_keys=True for stable diffs.
    """
    defs: dict[str, object] = {}
    for model in MODELS:
        model_schema = model.model_json_schema()
        nested_defs = model_schema.pop("$defs", None)
        if nested_defs:
            defs.update(nested_defs)
        defs[model.__name__] = model_schema
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "PolarisSharedTypes",
        "type": "object",
        "$defs": defs,
    }


def schema_text() -> str:
    return json.dumps(build_schema(), indent=2, sort_keys=True) + "\n"


def write_schema_json(path: Path = SCHEMA_JSON) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(schema_text(), encoding="utf-8")


def check_no_drift() -> int:
    """Return 0 if committed schema.json matches Pydantic source, 1 otherwise."""
    expected = schema_text()
    if not SCHEMA_JSON.exists():
        print(
            f"export.py --check: {SCHEMA_JSON.relative_to(ROOT.parent.parent)} "
            "does not exist. Run `make export-types` and commit.",
            file=sys.stderr,
        )
        return 1
    actual = SCHEMA_JSON.read_text(encoding="utf-8")
    if actual == expected:
        print("export.py --check: schema.json is in sync.")
        return 0
    print(
        "export.py --check: DRIFT detected in schema.json — Pydantic models "
        "changed but schema.json was not regenerated.\n"
        "Run `make export-types` locally and commit the regenerated file.",
        file=sys.stderr,
    )
    return 1


# ---------- Stage 2: Node generator (optional in CI) ----------


def invoke_generator() -> int:
    """Run json-schema-to-typescript against the workspace-root install.

    Originally shelled out to `pnpm dlx json-schema-to-typescript@^15`, but
    that has two independent bugs discovered 2026-07-02 (Windows dev
    machine, pnpm 9.12.0, Node 24):

      1. `pnpm dlx <pkg>` with no explicit --package/bin name tries to run a
         bin literally named `json-schema-to-typescript` — the package's
         actual bin is `json2ts` (see its package.json `"bin"` field). This
         alone would 127/"command not found" even if (2) didn't exist.
      2. `pnpm dlx` on Windows creates a fresh temp cache subdir per
         invocation and intermittently ENOENTs on the freshly-junctioned
         `node_modules/<pkg>/package.json` before the junction is queryable
         — reproduced 5/5 times, including with a warm dlx package cache.
         Root cause not pinned down further (likely a race between pnpm's
         junction creation and Node's resolver); not worth chasing given a
         reliable workaround exists.

    Fix: depend on `json-schema-to-typescript` as a root devDependency
    (`pnpm install` once, ordinary workspace resolution — no dlx, no
    per-invocation temp dirs) and invoke its `dist/src/cli.js` directly via
    the resolved `node_modules/.bin` shim. `--unreachableDefinitions` is
    required: our bundled schema.json's root has no top-level `properties`
    referencing the `$defs` (they're a flat bag of independently-named
    models), so without this flag the generator only emits the root
    `PolarisSharedTypes` wrapper interface and silently drops every model.
    """
    workspace_root = ROOT.parent.parent
    bin_path = workspace_root / "node_modules" / ".bin" / (
        "json2ts.cmd" if sys.platform == "win32" else "json2ts"
    )
    if not bin_path.exists():
        print(
            "export.py: json-schema-to-typescript not installed at "
            f"{bin_path} — run `pnpm install` at the workspace root first "
            "(it's a root devDependency).",
            file=sys.stderr,
        )
        return 1
    proc = subprocess.run(  # noqa: S603 — fixed argv
        [
            str(bin_path),
            str(SCHEMA_JSON),
            "-o",
            str(OUTPUT_TS),
            "--unreachableDefinitions",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # On failure, BOTH streams go to stderr so CI/scripts parsing stdout
        # for the schema path don't ingest TS-generator error text.
        print(proc.stdout, file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
    return proc.returncode


# ---------- CLI ----------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="export.py",
        description="Polaris shared-types Pydantic -> JSON Schema -> TS pipeline.",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="Drift check: compare committed schema.json against Pydantic source. "
        "Exits 1 on drift, 0 otherwise. Does not write anything.",
    )
    p.add_argument(
        "--schema-only",
        action="store_true",
        help="Stage 1 only: regenerate schema.json. Skip Node TS generator.",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()

    if args.check:
        return check_no_drift()

    write_schema_json()
    print(f"wrote {SCHEMA_JSON.relative_to(ROOT.parent.parent)}")

    if args.schema_only:
        return 0

    rc = invoke_generator()
    if rc != 0:
        print(
            "export.py: TS generator failed — schema.json is still up to date.",
            file=sys.stderr,
        )
        return rc
    print(f"wrote {OUTPUT_TS.relative_to(ROOT.parent.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
