/**
 * Products list — Screen #16 (Phase 15 PR-7).
 *
 * Universal across tiers per ADR-061 — every plan tier manages products
 * identically. CRUD against /api/tenant/products. The bot answers product
 * questions from the description and sends the photo when a customer wants to
 * see it. Mirrors the Promotions screen.
 */

import Link from "next/link";

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

interface ProductImageDTO {
  id: string;
  url: string | null;
}

interface ProductDTO {
  id: string;
  name: string;
  description: string;
  price_text: string | null;
  is_active: boolean;
  images: ProductImageDTO[];
}

async function loadProducts(): Promise<{
  items: ProductDTO[];
  error?: string;
}> {
  try {
    const res = await apiFetchServer<ProductDTO[]>(TenantRoutes.products());
    return { items: res ?? [] };
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error("[portal /products] fetch failed", err);
    return {
      items: [],
      error:
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't load products.",
    };
  }
}

export default async function ProductsPage() {
  const { items, error } = await loadProducts();

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Products</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Your catalog. The bot answers questions from each product&apos;s
            description and can send its photo when a customer asks to see it.
          </p>
        </div>
        <Button asChild>
          <Link href="/products/new">New product</Link>
        </Button>
      </header>

      {error ? (
        <div
          role="status"
          className="rounded-lg border border-warning bg-warning/10 px-4 py-3 text-sm text-warning"
        >
          {error}
        </div>
      ) : null}

      {items.length === 0 && !error ? (
        <div className="rounded-xl border border-dashed bg-muted/30 p-8 text-center">
          <h2 className="text-base font-semibold">No products yet</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Add a product with a photo and the bot can show it to customers who
            ask.
          </p>
          <div className="mt-4">
            <Button asChild>
              <Link href="/products/new">Add your first product</Link>
            </Button>
          </div>
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {items.map((p) => {
            const thumb = p.images.find((i) => i.url)?.url ?? null;
            return (
              <Link
                key={p.id}
                href={`/products/${p.id}`}
                className="overflow-hidden rounded-xl border bg-card transition-colors hover:border-foreground/40"
              >
                <div className="aspect-video w-full bg-muted">
                  {thumb ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={thumb}
                      alt={p.name}
                      className="h-full w-full object-cover"
                    />
                  ) : (
                    <div className="flex h-full w-full items-center justify-center text-xs text-muted-foreground">
                      No photo
                    </div>
                  )}
                </div>
                <div className="p-4">
                  <div className="flex items-start justify-between gap-2">
                    <h3 className="font-semibold">{p.name}</h3>
                    {p.is_active ? (
                      <Badge
                        variant="outline"
                        className="border-0 bg-emerald-100 text-xs font-normal text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100"
                      >
                        Active
                      </Badge>
                    ) : (
                      <Badge variant="outline" className="text-xs font-normal">
                        Hidden
                      </Badge>
                    )}
                  </div>
                  {p.price_text ? (
                    <p className="mt-1 text-sm font-medium">{p.price_text}</p>
                  ) : null}
                  <p className="mt-2 line-clamp-2 text-sm text-muted-foreground">
                    {p.description}
                  </p>
                </div>
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}
