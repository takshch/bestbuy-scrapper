"use client";

import type { Stats } from "@/lib/api";

const ROUTE_LABELS: Record<string, string> = {
  direct: "Direct IP",
  direct_then_proxy: "Direct → Oxylabs",
  proxy: "Oxylabs",
  proxy_unavailable: "Direct (proxy unavailable)",
};

function Tile({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div className="rounded-lg border border-zinc-200 px-3 py-2 dark:border-zinc-800">
      <div className="text-xs text-zinc-500">{label}</div>
      <div className="text-lg font-semibold tabular-nums">{value}</div>
      {hint && <div className="text-xs text-zinc-500">{hint}</div>}
    </div>
  );
}

export function StatsPanel({ stats }: { stats: Stats | null }) {
  if (!stats) return <p className="text-sm text-zinc-500">Loading stats…</p>;

  const alive = stats.workers.filter((w) => w.alive);
  const slots = alive.reduce((n, w) => n + w.concurrency, 0);
  const busy = alive.reduce((n, w) => n + w.busy, 0);
  const routes = Object.entries(stats.routes_24h);

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Tile
          label="Workers"
          value={alive.length}
          hint={alive.length ? `${busy}/${slots} browsers busy` : "none running"}
        />
        <Tile label="Queued pages" value={stats.queue_depth} hint={`${stats.tasks.running ?? 0} running`} />
        <Tile label="Pages / hour" value={stats.pages_last_hour} hint={`${stats.products_last_hour} products`} />
        <Tile label="Products stored" value={stats.products.toLocaleString()} />
        <Tile label="Price changes (24h)" value={stats.price_changes_24h} />
        <Tile
          label="Direct IP"
          value={stats.direct_cooldown_seconds > 0 ? "cooling down" : "ok"}
          hint={stats.direct_cooldown_seconds > 0 ? `${Math.ceil(stats.direct_cooldown_seconds / 60)} min left` : undefined}
        />
      </div>

      {routes.length > 0 && (
        <div className="text-xs">
          <div className="mb-1 font-medium text-zinc-500">Pages by route (24h)</div>
          <div className="flex flex-wrap gap-2">
            {routes.map(([name, r]) => (
              <span key={name} className="rounded-md bg-zinc-100 px-2 py-1 dark:bg-zinc-800">
                {ROUTE_LABELS[name] ?? name}: {r.pages} pages ·{" "}
                {r.success_rate === null ? "—" : `${Math.round(r.success_rate * 100)}% ok`}
              </span>
            ))}
          </div>
        </div>
      )}

      {alive.length === 0 && (
        <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-950 dark:text-amber-300">
          No workers are running, so queued pages won&apos;t be scraped. Start one with{" "}
          <code className="font-mono">python -m app.worker</code> or{" "}
          <code className="font-mono">docker compose up -d --scale worker=2</code>.
        </p>
      )}
    </div>
  );
}
