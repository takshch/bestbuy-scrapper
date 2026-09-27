"use client";

import { Fragment, useEffect, useState } from "react";
import { api, formatPrice, type PricePoint, type Product } from "@/lib/api";

function PriceHistory({ sku }: { sku: string }) {
  const [points, setPoints] = useState<PricePoint[] | null>(null);
  useEffect(() => {
    let cancelled = false;
    api.history(sku).then((p) => !cancelled && setPoints(p), () => !cancelled && setPoints([]));
    return () => {
      cancelled = true;
    };
  }, [sku]);

  if (!points) return <span className="text-zinc-500">Loading history…</span>;
  if (points.length === 0) return <span className="text-zinc-500">No price history yet.</span>;
  return (
    <ol className="flex flex-wrap gap-x-4 gap-y-1">
      {points.map((p, i) => {
        const prev = i > 0 ? points[i - 1].price : null;
        const delta = prev !== null && p.price !== null ? p.price - prev : 0;
        return (
          <li key={p.observed_at} className="tabular-nums">
            <span className="text-zinc-500">{new Date(p.observed_at).toLocaleString()}</span>{" "}
            <span className="font-medium">{formatPrice(p.price)}</span>
            {delta !== 0 && (
              <span className={delta < 0 ? "text-emerald-600" : "text-red-600"}>
                {" "}({delta < 0 ? "−" : "+"}{formatPrice(Math.abs(delta))})
              </span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function Stars({ rating, count }: { rating: number | null; count: number | null }) {
  if (rating === null) return <span className="text-zinc-400">—</span>;
  return (
    <span className="whitespace-nowrap">
      <span className="text-amber-500">★</span> {rating.toFixed(1)}
      {count !== null && <span className="text-zinc-500"> ({count.toLocaleString()})</span>}
    </span>
  );
}

export function ProductsTable({ products }: { products: Product[] }) {
  const [open, setOpen] = useState<string | null>(null);
  if (products.length === 0) {
    return <p className="py-10 text-center text-sm text-zinc-500">No products match these filters.</p>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-zinc-200 text-xs uppercase text-zinc-500 dark:border-zinc-800">
          <tr>
            <th className="py-2 pr-4 font-medium">Product</th>
            <th className="py-2 pr-4 text-right font-medium">Price</th>
            <th className="py-2 pr-4 font-medium">Rating</th>
            <th className="py-2 pr-4 font-medium">Availability</th>
            <th className="py-2 pr-4 font-medium">Updated</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
          {products.map((p) => {
            const onSale = p.price !== null && p.regular_price !== null && p.regular_price > p.price;
            return (
              <Fragment key={p.sku}>
              <tr>
                <td className="py-3 pr-4">
                  <div className="flex items-center gap-3">
                    {p.image_url ? (
                      // Remote Best Buy CDN images; plain <img> avoids configuring next/image domains.
                      // eslint-disable-next-line @next/next/no-img-element
                      <img
                        src={`${p.image_url.split(";")[0]};maxHeight=96;maxWidth=96?format=webp`}
                        alt=""
                        className="size-12 shrink-0 rounded bg-white object-contain p-0.5 ring-1 ring-zinc-200 dark:ring-zinc-700"
                        loading="lazy"
                      />
                    ) : (
                      <div className="size-12 shrink-0 rounded bg-zinc-100 dark:bg-zinc-800" />
                    )}
                    <div className="min-w-0">
                      {p.url ? (
                        <a href={p.url} target="_blank" rel="noreferrer" className="line-clamp-2 font-medium hover:text-blue-600 hover:underline">
                          {p.name}
                        </a>
                      ) : (
                        <span className="line-clamp-2 font-medium">{p.name}</span>
                      )}
                      <div className="mt-0.5 text-xs text-zinc-500">
                        SKU {p.sku}
                        {p.model && <> · Model {p.model}</>}
                      </div>
                    </div>
                  </div>
                </td>
                <td className="whitespace-nowrap py-3 pr-4 text-right tabular-nums">
                  <div className="font-semibold">{formatPrice(p.price)}</div>
                  <button
                    onClick={() => setOpen(open === p.sku ? null : p.sku)}
                    className="text-xs text-blue-600 hover:underline dark:text-blue-400"
                  >
                    {open === p.sku ? "hide history" : "history"}
                  </button>
                  {onSale && (
                    <div className="text-xs">
                      <span className="text-zinc-500 line-through">{formatPrice(p.regular_price)}</span>{" "}
                      <span className="font-medium text-emerald-600">
                        −{Math.round((1 - p.price! / p.regular_price!) * 100)}%
                      </span>
                    </div>
                  )}
                </td>
                <td className="py-3 pr-4">
                  <Stars rating={p.rating} count={p.review_count} />
                </td>
                <td className="py-3 pr-4 text-xs text-zinc-600 dark:text-zinc-400">
                  {p.availability?.replaceAll("_", " ").toLowerCase() ?? "—"}
                </td>
                <td className="whitespace-nowrap py-3 pr-4 text-xs text-zinc-500">
                  {new Date(p.updated_at).toLocaleString()}
                </td>
              </tr>
              {open === p.sku && (
                <tr>
                  <td colSpan={5} className="bg-zinc-50 px-3 py-2 text-xs dark:bg-zinc-900">
                    <PriceHistory sku={p.sku} />
                  </td>
                </tr>
              )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
