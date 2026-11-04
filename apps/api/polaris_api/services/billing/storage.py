"""Supabase Storage helpers for PDF uploads — Phase 9 PR-7/9.

All calls use httpx directly (no supabase-py SDK). Service-role key is
required for bucket writes; signed URLs give time-limited download access
without exposing the bucket publicly.

Async — safe to call from FastAPI handlers without blocking the event loop.
"""

from __future__ import annotations

import httpx

from polaris_api.config import Settings


async def upload_pdf(
    bucket: str,
    object_key: str,
    pdf_bytes: bytes,
    settings: Settings,
) -> None:
    """Upload a PDF to a Supabase Storage bucket.

    Raises httpx.HTTPStatusError on non-2xx response.

    Upload happens BEFORE the DB row is inserted so a Storage failure
    leaves no broken DB record (caller contract).
    """
    url = f"{settings.supabase_url}/storage/v1/object/{bucket}/{object_key}"
    headers = {
        "Authorization": f"Bearer {settings.supabase_service_key}",
        "Content-Type": "application/pdf",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, content=pdf_bytes, headers=headers)
        resp.raise_for_status()


async def get_signed_url(
    bucket: str,
    object_key: str,
    settings: Settings,
    expires_in: int = 3600,
) -> str:
    """Return a time-limited signed URL for a stored object.

    expires_in: seconds until the URL expires (default 1 hour).
    Raises httpx.HTTPStatusError on non-2xx response.
    """
    url = f"{settings.supabase_url}/storage/v1/object/sign/{bucket}/{object_key}"
    headers = {"Authorization": f"Bearer {settings.supabase_service_key}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            url,
            json={"expiresIn": expires_in},
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        return f"{settings.supabase_url}/storage/v1{data['signedURL']}"
