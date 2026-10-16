"use client";

import { useEffect, useState } from "react";
import { fetchJobs, type Job } from "@/lib/api";
import { formatDate, scoreColor } from "@/lib/utils";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Briefcase, Building2, MapPin, Loader2 } from "lucide-react";

type Filters = {
  source?: number;
  min_score?: number;
  q?: string;
  page: number;
};

type JobsListProps = { filters: Filters; onPageChange?: (page: number) => void; refreshKey?: number };

export function JobsList({ filters, onPageChange, refreshKey = 0 }: JobsListProps) {
  const [data, setData] = useState<{ items: Job[]; total: number; pages: number } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    fetchJobs({
      source: filters.source,
      min_score: filters.min_score,
      q: filters.q || undefined,
      page: filters.page,
      page_size: 20,
    })
      .then((r) => setData({ items: r.items, total: r.total, pages: r.pages }))
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"))
      .finally(() => setLoading(false));
  }, [filters.source, filters.min_score, filters.q, filters.page, refreshKey]);

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
            <p className="text-sm text-muted-foreground/70">Add a source and trigger a scrape to get started.</p>
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-4 animate-fade-in">
      <div className="flex items-center justify-between">
        <p className="text-sm font-medium text-muted-foreground">
          <span className="text-foreground font-semibold">{data.total}</span> job{data.total !== 1 ? "s" : ""} <span className="text-muted-foreground/60">(page {filters.page} of {data.pages})</span>
        </p>
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
      <div className="space-y-3">
        {data.items.map((job) => (
          <JobCard key={job.id} job={job} />
        ))}
      </div>
    </div>
  );
}

function JobCard({ job }: { job: Job }) {
  const score = job.analysis?.score;
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
          {score != null && (
            <div className={`score-badge ${scoreColor(score)}`} title="AI opportunity score">
              <span className="font-bold">{(score * 100).toFixed(0)}%</span>
            </div>
          )}
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
        {job.analysis?.seniority && (
          <div className="mt-2">
            <span className="inline-flex items-center rounded-full bg-primary/10 px-2.5 py-0.5 text-xs font-medium text-primary border border-primary/20">
              {job.analysis.seniority}
            </span>
          </div>
        )}
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
      {job.description && (
        <CardContent className="pt-0">
          <p className="line-clamp-2 text-sm text-muted-foreground leading-relaxed">{job.description}</p>
        </CardContent>
      )}
    </Card>
  );
}
