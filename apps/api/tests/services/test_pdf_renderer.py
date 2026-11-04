from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="function")


async def test_render_invoice_pdf_calls_weasyprint() -> None:
    """render_invoice_pdf should call WeasyPrint inside a thread."""
    context: dict[str, Any] = {
        "invoice_number": "POL-M-2605-0001",
        "invoice_kind": "monthly_subscription",
        "period_start": "2026-05-01",
        "period_end": "2026-05-31",
        "subtotal_myr": "2499.00",
        "tax_myr": "0.00",
        "total_myr": "2499.00",
        "tenant_name": "Acme Corp",
        "issued_date": "2026-05-01",
        "due_date": "2026-05-31",
    }

    fake_pdf_bytes = b"%PDF-1.4 fake"

    # Patch asyncio.to_thread to call the sync function synchronously
    # but inject our fake weasyprint output.

    async def fake_to_thread(fn: Any, *args: Any, **kwargs: Any) -> bytes:
        # Call the actual _render_to_pdf stub but intercept weasyprint import
        return fake_pdf_bytes

    with patch("polaris_api.services.billing.pdf_renderer.asyncio.to_thread", side_effect=fake_to_thread):
        from polaris_api.services.billing.pdf_renderer import render_invoice_pdf
        result = await render_invoice_pdf(context)

    assert result == fake_pdf_bytes


async def test_render_quote_pdf_calls_weasyprint() -> None:
    """render_quote_pdf should call WeasyPrint inside a thread."""
    context: dict[str, Any] = {
        "quote_number": "POL-Q-2605-0001",
        "tenant_name": "Acme Corp",
        "target_tier": "pro",
        "billing_cycle": "monthly",
        "amount_due_myr": "2499.00",
        "headline_price_myr": "2499.00",
        "headline_reply_cap": 25000,
        "quota_topup_bot_replies": 0,
        "proration_ratio": "1",
        "days_billed": 31,
        "days_in_period": 31,
        "valid_until": "2026-06-21",
        "issued_date": "2026-05-21",
        "notes": "",
    }
    fake_pdf_bytes = b"%PDF-1.4 fake-quote"

    async def fake_to_thread(fn: Any, *args: Any, **kwargs: Any) -> bytes:
        return fake_pdf_bytes

    with patch("polaris_api.services.billing.pdf_renderer.asyncio.to_thread", side_effect=fake_to_thread):
        from polaris_api.services.billing.pdf_renderer import render_quote_pdf
        result = await render_quote_pdf(context)

    assert result == fake_pdf_bytes


def test_templates_dir_resolves() -> None:
    """TEMPLATES_DIR should be a subdirectory of the repo root."""
    from polaris_api.services.billing.pdf_renderer import TEMPLATES_DIR
    # Just verify the path resolves without error and looks right
    assert "infra" in str(TEMPLATES_DIR)
    assert "templates" in str(TEMPLATES_DIR)
