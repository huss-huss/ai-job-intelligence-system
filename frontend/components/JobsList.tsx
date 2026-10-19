"use client";

import { useEffect, useState } from "react";
import { fetchApplications, fetchJobs, hasViewerToken, getHire, getMatch, getRank, updateApplication, type ApplicationStatus, type Job, type JobSort } from "@/lib/api";
import { formatDate, scoreColor } from "@/lib/utils";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { EligibilityBadge, HireBadge, TopReasons, WhyThisScore } from "@/components/ScoreDetails";
import { StatusBadge, StatusFilterSelect, StatusSelect, statusParam, useAuthKey, type StatusFilter } from "@/components/Tracker";
import { Briefcase, Building2, Eye, EyeOff, MapPin, Loader2 } from "lucide-react";

type Filters = {
  source?: number;
  min_score?: number;
  q?: string;
  sort?: JobSort;
  page: number;
};

type JobsListProps = { filters: Filters; onPageChange?: (page: number) => void; refreshKey?: number };

export function JobsList({ filters, onPageChange, refreshKey = 0 }: JobsListProps) {
  const [data, setData] = useState<{ items: Job[]; total: number; pages: number } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Jobs that are known not to be open to the profile's location are hidden unless asked for.
  const [showIneligible, setShowIneligible] = useState(false);
  const [ineligibleCount, setIneligibleCount] = useState<number | null>(null);
  // Tracker: jobs marked "not for me" are hidden unless the filter asks for them.
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("active");
  const [statuses, setStatuses] = useState<Record<number, ApplicationStatus>>({});
  const [statusError, setStatusError] = useState<string | null>(null);
  // Tracker statuses are private: without a valid token this is the public view, with none of them.
  const [privateView, setPrivateView] = useState(false);
  const authKey = useAuthKey();

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    (async () => {
      const apps = hasViewerToken() ? await fetchApplications().catch(() => null) : null;
      const isPrivate = apps !== null;
      const common = {
        source: filters.source,
        min_score: filters.min_score,
        q: filters.q || undefined,
        status: isPrivate ? statusParam(statusFilter) : undefined,
      };
      const [r, hidden] = await Promise.all([
        fetchJobs({
          ...common,
          page: filters.page,
          page_size: 20,
          sort: filters.sort ?? "rank",
          eligible: showIneligible ? "any" : "true,unknown",
        }),
        // Only the total is needed for the toggle label.
        fetchJobs({ ...common, page: 1, page_size: 1, eligible: "false" }).then((x) => x.total, () => null),
      ]);
      if (cancelled) return;
      setPrivateView(isPrivate);
      setData({ items: r.items, total: r.total, pages: r.pages });
      setIneligibleCount(hidden);
      setStatuses(apps ? Object.fromEntries(apps.map((a) => [a.job_id, a.status])) : {});
    })()
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : "Failed to load"))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [filters.source, filters.min_score, filters.q, filters.sort, filters.page, refreshKey, showIneligible, statusFilter, authKey]);

  const changeStatus = async (jobId: number, status: ApplicationStatus) => {
    const previous = statuses[jobId] ?? "new";
    setStatuses((m) => ({ ...m, [jobId]: status }));
    setStatusError(null);
    try {
      await updateApplication(jobId, { status });
    } catch (e) {
      setStatuses((m) => ({ ...m, [jobId]: previous }));
      setStatusError(e instanceof Error ? e.message : "Update failed");
    }
  };
  const statusSelect = privateView && (
    <StatusFilterSelect
      value={statusFilter}
      onChange={(v) => {
        setStatusFilter(v);
        onPageChange?.(1);
      }}
    />
  );

  const toggleIneligible = () => {
    setShowIneligible((v) => !v);
    onPageChange?.(1);
  };
  const ineligibleToggle =
    ineligibleCount != null && ineligibleCount > 0 ? (
      <Button variant="outline" size="sm" onClick={toggleIneligible} aria-pressed={showIneligible}>
        {showIneligible ? <EyeOff className="h-4 w-4" aria-hidden="true" /> : <Eye className="h-4 w-4" aria-hidden="true" />}
        {showIneligible ? "Hide" : "Show"} {ineligibleCount} job{ineligibleCount !== 1 ? "s" : ""} not open to your location
      </Button>
    ) : null;

  if (error) {
    return (
      <Card>
        <CardContent className="pt-6">
          <p className="text-destructive">{error}</p>
        </CardContent>
      </Card>
    );
  }

  if (loading) {
    return (
      <Card className="border-border/50 shadow-lg shadow-primary/5">
        <CardContent className="pt-8 pb-8">
          <div className="flex flex-col items-center justify-center gap-3">
            <Loader2 className="h-8 w-8 animate-spin text-primary" />
            <p className="text-muted-foreground font-medium">Loading jobs…</p>
          </div>
        </CardContent>
      </Card>
    );
  }

  if (!data || data.items.length === 0) {
    return (
      <Card className="border-border/50 shadow-lg shadow-primary/5">
        <CardContent className="pt-8 pb-8">
          <div className="flex flex-col items-center justify-center gap-3 text-center">
            <Briefcase className="h-12 w-12 text-muted-foreground/40" />
            <p className="text-muted-foreground font-medium">No jobs found</p>
            <p className="text-sm text-muted-foreground/70">
              {ineligibleCount
                ? "Every match is outside your location."
                : privateView && statusFilter !== "active"
                  ? "No jobs with this tracker status."
                  : "Add a source and trigger a scrape to get started."}
            </p>
            {statusSelect}
            {ineligibleToggle}
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-4 animate-fade-in">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <p className="text-sm font-medium text-muted-foreground">
            <span className="text-foreground font-semibold">{data.total}</span> job{data.total !== 1 ? "s" : ""} <span className="text-muted-foreground/60">(page {filters.page} of {data.pages})</span>
          </p>
          {statusSelect}
          {ineligibleToggle}
        </div>
        {onPageChange && data.pages > 1 && (
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={filters.page <= 1}
              onClick={() => onPageChange(filters.page - 1)}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={filters.page >= data.pages}
              onClick={() => onPageChange(filters.page + 1)}
            >
              Next
            </Button>
          </div>
        )}
      </div>
      {statusError && <p className="text-sm text-destructive">{statusError}</p>}
      <div className="space-y-3">
        {data.items.map((job) => (
          <JobCard
            key={job.id}
            job={job}
            status={privateView ? statuses[job.id] ?? "new" : null}
            onStatus={(s) => changeStatus(job.id, s)}
          />
        ))}
      </div>
    </div>
  );
}

