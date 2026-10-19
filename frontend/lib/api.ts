// For production: use full URL, for dev: use empty string (rewrites handle it)
const API_BASE = process.env.NEXT_PUBLIC_API_URL || (typeof window !== "undefined" ? window.location.origin.replace(/:\d+$/, ":8000") : "");

export type Job = {
  id: number;
  source_id: number;
  url: string;
  title: string;
  company: string | null;
  location: string | null;
  description: string | null;
  posted_at: string | null;
  content_hash: string;
  created_at: string;
  analysis: {
    seniority: string | null;
    skills: string[] | null;
    salary_min: number | null;
    salary_max: number | null;
    extracted_json: Record<string, unknown>;
    score: number;
    created_at: string;
  } | null;
};

/** Explainable score stored by the worker in analysis.extracted_json.match (see worker/matching.py). */
export type MatchInfo = {
  score: number; // 0-100
  eligible: boolean | null; // null: the posting doesn't say who can apply
  breakdown: Partial<Record<"role" | "skills" | "seniority" | "location" | "salary" | "recency", number>>;
  reasons: string[];
  matched_skills: string[];
};

export function getMatch(job: Job): MatchInfo | null {
  const m = job.analysis?.extracted_json?.match as Partial<MatchInfo> | undefined;
  if (!m || typeof m !== "object") return null;
  return {
    score: typeof m.score === "number" ? m.score : Math.round((job.analysis?.score ?? 0) * 100),
    eligible: typeof m.eligible === "boolean" ? m.eligible : null,
    breakdown: m.breakdown && typeof m.breakdown === "object" ? m.breakdown : {},
    reasons: Array.isArray(m.reasons) ? m.reasons : [],
    matched_skills: Array.isArray(m.matched_skills) ? m.matched_skills : [],
  };
}

/** Hire probability stored in analysis.extracted_json.hire (see worker/hire.py). */
export type HireFactor = "eligibility" | "timezone" | "language" | "competition" | "freshness" | "seniority" | "employment";
export type HireInfo = {
  score: number; // 0-100
  factors: Partial<Record<HireFactor, number>>; // each 0-1
  reasons: string[];
};

export function getHire(job: Job): HireInfo | null {
  const h = job.analysis?.extracted_json?.hire as Partial<HireInfo> | undefined;
  if (!h || typeof h !== "object" || typeof h.score !== "number") return null;
  return {
    score: h.score,
    factors: h.factors && typeof h.factors === "object" ? h.factors : {},
    reasons: Array.isArray(h.reasons) ? h.reasons : [],
  };
}

/** Ranking score: geometric mean of fit and hire probability, 0-100 (worker/hire.py rank_score). */
export function getRank(job: Job): number | null {
  const r = job.analysis?.extracted_json?.rank;
  return typeof r === "number" ? r : null;
}

export type JobSort = "rank" | "score" | "recent";

/** Location eligibility filter for GET /api/jobs: "any" or a comma-separated subset of true,false,unknown. */
export type EligibleFilter = "any" | "true" | "false" | "unknown" | "true,unknown";

export type Source = {
  id: number;
  name: string;
  base_url: string;
  config: Record<string, unknown> | null;
  enabled: boolean;
  created_at: string;
  updated_at: string;
};

export type Run = {
  id: number;
  type: string;
  source_id: number | null;
  status: string;
  started_at: string;
  ended_at: string | null;
  stats: Record<string, unknown> | null;
};

export type JobsResponse = {
  items: Job[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
};

export async function fetchHealth(): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE}/api/health`);
  if (!res.ok) throw new Error("Health check failed");
  return res.json();
}

export async function fetchJobs(params: {
  source?: number;
  min_score?: number;
  q?: string;
  page?: number;
  page_size?: number;
  eligible?: EligibleFilter;
  status?: string; // comma-separated tracker statuses; "new" includes untracked jobs
  sort?: JobSort;
}): Promise<JobsResponse> {
  const sp = new URLSearchParams();
  if (params.source != null) sp.set("source", String(params.source));
  if (params.min_score != null) sp.set("min_score", String(params.min_score));
  if (params.q) sp.set("q", params.q);
  if (params.page != null) sp.set("page", String(params.page));
  if (params.page_size != null) sp.set("page_size", String(params.page_size));
  if (params.eligible) sp.set("eligible", params.eligible);
  if (params.status) sp.set("status", params.status);
  if (params.sort) sp.set("sort", params.sort);
  const res = await fetch(`${API_BASE}/api/jobs?${sp}`);
  if (!res.ok) throw new Error("Failed to fetch jobs");
  return res.json();
}

export async function fetchJob(id: number): Promise<Job> {
  const res = await fetch(`${API_BASE}/api/jobs/${id}`);
  if (!res.ok) throw new Error("Job not found");
  return res.json();
}

export async function fetchSources(): Promise<Source[]> {
  const res = await fetch(`${API_BASE}/api/sources`);
  if (!res.ok) throw new Error("Failed to fetch sources");
  return res.json();
}

export async function fetchRuns(params?: { type?: string; limit?: number }): Promise<Run[]> {
  const sp = new URLSearchParams();
  if (params?.type) sp.set("type", params.type);
  if (params?.limit != null) sp.set("limit", String(params.limit));
  const res = await fetch(`${API_BASE}/api/runs?${sp}`);
  if (!res.ok) throw new Error("Failed to fetch runs");
  return res.json();
}

export async function triggerScrape(sourceId?: number): Promise<{ job_id?: string; jobs?: { source_id: number; job_id: string }[]; message: string }> {
  const res = await fetch(`${API_BASE}/api/scrape`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(sourceId != null ? { source_id: sourceId } : {}),
  });
  if (!res.ok) throw new Error("Scrape trigger failed");
  return res.json();
}

export async function triggerAnalyze(jobIds?: number[]): Promise<{ job_id: string; message: string; job_count: number }> {
  const res = await fetch(`${API_BASE}/api/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(jobIds != null ? { job_ids: jobIds } : {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = typeof data?.detail === "string" ? data.detail : Array.isArray(data?.detail) ? data.detail.map((d: { msg?: string }) => d?.msg).filter(Boolean).join(", ") : "Analyze trigger failed";
    throw new Error(msg);
  }
  return data;
}

