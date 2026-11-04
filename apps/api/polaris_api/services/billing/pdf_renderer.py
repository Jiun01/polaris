"""PDF rendering via WeasyPrint + Jinja2 — Phase 9 PR-7/9.

WeasyPrint is synchronous; every public function wraps the render in
asyncio.to_thread() so callers can await without blocking the event loop.

Templates live in infra/templates/ (resolved relative to this file's
location so the path works in both local dev and Docker).
"""

from __future__ import annotations

import asyncio
import os
from io import BytesIO
from pathlib import Path
from typing import Any

from jinja2 import (  # noqa: F401 (re-exported for test)
    Environment,
    FileSystemLoader,
    TemplateNotFound,
)

# Resolve template directory relative to this file.
# apps/api/polaris_api/services/billing/pdf_renderer.py
# → parents[4] = repo root → infra/templates/
_REPO_ROOT = Path(__file__).resolve().parents[5]
# In the Docker image the source layout (apps/api/…/infra/templates) is gone —
# the package is installed into site-packages — so the relative path won't
# resolve. POLARIS_TEMPLATES_DIR (set in the image, see apps/api/Dockerfile)
# overrides; the parents[5] path is the local-source fallback.
TEMPLATES_DIR = Path(
    os.environ.get("POLARIS_TEMPLATES_DIR") or str(_REPO_ROOT / "infra" / "templates")
)

_env: Environment | None = None


def _get_env() -> Environment:
    global _env  # noqa: PLW0603
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=True,
        )
    return _env


def _render_to_pdf(template_name: str, context: dict[str, Any]) -> bytes:
    """Synchronous render — must run in a thread (WeasyPrint is sync)."""
    from weasyprint import CSS, HTML

    env = _get_env()
    template = env.get_template(template_name)
    html_str = template.render(**context)

    css_name = template_name.replace(".html.j2", ".css")
    css_path = TEMPLATES_DIR / css_name
    stylesheets = [CSS(filename=str(css_path))] if css_path.exists() else []

    buf = BytesIO()
    # base_url lets WeasyPrint resolve the template's relative <link href="…css">
    # (otherwise: "Relative URI reference without a base URI").
    HTML(string=html_str, base_url=str(TEMPLATES_DIR)).write_pdf(
        target=buf, stylesheets=stylesheets
    )
    return buf.getvalue()


async def render_invoice_pdf(context: dict[str, Any]) -> bytes:
    """Render invoice.html.j2 to PDF bytes (async wrapper)."""
    return await asyncio.to_thread(_render_to_pdf, "invoice.html.j2", context)


async def render_quote_pdf(context: dict[str, Any]) -> bytes:
    """Render sales_quote.html.j2 to PDF bytes (async wrapper)."""
    return await asyncio.to_thread(_render_to_pdf, "sales_quote.html.j2", context)
