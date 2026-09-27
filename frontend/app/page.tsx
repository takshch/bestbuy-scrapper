"use client";

import { useEffect, useRef, useState } from "react";
import { HealthBar } from "@/components/HealthBar";
import { JobsTable } from "@/components/JobsTable";
import { ProductsTable } from "@/components/ProductsTable";
import { ScrapeForm } from "@/components/ScrapeForm";
import { StatsPanel } from "@/components/StatsPanel";
import { api, isActive, type Health, type ProductFilters, type ProductPage, type ScrapeJob, type Stats } from "@/lib/api";

const PAGE_SIZE = 25;
const ACTIVE_POLL_MS = 3000;
const IDLE_POLL_MS = 15000;

const errorMessage = (err: unknown) => (err instanceof Error ? err.message : String(err));

const inputClass =
  "rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-zinc-700 dark:bg-zinc-900";

function Card({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="rounded-xl border border-zinc-200 bg-white p-5 shadow-sm dark:border-zinc-800 dark:bg-zinc-950">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-base font-semibold">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

export default function Dashboard() {
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [jobs, setJobs] = useState<ScrapeJob[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [products, setProducts] = useState<ProductPage | null>(null);
  const [productsError, setProductsError] = useState<string | null>(null);
  const [filters, setFilters] = useState<ProductFilters>({ sort: "updated", offset: 0 });
  const [search, setSearch] = useState("");
  // Bumped when a scrape job finishes, to reload products and health.
  const [refreshKey, setRefreshKey] = useState(0);
  const activeJobIds = useRef<Set<number>>(new Set());

  // Health: on mount, every 15s, and after a job finishes.
  useEffect(() => {
    let cancelled = false;
    const tick = () =>
      api.health().then(
        (h) => !cancelled && (setHealth(h), setHealthError(null)),
        (err) => !cancelled && setHealthError(errorMessage(err)),
      );
    tick();
    const t = setInterval(tick, IDLE_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [refreshKey]);

  // Jobs: poll fast while something is running, slowly otherwise.
  const hasActiveJob = jobs.some(isActive);
  useEffect(() => {
    let cancelled = false;
    const tick = () => {
      api.stats().then((s) => !cancelled && setStats(s), () => {});
      return api.jobs(20).then((latest) => {
        if (cancelled) return;
        setJobs(latest);
        const nowActive = new Set(latest.filter(isActive).map((j) => j.id));
        const finished = [...activeJobIds.current].some((id) => !nowActive.has(id));
        activeJobIds.current = nowActive;
        if (finished) setRefreshKey((k) => k + 1);
      }, () => {
        // Health bar already reports connectivity problems.
      });
    };
    tick();
    const t = setInterval(tick, hasActiveJob ? ACTIVE_POLL_MS : IDLE_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [hasActiveJob]);

  // Products: reload when filters change or a job finishes.
  useEffect(() => {
    let cancelled = false;
    api.products({ ...filters, limit: PAGE_SIZE }).then(
      (page) => !cancelled && (setProducts(page), setProductsError(null)),
      (err) => !cancelled && setProductsError(errorMessage(err)),
    );
    return () => {
      cancelled = true;
    };
  }, [filters, refreshKey]);

  // Debounce the free-text search box.
  useEffect(() => {
    const t = setTimeout(() => setFilters((f) => (f.q === search ? f : { ...f, q: search, offset: 0 })), 300);
    return () => clearTimeout(t);
  }, [search]);

  const update = (patch: ProductFilters) => setFilters((f) => ({ ...f, ...patch, offset: 0 }));
  const offset = filters.offset ?? 0;
  const total = products?.total ?? 0;

  return (
    <main className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 py-8">
      <header className="flex flex-col gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Best Buy Scraper</h1>
          <p className="text-sm text-zinc-500">
            Queue Best Buy searches for the scrape workers and browse the products stored in Postgres.
          </p>
        </div>
        <HealthBar health={health} error={healthError} />
      </header>

      <Card title="New scrape">
        <ScrapeForm
          onStarted={(job) => {
            activeJobIds.current.add(job.id);
            setJobs((prev) => [job, ...prev.filter((j) => j.id !== job.id)]);
          }}
        />
      </Card>

      <Card title="Workers & throughput">
        <StatsPanel stats={stats} />
      </Card>

      <Card
        title="Scrape jobs"
        action={
          hasActiveJob && <span className="text-xs text-blue-600 dark:text-blue-400">Refreshing every 3s…</span>
        }
      >
        <JobsTable
          jobs={jobs}
          onCancelled={(job) => setJobs((prev) => prev.map((j) => (j.id === job.id ? job : j)))}
          onSelectQuery={(q) => {
            update({ search_query: q });
            document.getElementById("products")?.scrollIntoView({ behavior: "smooth" });
          }}
        />
      </Card>

      <div id="products">
        <Card title={`Products${products ? ` (${total.toLocaleString()})` : ""}`}>
          <div className="mb-4 flex flex-wrap items-end gap-3">
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search product names…"
              className={`${inputClass} min-w-48 flex-1`}
            />
            <input
              type="number"
              min={0}
              placeholder="Min $"
              value={filters.min_price ?? ""}
              onChange={(e) => update({ min_price: e.target.value })}
              className={`${inputClass} w-24`}
            />
            <input
              type="number"
              min={0}
              placeholder="Max $"
              value={filters.max_price ?? ""}
              onChange={(e) => update({ max_price: e.target.value })}
              className={`${inputClass} w-24`}
            />
            <select
              value={filters.sort}
              onChange={(e) => update({ sort: e.target.value as ProductFilters["sort"] })}
              className={inputClass}
            >
              <option value="updated">Recently updated</option>
              <option value="price_asc">Price: low to high</option>
              <option value="price_desc">Price: high to low</option>
              <option value="rating">Top rated</option>
            </select>
            {filters.search_query && (
              <button
                onClick={() => update({ search_query: undefined })}
                className="rounded-full bg-blue-50 px-3 py-1 text-xs font-medium text-blue-700 hover:bg-blue-100 dark:bg-blue-950 dark:text-blue-300"
                title="Clear filter"
              >
                From search: “{filters.search_query}” ✕
              </button>
            )}
          </div>

          {productsError ? (
            <p className="py-6 text-center text-sm text-red-600">{productsError}</p>
          ) : products ? (
            <ProductsTable products={products.items} />
          ) : (
            <p className="py-10 text-center text-sm text-zinc-500">Loading…</p>
          )}

          {total > PAGE_SIZE && (
            <div className="mt-4 flex items-center justify-between text-sm">
              <span className="text-zinc-500">
                {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
              </span>
              <div className="flex gap-2">
                <button
                  disabled={offset === 0}
                  onClick={() => setFilters((f) => ({ ...f, offset: Math.max(0, offset - PAGE_SIZE) }))}
                  className="rounded-lg border border-zinc-300 px-3 py-1 disabled:opacity-40 dark:border-zinc-700"
                >
                  Previous
                </button>
                <button
                  disabled={offset + PAGE_SIZE >= total}
                  onClick={() => setFilters((f) => ({ ...f, offset: offset + PAGE_SIZE }))}
                  className="rounded-lg border border-zinc-300 px-3 py-1 disabled:opacity-40 dark:border-zinc-700"
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </Card>
      </div>
    </main>
  );
}
