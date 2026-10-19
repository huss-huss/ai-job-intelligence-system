"use client";

import { useEffect, useState } from "react";
import { APPLICATION_STATUSES, getAdminToken, setAdminToken, type ApplicationStatus } from "@/lib/api";
import { cn } from "@/lib/utils";
import { KeyRound } from "lucide-react";

export const STATUS_META: Record<ApplicationStatus, { label: string; cls: string }> = {
  new: { label: "New", cls: "bg-secondary/80 text-muted-foreground border-border/50" },
  saved: { label: "Saved", cls: "bg-amber-500/15 text-amber-300 border-amber-500/30" },
  applied: { label: "Applied", cls: "bg-sky-500/15 text-sky-300 border-sky-500/30" },
  interviewing: { label: "Interviewing", cls: "bg-violet-500/15 text-violet-300 border-violet-500/30" },
  offer: { label: "Offer", cls: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30" },
  rejected: { label: "Rejected", cls: "bg-red-500/15 text-red-300 border-red-500/30" },
  not_interested: { label: "Not for me", cls: "bg-zinc-500/15 text-zinc-400 border-zinc-500/30" },
};

/** Dashboard filter values; "active" hides jobs you marked not for you. */
export type StatusFilter = "active" | "all" | ApplicationStatus;

export function statusParam(filter: StatusFilter): string | undefined {
  if (filter === "all") return undefined;
  if (filter === "active") return APPLICATION_STATUSES.filter((s) => s !== "not_interested").join(",");
  return filter;
}

const selectCls =
  "rounded-lg border border-border/50 bg-secondary/50 px-3 py-2 text-sm transition-all hover:border-primary/30 focus:border-primary focus:ring-2 focus:ring-primary/20 focus:outline-none";

export function StatusFilterSelect({ value, onChange }: { value: StatusFilter; onChange: (v: StatusFilter) => void }) {
  return (
    <select className={selectCls} value={value} onChange={(e) => onChange(e.target.value as StatusFilter)} aria-label="Tracker status">
      <option value="active">All but not for me</option>
      <option value="all">All statuses</option>
      {APPLICATION_STATUSES.map((s) => (
        <option key={s} value={s}>
          {STATUS_META[s].label}
        </option>
      ))}
    </select>
  );
}

export function StatusBadge({ status }: { status: ApplicationStatus }) {
  const meta = STATUS_META[status] ?? STATUS_META.new;
  return (
    <span className={cn("inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium border", meta.cls)}>
      {meta.label}
    </span>
  );
}

export function StatusSelect({
  status,
  onChange,
  disabled,
}: {
  status: ApplicationStatus;
  onChange: (s: ApplicationStatus) => void;
  disabled?: boolean;
}) {
  return (
    <select
      className="rounded-md border border-border/50 bg-secondary/50 px-2 py-1 text-xs focus:border-primary focus:outline-none"
      value={status}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value as ApplicationStatus)}
      aria-label="Change tracker status"
    >
      {APPLICATION_STATUSES.map((s) => (
        <option key={s} value={s}>
          {STATUS_META[s].label}
        </option>
      ))}
    </select>
  );
}

/** Token for writes on a read-only deployment. Stored in localStorage, sent as X-Admin-Token. */
export function AdminTokenField() {
  const [value, setValue] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    setValue(getAdminToken());
    setSaved(!!getAdminToken());
  }, []);
  return (
    <form
      className="flex items-center gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        setAdminToken(value.trim());
        setSaved(!!value.trim());
      }}
    >
      <KeyRound className={cn("h-4 w-4", saved ? "text-emerald-400" : "text-muted-foreground")} aria-hidden="true" />
      <input
        type="password"
        autoComplete="off"
        placeholder="Admin token (for edits)"
        aria-label="Admin token"
        className="w-44 rounded-md border border-border/50 bg-secondary/50 px-2 py-1 text-xs placeholder:text-muted-foreground/60 focus:border-primary focus:outline-none"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onBlur={() => {
          setAdminToken(value.trim());
          setSaved(!!value.trim());
        }}
      />
    </form>
  );
}