export async function createTestJobs(): Promise<{ message: string; inserted: number; skipped: number; total: number }> {
  const res = await fetch(`${API_BASE}/api/jobs/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = typeof data?.detail === "string" ? data.detail : "Failed to create test jobs";
    if (res.status === 405) {
      msg = "Method Not Allowed - backend needs restart to register /api/jobs/test route";
    }
    throw new Error(msg);
  }
  return data;
}

export async function clearRuns(): Promise<{ message: string; deleted: number }> {
  const res = await fetch(`${API_BASE}/api/runs`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = typeof data?.detail === "string" ? data.detail : "Failed to clear runs";
    if (res.status === 405) {
      msg = "Method Not Allowed - backend needs restart to register DELETE /api/runs route";
    }
    throw new Error(msg);
  }
  return data;
}

export async function clearJobs(): Promise<{ message: string; deleted_jobs: number; deleted_analyses: number }> {
  const res = await fetch(`${API_BASE}/api/jobs`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = typeof data?.detail === "string" ? data.detail : "Failed to clear jobs";
    if (res.status === 405) {
      msg = "Method Not Allowed - backend needs restart to register DELETE /api/jobs route";
    }
    throw new Error(msg);
  }
  return data;
}

// ---------------------------------------------------------------- tracker & talent networks

/** Mirrors APPLICATION_STATUSES in backend/app/models/tracker.py. */
export const APPLICATION_STATUSES = ["new", "saved", "applied", "interviewing", "offer", "rejected", "not_interested"] as const;
export type ApplicationStatus = (typeof APPLICATION_STATUSES)[number];

export const TALENT_STATUSES = ["not_started", "applied", "vetted", "active"] as const;
export type TalentStatus = (typeof TALENT_STATUSES)[number];

export type Application = {
  job_id: number;
  status: ApplicationStatus;
  notes: string | null;
  created_at: string | null;
  updated_at: string | null;
  status_changed_at: string | null;
  applied_at: string | null;
  notified_at: string | null;
  job: { title: string; company: string | null; location: string | null; url: string; score: number | null };
};

export type TalentNetwork = {
  slug: string;
  name: string;
  url: string;
  summary: string;
  status: TalentStatus;
  updated_at: string | null;
};

const ADMIN_TOKEN_KEY = "firstfetch.adminToken";

/** On a deployment with ADMIN_TOKEN set, writes need it. It's kept in this browser only. */
export function getAdminToken(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(ADMIN_TOKEN_KEY) ?? "";
}

export function setAdminToken(token: string): void {
  if (token) window.localStorage.setItem(ADMIN_TOKEN_KEY, token);
  else window.localStorage.removeItem(ADMIN_TOKEN_KEY);
}

async function writeJson<T>(path: string, body: unknown): Promise<T> {
  const token = getAdminToken();
  const res = await fetch(`${API_BASE}${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...(token ? { "X-Admin-Token": token } : {}) },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 403) throw new Error("Read-only: enter the admin token to make changes");
    throw new Error(typeof data?.detail === "string" ? data.detail : `Request failed (${res.status})`);
  }
  return data as T;
}

export async function fetchApplications(status?: string): Promise<Application[]> {
  const sp = new URLSearchParams();
  if (status) sp.set("status", status);
  const res = await fetch(`${API_BASE}/api/applications?${sp}`);
  if (!res.ok) throw new Error("Failed to fetch applications");
  return res.json();
}

export function updateApplication(jobId: number, body: { status?: ApplicationStatus; notes?: string }): Promise<Application> {
  return writeJson(`/api/applications/${jobId}`, body);
}

export async function fetchTalentNetworks(): Promise<TalentNetwork[]> {
  const res = await fetch(`${API_BASE}/api/talent-networks`);
  if (!res.ok) throw new Error("Failed to fetch talent networks");
  return res.json();
}

export function updateTalentNetwork(slug: string, status: TalentStatus): Promise<TalentNetwork> {
  return writeJson(`/api/talent-networks/${slug}`, { status });
}
