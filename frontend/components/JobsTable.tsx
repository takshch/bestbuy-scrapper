"use client";

import { useEffect, useState } from "react";
import { api, isActive, parseErrors, type JobStatus, type ScrapeJob, type ScrapeTask } from "@/lib/api";

const STATUS_STYLES: Record<string, string> = {
  pending: "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  queued: "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  running: "bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
  done: "bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
  failed: "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300",
  cancelled: "bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  skipped: "bg-zinc-50 text-zinc-400 dark:bg-zinc-900 dark:text-zinc-500",
};

function StatusBadge({ status }: { status: JobStatus | ScrapeTask["status"] }) {
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_STYLES[status]}`}>
      {status === "running" && <span className="size-1.5 animate-pulse rounded-full bg-current" />}
      {status}
    </span>
  );
}

function duration(job: ScrapeJob) {
  const start = new Date(job.started_at ?? job.created_at);
  const end = job.finished_at ? new Date(job.finished_at) : new Date();
  const secs = Math.max(0, Math.round((end.getTime() - start.getTime()) / 1000));
  return secs < 60 ? `${secs}s` : `${Math.floor(secs / 60)}m ${secs % 60}s`;
}

function Progress({ job }: { job: ScrapeJob }) {
  const total = job.last_page ?? job.max_pages;
  const pct = total ? Math.round((job.pages_done / total) * 100) : 100;
  return (
    <div className="w-28">
      <div className="h-1.5 overflow-hidden rounded-full bg-zinc-100 dark:bg-zinc-800">
        <div className="h-full rounded-full bg-blue-500 transition-all" style={{ width: `${pct}%` }} />
      </div>
      <div className="mt-1 text-xs tabular-nums text-zinc-500">
        {job.pages_done}/{total} pages
        {job.last_page !== null && job.last_page < job.max_pages && " (end of results)"}
      </div>
    </div>
  );
}

function TaskList({ jobId }: { jobId: number }) {
  const [tasks, setTasks] = useState<ScrapeTask[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.tasks(jobId).then(
      (t) => !cancelled && setTasks(t),
      (e) => !cancelled && setError(String(e)),
    );
    return () => {
      cancelled = true;
    };
  }, [jobId]);
  if (error) return <p className="text-xs text-red-600">{error}</p>;
  if (!tasks) return <p className="text-xs text-zinc-500">Loading pages…</p>;

  return (
    <table className="mt-2 w-full text-xs">
      <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
        {tasks.map((t) => (
          <tr key={t.id} className="align-top">
            <td className="py-1 pr-3 tabular-nums text-zinc-500">p{t.page}</td>
            <td className="py-1 pr-3"><StatusBadge status={t.status} /></td>
            <td className="py-1 pr-3 tabular-nums">{t.products_found} items</td>
            <td className="py-1 pr-3 text-zinc-500">{t.attempts > 1 ? `${t.attempts} attempts` : ""}</td>
            <td className="py-1 pr-3 text-zinc-600 dark:text-zinc-400">
              {t.route ?? "—"}
              {parseErrors(t.errors).map((e, i) => (
                <div key={i} className="text-amber-700 dark:text-amber-400">
                  {e.route ? `${e.route}: ` : ""}{e.error}
                </div>
              ))}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function JobsTable({
  jobs,
  onSelectQuery,
  onCancelled,
}: {
  jobs: ScrapeJob[];
  onSelectQuery: (query: string) => void;
  onCancelled: (job: ScrapeJob) => void;
}) {
  const [expanded, setExpanded] = useState<number | null>(null);

  if (jobs.length === 0) {
    return <p className="py-6 text-center text-sm text-zinc-500">No scrape jobs yet. Start one above.</p>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-zinc-200 text-xs uppercase text-zinc-500 dark:border-zinc-800">
          <tr>
            <th className="py-2 pr-4 font-medium">#</th>
            <th className="py-2 pr-4 font-medium">Query</th>
            <th className="py-2 pr-4 font-medium">Status</th>
            <th className="py-2 pr-4 font-medium">Progress</th>
            <th className="py-2 pr-4 font-medium">Products</th>
            <th className="py-2 pr-4 font-medium">Routes / issues</th>
            <th className="py-2 pr-4 font-medium">Time</th>
            <th className="py-2 font-medium" />
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
          {jobs.map((job) => {
            const errors = parseErrors(job.error);
            const open = expanded === job.id;
            return (
              <tr key={job.id} className="align-top">
                <td className="py-2 pr-4 tabular-nums text-zinc-500">{job.id}</td>
                <td className="py-2 pr-4">
                  <button
                    onClick={() => onSelectQuery(job.query)}
                    className="font-medium text-blue-600 hover:underline dark:text-blue-400"
                    title="Show products from this search"
                  >
                    {job.query}
                  </button>
                </td>
                <td className="py-2 pr-4"><StatusBadge status={job.status} /></td>
                <td className="py-2 pr-4"><Progress job={job} /></td>
                <td className="py-2 pr-4 tabular-nums">{job.products_found}</td>
                <td className="max-w-md py-2 pr-4 text-xs">
                  <div className="text-zinc-600 dark:text-zinc-400">{job.route ?? "—"}</div>
                  <button
                    onClick={() => setExpanded(open ? null : job.id)}
                    className="mt-1 text-blue-600 hover:underline dark:text-blue-400"
                  >
                    {open ? "Hide pages" : "Pages"}
                    {errors.length > 0 && (
                      <span className="text-amber-700 dark:text-amber-400"> · {errors.length} issue{errors.length > 1 ? "s" : ""}</span>
                    )}
                  </button>
                  {/* Keyed by status so the list refetches as the job progresses. */}
                  {open && <TaskList key={`${job.id}-${job.pages_done}-${job.status}`} jobId={job.id} />}
                </td>
                <td className="whitespace-nowrap py-2 pr-4 text-xs text-zinc-500">
                  {new Date(job.created_at).toLocaleTimeString()}
                  <div>{duration(job)}</div>
                </td>
                <td className="py-2">
                  {isActive(job) && (
                    <button
                      onClick={() => api.cancel(job.id).then(onCancelled)}
                      className="rounded-md border border-zinc-300 px-2 py-0.5 text-xs hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-800"
                    >
                      Cancel
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
