"use client";

import { useState, useEffect } from "react";
import { fetchSources, triggerScrape, triggerAnalyze, fetchRuns, createTestJobs, clearJobs, type JobSort, type Source } from "@/lib/api";
import { JobsList } from "@/components/JobsList";
import { RunsList } from "@/components/RunsList";
import { TalentNetworks } from "@/components/TalentNetworks";
import { ViewerControl } from "@/components/Tracker";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Play, Search, Briefcase, Loader2, Trash2, RefreshCw } from "lucide-react";

export default function DashboardPage() {
  const [sources, setSources] = useState<Source[]>([]);
  const [loadingSources, setLoadingSources] = useState(true);
  const [scraping, setScraping] = useState(false);
  const [scrapeStatus, setScrapeStatus] = useState<"idle" | "enqueuing" | "scraping" | "analyzing" | "completed" | "error">("idle");
  const [scrapeError, setScrapeError] = useState<string | null>(null);
  const [scrapingSourceId, setScrapingSourceId] = useState<number | null>(null); // Track which source is being scraped
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeStatus, setAnalyzeStatus] = useState<"idle" | "enqueuing" | "processing" | "completed" | "error">("idle");
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);
  const [creatingTestJobs, setCreatingTestJobs] = useState(false);
  const [testJobsMessage, setTestJobsMessage] = useState<string | null>(null);
  const [clearingJobs, setClearingJobs] = useState(false);
  const [runsRefresh, setRunsRefresh] = useState(0);
  const [jobsRefresh, setJobsRefresh] = useState(0); // Force refresh of jobs list
  const [filters, setFilters] = useState({
    source: undefined as number | undefined,
    min_score: undefined as number | undefined,
    q: "" as string,
    sort: "rank" as JobSort,
    page: 1,
  });

  useEffect(() => {
    fetchSources()
      .then(setSources)
      .catch(() => setSources([]))
      .finally(() => setLoadingSources(false));
  }, []);

  const handleScrape = async (sourceId?: number) => {
    setScraping(true);
    setScrapeStatus("enqueuing");
    setScrapeError(null);
    setScrapingSourceId(sourceId ?? null);
    try {
      await triggerScrape(sourceId);
      setScrapeStatus("scraping");
      setRunsRefresh((r) => r + 1);

      // Poll runs to detect when scrape completes or fails
      const startTime = Date.now();
      const maxWait = 120000; // 120 seconds max for scrape (Playwright can be slow)
      const pollInterval = setInterval(async () => {
        try {
          const runs = await fetchRuns({ limit: 20 });
          const scrapeRun = runs.find(
            (r) =>
              r.type === "scrape" &&
              (r.status === "completed" || r.status === "failed") &&
              (sourceId == null || r.source_id === sourceId) &&
              new Date(r.started_at).getTime() > startTime - 2000
          );
          if (scrapeRun) {
            clearInterval(pollInterval);
            if (scrapeRun.status === "failed") {
              const errorMsg = scrapeRun.stats?.error || "Scrape task failed";
              setScrapeStatus("error");
              setScrapeError(`Worker error: ${errorMsg}. Check worker logs for details.`);
              setScraping(false);
              setScrapingSourceId(null);
            } else {
              // Scrape completed successfully
              const stats = scrapeRun.stats as { inserted?: number; scraped?: number; error?: string } | null;
              const insertedCount = stats?.inserted ?? 0;
              setRunsRefresh((r) => r + 1);
              setJobsRefresh((r) => r + 1); // Refresh jobs list to show newly scraped jobs
              
              // If new jobs were inserted, wait for analysis to complete
              if (insertedCount > 0) {
                setScrapeStatus("analyzing"); // Change to "analyzing" state
                const analyzeStartTime = Date.now();
                const analyzeMaxWait = 60000; // 60 seconds max for analysis
                const analyzePollInterval = setInterval(async () => {
                  try {
                    const analyzeRuns = await fetchRuns({ limit: 10 });
                    const analyzeDone = analyzeRuns.find(
                      (r) =>
                        r.type === "analyze" &&
                        r.status === "completed" &&
                        new Date(r.started_at).getTime() > analyzeStartTime - 2000
                    );
                    if (analyzeDone) {
                      clearInterval(analyzePollInterval);
                      setScrapeStatus("completed");
                      setJobsRefresh((r) => r + 1); // Refresh jobs list to show updated analysis scores
                      setRunsRefresh((r) => r + 1);
                      setTimeout(() => {
                        setScrapeStatus("idle");
                        setScraping(false);
                        setScrapingSourceId(null);
                      }, 2000);
                    } else if (Date.now() - analyzeStartTime > analyzeMaxWait) {
                      clearInterval(analyzePollInterval);
                      setScrapeStatus("completed");
                      setScraping(false);
                      setScrapingSourceId(null);
                    }
                  } catch {
                    // silent poll errors
                  }
                }, 1500);
              } else {
                // No new jobs, just finish
                setScrapeStatus("completed");
                setTimeout(() => {
                  setScrapeStatus("idle");
                  setScraping(false);
                  setScrapingSourceId(null);
                }, 2000);
              }
            }
          } else if (Date.now() - startTime > maxWait) {
            clearInterval(pollInterval);
            setScrapeStatus("error");
            setScrapeError("Scrape timed out after 2 minutes. The worker may have crashed. Check worker logs.");
            setScraping(false);
            setScrapingSourceId(null);
          }
        } catch {
          // silent poll errors
        }
      }, 1500);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to trigger scrape";
      setScrapeStatus("error");
      setScrapeError(msg);
      setScraping(false);
      setScrapingSourceId(null);
    }
  };

  const handleReanalyze = async () => {
    setAnalyzing(true);
    setAnalyzeStatus("enqueuing");
    setAnalyzeError(null);
    try {
      await triggerAnalyze();
      setAnalyzeStatus("processing");
      setRunsRefresh((r) => r + 1);

      // Poll runs to detect when the analyze run completes
      const startTime = Date.now();
      const maxWait = 30000;
      const pollInterval = setInterval(async () => {
        try {
          const runs = await fetchRuns({ limit: 10 });
          const done = runs.find(
            (r) =>
              r.type === "analyze" &&
              r.status === "completed" &&
              new Date(r.started_at).getTime() > startTime - 2000
          );
          if (done) {
            clearInterval(pollInterval);
            setAnalyzeStatus("completed");
            setRunsRefresh((r) => r + 1);
            setJobsRefresh((r) => r + 1); // Refresh jobs list to show updated analysis scores
            setTimeout(() => { setAnalyzeStatus("idle"); setAnalyzing(false); }, 2000);
          } else if (Date.now() - startTime > maxWait) {
            clearInterval(pollInterval);
            setAnalyzeStatus("idle");
            setAnalyzing(false);
          }
        } catch {
          // silent poll errors
        }
      }, 1500);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to trigger analyze";
      setAnalyzeStatus("error");
      setAnalyzeError(msg);
      setAnalyzing(false);
    }
  };

  const handleCreateTestJobs = async () => {
    setCreatingTestJobs(true);
    setTestJobsMessage(null);
    try {
      const result = await createTestJobs();
      setTestJobsMessage(`✓ Created ${result.inserted} test job(s)${result.skipped > 0 ? ` (${result.skipped} already existed)` : ""}. Analyzing them now...`);
      // Refresh job list immediately to show the new jobs
      setFilters((f) => ({ ...f, page: 1 }));
      setJobsRefresh((r) => r + 1); // Force refresh
      // Auto-trigger analyze for the new jobs
      if (result.inserted > 0) {
        try {
          await triggerAnalyze();
          setRunsRefresh((r) => r + 1);
          setTestJobsMessage(`✓ Created ${result.inserted} job(s). Analyzing...`);
          
          // Poll runs to detect when the analyze run completes
          const startTime = Date.now();
          const maxWait = 60000; // 60 seconds max for analysis
          const pollInterval = setInterval(async () => {
            try {
              const runs = await fetchRuns({ limit: 10 });
              const done = runs.find(
                (r) =>
                  r.type === "analyze" &&
                  r.status === "completed" &&
                  new Date(r.started_at).getTime() > startTime - 2000
              );
              if (done) {
                clearInterval(pollInterval);
                setJobsRefresh((r) => r + 1); // Refresh jobs list to show updated analysis scores
                setRunsRefresh((r) => r + 1);
                setTestJobsMessage(`✓ Created and analyzed ${result.inserted} job(s). Analysis complete!`);
                setTimeout(() => setTestJobsMessage(null), 5000);
              } else if (Date.now() - startTime > maxWait) {
                clearInterval(pollInterval);
                setTestJobsMessage(`✓ Created ${result.inserted} job(s). Analysis may still be running - check Run history.`);
                setTimeout(() => setTestJobsMessage(null), 5000);
              }
            } catch {
              // silent poll errors
            }
          }, 1500);
        } catch {
          // Analyze might fail, but jobs are created
          setTestJobsMessage(`✓ Created ${result.inserted} job(s). Analysis trigger failed - you can manually re-analyze.`);
          setTimeout(() => setTestJobsMessage(null), 5000);
        }
      } else {
        setTimeout(() => setTestJobsMessage(null), 5000);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to create test jobs";
      setTestJobsMessage(`Error: ${msg}`);
      setTimeout(() => setTestJobsMessage(null), 5000);
    } finally {
      setCreatingTestJobs(false);
    }
  };

  const handleClearJobs = async () => {
    if (!confirm("Delete all jobs? This will remove all job listings and their analyses. This cannot be undone.")) return;
    setClearingJobs(true);
    try {
      const result = await clearJobs();
      // Refresh the job list immediately
      setFilters((f) => ({ ...f, page: 1 }));
      setJobsRefresh((r) => r + 1); // Force refresh
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to clear jobs";
      alert(msg);
    } finally {
      setClearingJobs(false);
    }
  };

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b border-border/50 bg-gradient-to-r from-card via-card/95 to-card/90 backdrop-blur-sm sticky top-0 z-50 shadow-lg shadow-primary/5">
        <div className="container mx-auto flex h-16 items-center justify-between px-4">
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-primary/10 border border-primary/20">
              <Briefcase className="h-5 w-5 text-primary" />
            </div>
            <div>
              <h1 className="text-xl font-bold bg-gradient-to-r from-foreground to-foreground/80 bg-clip-text text-transparent">
                AI Job Intelligence
              </h1>
              <p className="text-xs text-muted-foreground">Smart Job Discovery Platform</p>
            </div>
          </div>
          <ViewerControl />
        </div>
      </header>

      <main className="container mx-auto grid gap-6 px-4 py-8 lg:grid-cols-3">
        <div className="lg:col-span-2 space-y-6">
          <Card className="border-border/50 shadow-lg shadow-primary/5 animate-fade-in">
            <CardHeader className="pb-4">
              <div className="flex items-center justify-between">
                <CardTitle className="text-lg font-semibold flex items-center gap-2">
                  <Search className="h-5 w-5 text-primary" />
                  Filters & Search
                </CardTitle>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={handleClearJobs}
                  disabled={clearingJobs}
                  className="text-muted-foreground hover:text-destructive"
                >
                  {clearingJobs ? (
                    <>
                      <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                      Clearing…
                    </>
                  ) : (
                    <>
                      <Trash2 className="mr-1 h-3 w-3" />
                      Clear jobs
                    </>
                  )}
                </Button>
              </div>
            </CardHeader>
            <CardContent>
              <div className="flex flex-wrap gap-3">
                  <select
                    className="rounded-lg border border-border/50 bg-secondary/50 px-3 py-2 text-sm transition-all hover:border-primary/30 focus:border-primary focus:ring-2 focus:ring-primary/20 focus:outline-none"
                    value={filters.source ?? ""}
                    onChange={(e) =>
                      setFilters((f) => ({
                        ...f,
                        source: e.target.value ? Number(e.target.value) : undefined,
                        page: 1,
                      }))
                    }
                  >
                    <option value="">All sources</option>
                    {sources.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.name}
                      </option>
                    ))}
                  </select>
                  <select
                    className="rounded-lg border border-border/50 bg-secondary/50 px-3 py-2 text-sm transition-all hover:border-primary/30 focus:border-primary focus:ring-2 focus:ring-primary/20 focus:outline-none"
                    value={filters.min_score ?? ""}
                    onChange={(e) =>
                      setFilters((f) => ({
                        ...f,
                        min_score: e.target.value ? Number(e.target.value) : undefined,
                        page: 1,
                      }))
                    }
                  >
                    <option value="">Any score</option>
                    <option value="0.7">70%+</option>
                    <option value="0.5">50%+</option>
                  </select>
                  <select
                    aria-label="Sort jobs"
                    className="rounded-lg border border-border/50 bg-secondary/50 px-3 py-2 text-sm transition-all hover:border-primary/30 focus:border-primary focus:ring-2 focus:ring-primary/20 focus:outline-none"
                    value={filters.sort}
                    onChange={(e) => setFilters((f) => ({ ...f, sort: e.target.value as JobSort, page: 1 }))}
                  >
                    <option value="rank">Best chance (fit × hire)</option>
                    <option value="score">Best fit</option>
                    <option value="recent">Newest</option>
                  </select>
                  <div className="flex flex-1 min-w-[200px] items-center gap-2">
                    <Search className="h-4 w-4 text-muted-foreground" />
                    <input
                      type="text"
                      placeholder="Search title or company..."
                      className="flex-1 rounded-lg border border-border/50 bg-secondary/50 px-3 py-2 text-sm placeholder:text-muted-foreground/60 transition-all hover:border-primary/30 focus:border-primary focus:ring-2 focus:ring-primary/20 focus:outline-none"
                      value={filters.q}
                      onChange={(e) =>
                        setFilters((f) => ({ ...f, q: e.target.value, page: 1 }))
                      }
                    />
                  </div>
                </div>
            </CardContent>
          </Card>

          <JobsList
            filters={filters}
            onPageChange={(page) => setFilters((f) => ({ ...f, page }))}
            refreshKey={jobsRefresh}
          />
        </div>

        <div className="space-y-6">
          <Card className="border-border/50 shadow-lg shadow-primary/5 animate-fade-in">
            <CardHeader className="pb-4">
              <CardTitle className="text-lg font-semibold flex items-center gap-2">
                <Play className="h-5 w-5 text-primary" />
                Sources
              </CardTitle>
            </CardHeader>
            <CardContent>
                {loadingSources ? (
                  <div className="flex items-center justify-center gap-2 py-4">
                    <Loader2 className="h-4 w-4 animate-spin text-primary" />
                    <p className="text-sm text-muted-foreground">Loading…</p>
                  </div>
                ) : sources.length === 0 ? (
                  <div className="flex flex-col items-center justify-center gap-2 py-6 text-center">
                    <Play className="h-8 w-8 text-muted-foreground/40" />
                    <p className="text-sm text-muted-foreground font-medium">No sources</p>
                    <p className="text-xs text-muted-foreground/70">Add one via API (POST /api/sources)</p>
                  </div>
                ) : (
                  <ul className="space-y-2">
                    {sources.map((s) => (
                      <li
                        key={s.id}
                        className="flex items-center justify-between rounded-lg border border-border/50 bg-secondary/30 px-3 py-2 text-sm transition-all hover:border-primary/30 hover:bg-secondary/50 hover:shadow-md"
                      >
                        <span className="font-medium">{s.name}</span>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={scraping}
                          onClick={() => handleScrape(s.id)}
                        >
                          {scraping && scrapingSourceId === s.id ? (
                            <>
                              <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                              {scrapeStatus === "enqueuing" ? "Enqueuing…" : scrapeStatus === "scraping" ? "Scraping…" : scrapeStatus === "analyzing" ? "Analyzing…" : scrapeStatus === "completed" ? "✓ Finished" : "Scrape"}
                            </>
                          ) : (
                            <>
                              <Play className="h-4 w-4" />
                              Scrape
                            </>
                          )}
                        </Button>
                      </li>
                    ))}
                  </ul>
                )}
                <Button
                  className="mt-3 w-full font-semibold shadow-md hover:shadow-lg transition-all"
                  variant="secondary"
                  disabled={scraping || sources.length === 0}
                  onClick={() => handleScrape()}
                >
                  {scraping && scrapingSourceId === null ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      {scrapeStatus === "enqueuing" ? "Enqueuing…" : scrapeStatus === "scraping" ? "Scraping…" : scrapeStatus === "analyzing" ? "Analyzing…" : scrapeStatus === "completed" ? "✓ Finished" : "Scrape all"}
                    </>
                  ) : (
                    "Scrape all"
                  )}
                </Button>
                {scraping && scrapingSourceId === null && scrapeStatus === "scraping" && (
                  <p className="mt-2 text-xs text-muted-foreground">
                    Scraping jobs… Waiting for worker to finish.
                  </p>
                )}
                {scraping && scrapingSourceId === null && scrapeStatus === "analyzing" && (
                  <p className="mt-2 text-xs text-muted-foreground">
                    Scraping complete! Analyzing new jobs…
                  </p>
                )}
                {scraping && scrapingSourceId === null && scrapeStatus === "completed" && (
                  <p className="mt-2 text-xs text-emerald-400">
                    ✓ Scraping and analysis complete! Jobs updated.
                  </p>
                )}
                {scrapeStatus === "error" && scrapeError && scrapingSourceId === null && (
                  <div className="mt-2 rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-xs text-red-400">
                    <strong>Error:</strong> {scrapeError}
                    {scrapeError.toLowerCase().includes("worker") && (
                      <div className="mt-1 text-xs opacity-80">
                        Tip: Make sure the worker is running with <code className="bg-background/50 px-1 rounded">OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES</code> on macOS.
                      </div>
                    )}
                  </div>
                )}
                {scrapeStatus === "error" && scrapeError && scrapingSourceId !== null && (
                  <div className="mt-2 rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-xs text-red-400">
                    <strong>Error:</strong> {scrapeError}
                  </div>
                )}
            </CardContent>
          </Card>

          <Card className="border-border/50 shadow-lg shadow-primary/5 animate-fade-in bg-gradient-to-br from-card to-card/50">
            <CardHeader className="pb-4">
              <CardTitle className="text-lg font-semibold flex items-center gap-2">
                <Briefcase className="h-5 w-5 text-primary" />
                Test data
              </CardTitle>
              <p className="text-sm text-muted-foreground font-normal mt-2">
                Create sample jobs for testing filters, pagination, and analysis. Adds 8 test jobs with different companies, locations, and roles.
              </p>
            </CardHeader>
            <CardContent>
              <Button
                className="w-full font-semibold shadow-md hover:shadow-lg transition-all"
                variant="outline"
                disabled={creatingTestJobs}
                onClick={handleCreateTestJobs}
              >
                {creatingTestJobs ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    Creating…
                  </>
                ) : (
                  "Create test jobs"
                )}
              </Button>
              {testJobsMessage && (
                <div className={`mt-2 rounded-md px-3 py-2 text-xs ${testJobsMessage.startsWith("✓") ? "bg-emerald-500/10 text-emerald-400 border border-emerald-500/30" : "bg-destructive/10 text-red-400 border border-destructive/50"}`}>
                  {testJobsMessage}
                  {testJobsMessage.toLowerCase().includes("method not allowed") && (
                    <p className="mt-1 text-muted-foreground">
                      The backend needs a restart to register the /api/jobs/test route. Restart it with: <code className="bg-secondary px-1 py-0.5 rounded text-xs">PYTHONPATH=backend python -m uvicorn main:app --host 0.0.0.0 --port 8000 --app-dir backend</code>
                    </p>
                  )}
                </div>
              )}
            </CardContent>
          </Card>

          <Card className="border-border/50 shadow-lg shadow-primary/5 animate-fade-in bg-gradient-to-br from-card to-card/50">
            <CardHeader className="pb-4">
              <CardTitle className="text-lg font-semibold flex items-center gap-2">
                <RefreshCw className="h-5 w-5 text-primary" />
                Re-analyze jobs
              </CardTitle>
              <p className="text-sm text-muted-foreground font-normal mt-2">
                Analyze runs only when a scrape <strong>inserts new jobs</strong>. If you see &quot;inserted: 0&quot; in run history, no new Analyze run is queued. Click below to run analysis on all current jobs and see &quot;Analyze completed&quot; in run history.
              </p>
            </CardHeader>
            <CardContent>
              <Button
                className="w-full font-semibold shadow-md hover:shadow-lg transition-all"
                variant="outline"
                disabled={analyzing}
                onClick={handleReanalyze}
              >
                {analyzeStatus === "enqueuing" && (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    Enqueuing…
                  </>
                )}
                {analyzeStatus === "processing" && (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    Processing analyze task…
                  </>
                )}
                {analyzeStatus === "completed" && "✓ Completed"}
                {analyzeStatus === "error" && "Re-analyze all jobs"}
                {analyzeStatus === "idle" && "Re-analyze all jobs"}
              </Button>
              {analyzeStatus === "processing" && (
                <p className="mt-2 text-xs text-muted-foreground">
                  Task enqueued — waiting for the worker to finish. Watch Run history below.
                </p>
              )}
              {analyzeStatus === "error" && analyzeError && (
                <div className="mt-2 rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-xs text-red-400">
                  <strong>Error:</strong> {analyzeError}
                  {analyzeError.toLowerCase().includes("not found") && (
                    <p className="mt-1 text-muted-foreground">
                      The backend may not be running or needs a restart to pick up the /api/analyze route.
                      Restart it with: <code className="bg-secondary px-1 py-0.5 rounded text-xs">PYTHONPATH=backend python -m uvicorn main:app --host 0.0.0.0 --port 8000 --app-dir backend</code>
                    </p>
                  )}
                  {analyzeError.toLowerCase().includes("queue") && (
                    <p className="mt-1 text-muted-foreground">
                      Redis may be down. Start it with: <code className="bg-secondary px-1 py-0.5 rounded text-xs">docker compose -f infra/docker-compose.yml up -d redis</code>
                    </p>
                  )}
                </div>
              )}
            </CardContent>
          </Card>

          <TalentNetworks />

          <RunsList key={runsRefresh} />
        </div>
      </main>
    </div>
  );
}
