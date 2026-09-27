"use client";

import { useState } from "react";
import { api, type ScrapeJob } from "@/lib/api";

export function ScrapeForm({ onStarted }: { onStarted: (job: ScrapeJob) => void }) {
  const [query, setQuery] = useState("");
  const [pages, setPages] = useState(1);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!query.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      const job = await api.startScrape(query.trim(), pages);
      onStarted(job);
      setQuery("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-3 sm:flex-row sm:items-end">
      <label className="flex flex-1 flex-col gap-1 text-sm">
        <span className="font-medium">Search Best Buy for</span>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="e.g. gaming laptop, 4k tv, airpods"
          maxLength={255}
          className="rounded-lg border border-zinc-300 bg-white px-3 py-2 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-zinc-700 dark:bg-zinc-900"
        />
      </label>
      <label className="flex flex-col gap-1 text-sm sm:w-28">
        <span className="font-medium">Pages</span>
        <input
          type="number"
          min={1}
          max={50}
          value={pages}
          onChange={(e) => setPages(Math.min(50, Math.max(1, Number(e.target.value) || 1)))}
          className="rounded-lg border border-zinc-300 bg-white px-3 py-2 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-zinc-700 dark:bg-zinc-900"
        />
      </label>
      <button
        type="submit"
        disabled={submitting || !query.trim()}
        className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {submitting ? "Starting…" : "Start scrape"}
      </button>
      {error && <p className="text-sm text-red-600 sm:basis-full">{error}</p>}
    </form>
  );
}
