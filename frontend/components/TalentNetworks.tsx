"use client";

import { useEffect, useState } from "react";
import { fetchTalentNetworks, TALENT_STATUSES, updateTalentNetwork, type TalentNetwork, type TalentStatus } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { ExternalLink, Loader2, Network } from "lucide-react";

const LABELS: Record<TalentStatus, string> = {
  not_started: "Not started",
  applied: "Applied",
  vetted: "Vetted",
  active: "Active",
};
const BADGE: Record<TalentStatus, string> = {
  not_started: "bg-secondary/80 text-muted-foreground border-border/50",
  applied: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  vetted: "bg-violet-500/15 text-violet-300 border-violet-500/30",
  active: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
};

export function TalentNetworks() {
  const [networks, setNetworks] = useState<TalentNetwork[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);

  useEffect(() => {
    fetchTalentNetworks()
      .then(setNetworks)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"));
  }, []);

  const change = async (slug: string, status: TalentStatus) => {
    setSaving(slug);
    setError(null);
    try {
      const updated = await updateTalentNetwork(slug, status);
      setNetworks((list) => list?.map((n) => (n.slug === slug ? updated : n)) ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Update failed");
    } finally {
      setSaving(null);
    }
  };

  const done = networks?.filter((n) => n.status !== "not_started").length ?? 0;

  return (
    <Card id="talent-networks" className="border-border/50 shadow-lg shadow-primary/5 animate-fade-in">
      <CardHeader className="pb-3">
        <CardTitle className="text-lg font-semibold flex items-center gap-2">
          <Network className="h-5 w-5 text-primary" />
          Talent networks
        </CardTitle>
        <p className="text-xs text-muted-foreground">
          Apply once, get vetted, then get matched to many remote roles. {networks ? `${done} of ${networks.length} started.` : ""}
        </p>
      </CardHeader>
      <CardContent>
        {error && <p className="mb-3 text-xs text-destructive">{error}</p>}
        {!networks ? (
          <div className="flex items-center justify-center gap-2 py-4">
            <Loader2 className="h-4 w-4 animate-spin text-primary" />
            <p className="text-sm text-muted-foreground">Loading…</p>
          </div>
        ) : (
          <ul className="space-y-3">
            {networks.map((n) => (
              <li key={n.slug} className="rounded-lg border border-border/50 bg-secondary/20 p-3">
                <div className="flex items-center justify-between gap-2">
                  <a
                    href={n.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex items-center gap-1 text-sm font-semibold hover:text-primary"
                  >
                    {n.name}
                    <ExternalLink className="h-3 w-3" aria-hidden="true" />
                  </a>
                  <div className="flex items-center gap-2">
                    <span className={cn("rounded-full border px-2 py-0.5 text-[11px] font-medium", BADGE[n.status])}>
                      {LABELS[n.status]}
                    </span>
                    <select
                      className="rounded-md border border-border/50 bg-secondary/50 px-1.5 py-0.5 text-xs focus:border-primary focus:outline-none"
                      value={n.status}
                      disabled={saving === n.slug}
                      onChange={(e) => change(n.slug, e.target.value as TalentStatus)}
                      aria-label={`${n.name} status`}
                    >
                      {TALENT_STATUSES.map((s) => (
                        <option key={s} value={s}>
                          {LABELS[s]}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
                <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{n.summary}</p>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
