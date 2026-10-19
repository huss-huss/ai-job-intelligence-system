"use client";

import { useId, useState } from "react";
import type { MatchInfo } from "@/lib/api";
import { cn } from "@/lib/utils";
import { CheckCircle2, ChevronDown, HelpCircle, XCircle } from "lucide-react";

// Mirrors WEIGHTS, RISK_PENALTY and INELIGIBLE_CAP in worker/matching.py.
const COMPONENTS = [
  { key: "role", label: "Role", max: 25 },
  { key: "skills", label: "Skills", max: 35 },
  { key: "seniority", label: "Seniority", max: 10 },
  { key: "location", label: "Location", max: 15 },
  { key: "salary", label: "Salary", max: 10 },
  { key: "recency", label: "Recency", max: 5 },
] as const;
const RISK_PENALTY = 10;
const INELIGIBLE_CAP = 20;

// Reasons written by location_eligibility(); the badge already says this, so they're kept out of the top reasons.
const LOCATION_REASON = /^(On-site\/hybrid|Description restricts|Location not specified|Remote, but eligible|Open to applicants|Restricted to)/;

const fmt = (n: number) => (Number.isInteger(n) ? String(n) : n.toFixed(1));

export function riskReasons(match: MatchInfo): string[] {
  return match.reasons.filter((r) => r.startsWith("Risk:"));
}

export function locationReason(match: MatchInfo): string | undefined {
  return match.reasons.find((r) => LOCATION_REASON.test(r));
}

/** Risks first (they cost the most), then the rest in scoring order; location is shown by the badge. */
export function topReasons(match: MatchInfo, n = 3): string[] {
  const risks = riskReasons(match);
  const rest = match.reasons.filter((r) => !r.startsWith("Risk:") && !LOCATION_REASON.test(r));
  return [...risks, ...rest].slice(0, n);
}

export function EligibilityBadge({ match }: { match: MatchInfo | null }) {
  const eligible = match?.eligible ?? null;
  const detail = match ? locationReason(match) : undefined;
  const style =
    eligible === true
      ? { text: "Eligible", Icon: CheckCircle2, cls: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30" }
      : eligible === false
        ? { text: "Not open to your location", Icon: XCircle, cls: "bg-red-500/15 text-red-400 border-red-500/30" }
        : { text: "Eligibility unknown", Icon: HelpCircle, cls: "bg-secondary/80 text-muted-foreground border-border/50" };
  return (
    <span
      className={cn("inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium border", style.cls)}
      title={detail}
    >
      <style.Icon className="h-3.5 w-3.5" aria-hidden="true" />
      {style.text}
      {detail && <span className="sr-only">: {detail}</span>}
    </span>
  );
}

export function TopReasons({ match }: { match: MatchInfo }) {
  const reasons = topReasons(match);
  if (reasons.length === 0) return null;
  return (
    <ul className="mt-3 space-y-1 text-sm text-muted-foreground" aria-label="Top reasons for this score">
      {reasons.map((r) => (
        <li key={r} className="flex gap-2">
          <span aria-hidden="true" className={r.startsWith("Risk:") ? "text-red-400" : "text-primary/70"}>
            •
          </span>
          <span className={r.startsWith("Risk:") ? "text-red-400" : undefined}>{r}</span>
        </li>
      ))}
    </ul>
  );
}

function Bar({ label, points, max, negative = false }: { label: string; points: number; max: number; negative?: boolean }) {
  const pct = max > 0 ? Math.max(0, Math.min(1, Math.abs(points) / max)) * 100 : 0;
  const shown = negative ? (points === 0 ? "0" : `−${fmt(Math.abs(points))}`) : fmt(points);
  return (
    <li className="grid grid-cols-[5.5rem_1fr_4.5rem] items-center gap-3 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <span className="h-1.5 rounded-full bg-secondary overflow-hidden" aria-hidden="true">
        <span
          className={cn("block h-full rounded-full", negative ? "bg-red-500/80" : "bg-primary/80")}
          style={{ width: `${pct}%` }}
        />
      </span>
      <span className="text-right tabular-nums text-foreground/90">
        {shown}
        {!negative && <span className="text-muted-foreground"> / {max}</span>}
        <span className="sr-only"> points</span>
      </span>
    </li>
  );
}

export function WhyThisScore({ match }: { match: MatchInfo }) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const risks = riskReasons(match);
  const riskPoints = risks.length * RISK_PENALTY;
  const sum = COMPONENTS.reduce((acc, c) => acc + (match.breakdown[c.key] ?? 0), 0) - riskPoints;
  const capped = match.eligible === false && sum > INELIGIBLE_CAP;
  const excluded = Object.keys(match.breakdown).length === 0;

  return (
    <div className="mt-3">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls={panelId}
        className="inline-flex items-center gap-1 rounded-md text-xs font-medium text-primary hover:text-primary/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-background"
      >
        <ChevronDown className={cn("h-4 w-4 transition-transform", open && "rotate-180")} aria-hidden="true" />
        Why this score
      </button>
      <div id={panelId} hidden={!open}>
        <div className="mt-3 rounded-lg border border-border/50 bg-secondary/30 p-4 space-y-4 animate-fade-in">
          {excluded ? (
            <p className="text-xs text-muted-foreground">{match.reasons[0] ?? "Not scored"}</p>
          ) : (
            <div>
              <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Breakdown</p>
              <ul className="space-y-2" aria-label="Points per scoring component">
                {COMPONENTS.map((c) => (
                  <Bar key={c.key} label={c.label} points={match.breakdown[c.key] ?? 0} max={c.max} />
                ))}
                <Bar label={risks.length ? `Risk (${risks.length})` : "Risk"} points={-riskPoints} max={Math.max(riskPoints, RISK_PENALTY * 2)} negative />
              </ul>
              <p className="mt-3 text-xs text-muted-foreground">
                Total <span className="font-semibold text-foreground">{match.score}</span> / 100
                {capped && ` (${fmt(sum)} capped at ${INELIGIBLE_CAP}: not open to your location)`}
              </p>
            </div>
          )}
          {match.matched_skills.length > 0 && (
            <div>
              <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Matched skills</p>
              <ul className="flex flex-wrap gap-1.5" aria-label="Matched skills">
                {match.matched_skills.map((s) => (
                  <li key={s} className="rounded-md bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary border border-primary/20">
                    {s}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {match.reasons.length > 0 && (
            <div>
              <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">All reasons</p>
              <ul className="space-y-1 text-xs text-muted-foreground">
                {match.reasons.map((r) => (
                  <li key={r} className="flex gap-2">
                    <span aria-hidden="true" className={r.startsWith("Risk:") ? "text-red-400" : "text-primary/70"}>
                      •
                    </span>
                    <span className={r.startsWith("Risk:") ? "text-red-400" : undefined}>{r}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
