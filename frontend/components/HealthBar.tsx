"use client";

import type { Health } from "@/lib/api";

function Pill({ ok, label }: { ok: boolean | null; label: string }) {
  const color =
    ok === null
      ? "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400"
      : ok
        ? "bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-950 dark:text-emerald-300"
        : "bg-red-50 text-red-700 ring-red-600/20 dark:bg-red-950 dark:text-red-300";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-inset ring-transparent ${color}`}>
      <span className={`size-1.5 rounded-full ${ok === null ? "bg-zinc-400" : ok ? "bg-emerald-500" : "bg-red-500"}`} />
      {label}
    </span>
  );
}

export function HealthBar({ health, error }: { health: Health | null; error: string | null }) {
  if (error) return <Pill ok={false} label={`API offline: ${error}`} />;
  if (!health) return <Pill ok={null} label="Checking API…" />;

  const cooldownMin = Math.ceil(health.direct_cooldown_seconds / 60);
  return (
    <div className="flex flex-wrap gap-2">
      <Pill ok={health.database === "ok"} label={`Database ${health.database}`} />
      <Pill ok={true} label={`Proxy mode: ${health.proxy_mode}`} />
      <Pill
        ok={health.proxy !== null ? null : false}
        label={health.proxy ? `Proxy configured: ${health.proxy}` : "No proxy configured"}
      />
      {health.direct_cooldown_seconds > 0 && (
        <Pill ok={null} label={`Direct IP cooling down · ${cooldownMin} min`} />
      )}
    </div>
  );
}
