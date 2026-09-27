export type JobStatus = "pending" | "running" | "done" | "failed" | "cancelled";

export interface ScrapeJob {
  id: number;
  query: string;
  max_pages: number;
  status: JobStatus;
  route: string | null;
  products_found: number;
  pages_done: number;
  /** Set when results ran out before max_pages. */
  last_page: number | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ScrapeTask {
  id: number;
  page: number;
  status: "queued" | "running" | "done" | "failed" | "skipped";
  attempts: number;
  worker_id: string | null;
  route: string | null;
  fell_back: boolean;
  products_found: number;
  errors: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface WorkerInfo {
  id: string;
  hostname: string;
  concurrency: number;
  busy: number;
  tasks_done: number;
  tasks_failed: number;
  started_at: string;
  heartbeat_at: string;
  alive: boolean;
}

export interface Stats {
  jobs: Record<string, number>;
  tasks: Record<string, number>;
  queue_depth: number;
  products: number;
  price_changes_24h: number;
  pages_last_hour: number;
  products_last_hour: number;
  routes_24h: Record<string, { pages: number; succeeded: number; success_rate: number | null }>;
  direct_cooldown_seconds: number;
  workers: WorkerInfo[];
}

export interface PricePoint {
  price: number | null;
  regular_price: number | null;
  job_id: number | null;
  observed_at: string;
}

export interface Product {
  id: number;
  sku: string;
  name: string;
  model: string | null;
  url: string | null;
  image_url: string | null;
  price: number | null;
  regular_price: number | null;
  rating: number | null;
  review_count: number | null;
  availability: string | null;
  search_query: string | null;
  first_seen_at: string;
  updated_at: string;
}

export interface ProductPage {
  total: number;
  limit: number;
  offset: number;
  items: Product[];
}

export interface Health {
  status: string;
  database: string;
  proxy_mode: "fallback" | "always" | "never";
  proxy: string | null;
  direct_cooldown_seconds: number;
}

export interface ProductFilters {
  q?: string;
  search_query?: string;
  min_price?: string;
  max_price?: string;
  sort?: "updated" | "price_asc" | "price_desc" | "rating";
  limit?: number;
  offset?: number;
}

export interface JobError {
  page: number;
  route?: string;
  error: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/backend/${path}`, { cache: "no-store", ...init });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body?.detail;
    const message = Array.isArray(detail)
      ? detail.map((d: { msg: string }) => d.msg).join("; ")
      : detail ?? `Request failed (${res.status})`;
    throw new Error(message);
  }
  return body as T;
}

export const api = {
  health: () => request<Health>("health"),
  jobs: (limit = 20) => request<ScrapeJob[]>(`scrape/jobs?limit=${limit}`),
  tasks: (jobId: number) => request<ScrapeTask[]>(`scrape/jobs/${jobId}/tasks`),
  cancel: (jobId: number) => request<ScrapeJob>(`scrape/jobs/${jobId}/cancel`, { method: "POST" }),
  stats: () => request<Stats>("stats"),
  history: (sku: string) => request<PricePoint[]>(`products/${encodeURIComponent(sku)}/history`),
  startScrape: (query: string, maxPages: number) =>
    request<ScrapeJob>("scrape", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ query, max_pages: maxPages }),
    }),
  products: (filters: ProductFilters) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined && value !== "") params.set(key, String(value));
    }
    return request<ProductPage>(`products?${params}`);
  },
};

export function parseErrors(raw: string | null): JobError[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [{ page: 0, error: String(parsed) }];
  } catch {
    return [{ page: 0, error: raw }];
  }
}

export const isActive = (job: ScrapeJob) => job.status === "pending" || job.status === "running";

export const formatPrice = (value: number | null) =>
  value === null ? "—" : value.toLocaleString("en-US", { style: "currency", currency: "USD" });
