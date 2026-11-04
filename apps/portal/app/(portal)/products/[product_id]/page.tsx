/**
 * Product edit page (Phase 15 PR-7).
 *
 * Server Component fetches the product (with signed image URLs) + branch list
 * in parallel and hands off to the same form used for create. Edit mode shows
 * the image gallery (upload/delete) + a delete button.
 */

import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import {
  ProductForm,
  type ProductDTO,
} from "@/components/products/ProductForm";

interface BranchDTO {
  id: string;
  name: string;
}

async function load(
  id: string,
): Promise<{ product: ProductDTO | null; branches: BranchDTO[] }> {
  const [productRes, branchesRes] = await Promise.allSettled([
    apiFetchServer<ProductDTO>(TenantRoutes.product(id)),
    apiFetchServer<BranchDTO[]>(TenantRoutes.branches()),
  ]);
  let product: ProductDTO | null = null;
  if (productRes.status === "fulfilled") {
    product = productRes.value ?? null;
  } else if (
    productRes.reason instanceof PolarisApiError &&
    productRes.reason.status === 404
  ) {
    product = null;
  } else {
    throw productRes.reason;
  }
  const branches =
    branchesRes.status === "fulfilled" ? (branchesRes.value ?? []) : [];
  return { product, branches };
}

export default async function ProductEditPage({
  params,
}: {
  params: { product_id: string };
}) {
  const { product, branches } = await load(params.product_id);
  if (!product) {
    notFound();
  }

  return (
    <div className="space-y-6">
      <div>
        <Link
          href="/products"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
          All products
        </Link>
      </div>

      <header className="space-y-2">
        <h1 className="text-3xl font-semibold tracking-tight">Edit product</h1>
      </header>

      <ProductForm mode="edit" branches={branches} initial={product} />
    </div>
  );
}
