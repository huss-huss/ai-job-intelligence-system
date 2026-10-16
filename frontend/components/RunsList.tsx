"use client";

import { useEffect, useState } from "react";
import { fetchRuns, clearRuns, type Run } from "@/lib/api";
import { formatDate } from "@/lib/utils";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { RefreshCw, CheckCircle, XCircle, Loader2, Trash2 } from "lucide-react";

export function RunsList() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [loading, setLoading] = useState(true);
  const [clearing, setClearing] = useState(false);

  useEffect(() => {
    fetchRuns({ limit: 30 })
      .then(setRuns)
      .catch(() => setRuns([]))
      .finally(() => setLoading(false));
  }, []); // Parent can remount with key to refresh

  const hasRunning = runs.some((r) => r.status === "running");
  useEffect(() => {
    if (!hasRunning) return;
    const id = setInterval(
      () => fetchRuns({ limit: 30 }).then(setRuns).catch(() => {}),
      2500
    );
    return () => clearInterval(id);
  }, [hasRunning]);

  const handleClear = async () => {
    if (!confirm("Delete all run history? This cannot be undone.")) return;
    setClearing(true);
    try {
      const result = await clearRuns();
      setRuns([]);
      // Show success briefly
      setTimeout(() => {}, 1000);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to clear runs";
      if (msg.toLowerCase().includes("method not allowed")) {
        alert("Backend needs restart to register DELETE /api/runs route. Restart the backend server.");
      } else {
        alert(msg);
      }
    } finally {
      setClearing(false);
    }
  };

  if (loading) {
    return (
      <Card className="border-border/50 shadow-lg shadow-primary/5">
        <CardContent className="pt-6">
          <div className="flex items-center justify-center gap-2">
            <Loader2 className="h-5 w-5 animate-spin text-primary" />
            <p className="text-muted-foreground">Loading runs…</p>
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="border-border/50 shadow-lg shadow-primary/5 animate-fade-in">
      <CardHeader className="pb-4">
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2 text-lg font-semibold">
            <RefreshCw className="h-5 w-5 text-primary" />
            Run history
          </CardTitle>
          {runs.length > 0 && (
            <Button
              size="sm"
              variant="ghost"
              onClick={handleClear}
              disabled={clearing}
              className="text-muted-foreground hover:text-destructive"
            >
              {clearing ? (
                <>
                  <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                  Clearing…
                </>
              ) : (
                <>
                  <Trash2 className="mr-1 h-3 w-3" />
                  Clear
                </>
              )}
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {runs.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-2 py-6 text-center">
            <RefreshCw className="h-8 w-8 text-muted-foreground/40" />
            <p className="text-sm text-muted-foreground font-medium">No runs yet</p>
            <p className="text-xs text-muted-foreground/70">Trigger a scrape from Sources to see activity.</p>
          </div>
        ) : (
          <ul className="space-y-2">
            {runs.map((r) => (
              <li
                key={r.id}
                className="grid grid-cols-[auto_auto_auto_1fr] items-center gap-3 rounded-lg border border-border/50 bg-secondary/30 px-3 py-2 text-sm sm:gap-4 transition-all hover:bg-secondary/50 hover:border-primary/30 hover:shadow-md"
              >
                <span className="font-medium capitalize shrink-0">{r.type}</span>
                <span className="flex items-center gap-1.5 text-muted-foreground shrink-0">
                  {r.status === "completed" && <CheckCircle className="h-4 w-4 shrink-0 text-emerald-500" />}
                  {r.status === "failed" && <XCircle className="h-4 w-4 shrink-0 text-destructive" />}
                  {r.status === "running" && <Loader2 className="h-4 w-4 shrink-0 animate-spin" />}
                  <span className="capitalize">{r.status}</span>
                </span>
                <span className="text-muted-foreground whitespace-nowrap shrink-0 tabular-nums">
                  {formatDate(r.started_at)}
                </span>
                {r.stats && Object.keys(r.stats).length > 0 && (
                  <span className="text-xs text-muted-foreground whitespace-nowrap min-w-0 overflow-hidden text-ellipsis">
                    {JSON.stringify(r.stats)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
