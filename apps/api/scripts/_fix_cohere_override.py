"""One-shot: disable Cohere rerank override for E2E tenants."""
import asyncio
import os

import asyncpg


async def main() -> None:
    dsn = os.environ["POLARIS_DB_URL_SERVICE"]
    conn = await asyncpg.connect(dsn)
    r = await conn.execute(
        "UPDATE tenants SET cohere_rerank_enabled = FALSE "
        "WHERE slug IN ('polaris-owner', 'tenant-c', 'tenant-a', 'tenant-b', 'tenant-d')"
    )
    print("Updated:", r)
    rows = await conn.fetch(
        "SELECT slug, plan_tier, cohere_rerank_enabled FROM tenants "
        "WHERE slug IN ('polaris-owner','tenant-c','tenant-a','tenant-b','tenant-d')"
    )
    for row in rows:
        print(dict(row))
    await conn.close()

asyncio.run(main())