/** `status` is null in the public view: no tracker controls or badges at all. */
function JobCard({ job, status, onStatus }: { job: Job; status: ApplicationStatus | null; onStatus: (s: ApplicationStatus) => void }) {
  const score = job.analysis?.score;
  const match = getMatch(job);
  const hire = getHire(job);
  const rank = getRank(job);
  return (
    <Card className="transition-all duration-300 hover:border-primary/50 hover:shadow-lg hover:shadow-primary/10 hover:-translate-y-1 border-border/50 group">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <CardTitle className="text-base font-semibold leading-tight flex-1 min-w-0">
            <a
              href={job.url}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:text-primary transition-colors duration-200 group-hover:underline"
            >
              {job.title}
            </a>
          </CardTitle>
          <div className="flex items-center gap-2">
            {status && <StatusSelect status={status} onChange={onStatus} />}
            {rank != null && (
              <div className={`score-badge ${scoreColor(rank / 100)}`} title="Rank: fit and hire chance combined">
                <span className="font-bold">#{rank}</span>
              </div>
            )}
            {score != null && (
              <div className={`score-badge ${scoreColor(score)}`} title="Fit score">
                <span className="font-bold">{(score * 100).toFixed(0)}%</span>
              </div>
            )}
          </div>
        </div>
        <div className="flex flex-wrap gap-4 text-sm text-muted-foreground mt-2">
          {job.company && (
            <span className="flex items-center gap-1.5 transition-colors group-hover:text-foreground/80">
              <Building2 className="h-4 w-4 text-primary/60" />
              <span className="font-medium">{job.company}</span>
            </span>
          )}
          {job.location && (
            <span className="flex items-center gap-1.5 transition-colors group-hover:text-foreground/80">
              <MapPin className="h-4 w-4 text-primary/60" />
              {job.location}
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2 mt-2">
          {job.analysis && <EligibilityBadge match={match} />}
          {status && status !== "new" && <StatusBadge status={status} />}
          {hire && <HireBadge hire={hire} />}
          {job.analysis?.seniority && (
            <span className="inline-flex items-center rounded-full bg-primary/10 px-2.5 py-0.5 text-xs font-medium text-primary border border-primary/20">
              {job.analysis.seniority}
            </span>
          )}
        </div>
        {match && <TopReasons match={match} />}
        {job.analysis?.skills && job.analysis.skills.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mt-3">
            {job.analysis.skills.slice(0, 5).map((s) => (
              <span
                key={s}
                className="rounded-md bg-secondary/80 px-2.5 py-1 text-xs font-medium text-secondary-foreground border border-border/50 transition-all hover:bg-secondary hover:border-primary/30"
              >
                {s}
              </span>
            ))}
            {job.analysis.skills.length > 5 && (
              <span className="rounded-md bg-secondary/50 px-2.5 py-1 text-xs text-muted-foreground border border-border/50">
                +{job.analysis.skills.length - 5} more
              </span>
            )}
          </div>
        )}
      </CardHeader>
      {(job.description || match) && (
        <CardContent className="pt-0">
          {job.description && (
            <p className="line-clamp-2 text-sm text-muted-foreground leading-relaxed">{job.description}</p>
          )}
          {match && <WhyThisScore match={match} hire={hire} rank={rank} />}
        </CardContent>
      )}
    </Card>
  );
}
