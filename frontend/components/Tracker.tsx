"use client";

import { useEffect, useState } from "react";
import { APPLICATION_STATUSES, AUTH_EVENT, loginWithAdminToken, logout, resolveViewer, type ApplicationStatus, type Me } from "@/lib/api";
import { cn } from "@/lib/utils";
import { KeyRound, Loader2, LogOut, UserRound } from "lucide-react";

export const STATUS_META: Record<ApplicationStatus, { label: string; cls: string }> = {
  new: { label: "New", cls: "bg-secondary/80 text-muted-foreground border-border/50" },
  saved: { label: "Saved", cls: "bg-amber-500/15 text-amber-300 border-amber-500/30" },
  applied: { label: "Applied", cls: "bg-sky-500/15 text-sky-300 border-sky-500/30" },
  interviewing: { label: "Interviewing", cls: "bg-violet-500/15 text-violet-300 border-violet-500/30" },
  offer: { label: "Offer", cls: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30" },
  rejected: { label: "Rejected", cls: "bg-red-500/15 text-red-300 border-red-500/30" },
  not_interested: { label: "Not for me", cls: "bg-zinc-500/15 text-zinc-400 border-zinc-500/30" },
};

/** A counter that changes whenever the stored token does, for effects that load private data. */
export function useAuthKey(): number {
  const [key, setKey] = useState(0);
  useEffect(() => {
    const bump = () => setKey((k) => k + 1);
    window.addEventListener(AUTH_EVENT, bump);
    window.addEventListener("storage", bump);
    return () => {
      window.removeEventListener(AUTH_EVENT, bump);
      window.removeEventListener("storage", bump);
    };
  }, []);
  return key;
}

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

/**
 * The owner's fallback login on /login, for a deployment without Telegram login: the ADMIN_TOKEN, kept in
 * this browser's localStorage and sent as X-Admin-Token. Never shown in the header.
 */
export function AdminTokenLogin({ onLogin }: { onLogin: (me: Me) => void }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <form
      className="space-y-2"
      onSubmit={async (e) => {
        e.preventDefault();
        if (!value.trim()) return;
        setBusy(true);
        setError(null);
        try {
          onLogin(await loginWithAdminToken(value));
        } catch (err) {
          setError(err instanceof Error ? err.message : "Login failed");
          setBusy(false);
        }
      }}
    >
      <div className="flex items-center gap-2">
        <KeyRound className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <input
          type="password"
          autoComplete="off"
          placeholder="Admin token"
          aria-label="Admin token"
          className="min-w-0 flex-1 rounded-md border border-border/50 bg-secondary/50 px-2 py-1.5 text-sm placeholder:text-muted-foreground/60 focus:border-primary focus:outline-none"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        <button
          type="submit"
          disabled={busy || !value.trim()}
          className="rounded-md border border-border/50 px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground disabled:opacity-50"
        >
          {busy ? "Checking…" : "Use"}
        </button>
      </div>
      {error && (
        <p role="alert" className="text-xs text-red-400">
          {error}
        </p>
      )}
    </form>
  );
}

const pillCls = "inline-flex min-w-0 items-center gap-1 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1 text-emerald-300";
const ghostCls =
  "inline-flex items-center gap-1 rounded-md border border-border/50 px-2 py-1 text-muted-foreground hover:text-foreground hover:border-primary/30 transition-colors";

/**
 * Who is logged in, resolved once per login change: undefined while checking, null when nobody is (the
 * dashboard then goes to /login). Turns a /dashboard link's #token=... into a session first.
 */
export function useViewer(): Me | null | undefined {
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const authKey = useAuthKey();
  useEffect(() => {
    let cancelled = false;
    resolveViewer().then(
      (m) => !cancelled && setMe(m),
      () => !cancelled && setMe(null),
    );
    return () => {
      cancelled = true;
    };
  }, [authKey]);
  return me;
}

/** Header control for a logged-in viewer: their name (and "owner") and "Log out". */
export function ViewerControl({ me }: { me: Me }) {
  const [busy, setBusy] = useState(false);
  return (
    <div className="flex min-w-0 flex-wrap items-center justify-end gap-2 text-xs">
      <span className={pillCls} data-testid="viewer-name">
        <UserRound className="h-3 w-3 shrink-0" aria-hidden="true" />
        <span className="truncate">{me.display_name}</span>
        {me.role === "owner" && (
          <span className="text-emerald-300/70">
            {me.display_name.toLowerCase() === "owner" ? "" : "· owner"}
            {me.via === "admin" ? " (admin token)" : ""}
          </span>
        )}
      </span>
      <button
        type="button"
        className={ghostCls}
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          await logout(); // the dashboard notices (AUTH_EVENT) and goes to /login
          setBusy(false);
        }}
      >
        <LogOut className="h-3 w-3" aria-hidden="true" />
        Log out
      </button>
    </div>
  );
}

/** Full-page placeholder while the login is checked: nothing of the dashboard is rendered before that. */
export function AuthPending({ label }: { label: string }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background" role="status" aria-live="polite">
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin text-primary" aria-hidden="true" />
        {label}
      </div>
    </div>
  );
}
