"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  APPLICATION_STATUSES,
  AUTH_EVENT,
  captureTokenFromUrl,
  convertTokenToSession,
  fetchMe,
  getAdminToken,
  logout,
  setAdminToken,
  setUserToken,
  type ApplicationStatus,
  type Me,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { KeyRound, Lock, LogIn, LogOut, UserRound } from "lucide-react";

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
        placeholder="Admin token (private view)"
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

const pillCls = "inline-flex items-center gap-1 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1 text-emerald-300";
const ghostCls =
  "inline-flex items-center gap-1 rounded-md border border-border/50 px-2 py-1 text-muted-foreground hover:text-foreground hover:border-primary/30 transition-colors";

/** "Log in" in the header: the Telegram login page. */
export function LoginButton() {
  return (
    <Link
      href="/login"
      className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-semibold text-primary-foreground shadow-md hover:opacity-90"
    >
      <LogIn className="h-3.5 w-3.5" aria-hidden="true" />
      Log in
    </Link>
  );
}

/**
 * Header control. Logged in (Telegram session): the name and "Log out". With a /dashboard link token:
 * "Private view", an offer to stay logged in (turns the link into a session) and "Public view".
 * Public: "Log in" and the admin-token field. Also picks up a personal link's #token=... on first load.
 */
export function ViewerControl() {
  const [me, setMe] = useState<Me | null>(null);
  const [busy, setBusy] = useState(false);
  const [convertError, setConvertError] = useState<string | null>(null);
  const authKey = useAuthKey();
  useEffect(() => {
    captureTokenFromUrl();
  }, []);
  useEffect(() => {
    fetchMe().then(setMe, () => setMe(null));
  }, [authKey]);
  if (!me)
    return (
      <div className="flex items-center gap-3">
        <AdminTokenField />
        <LoginButton />
      </div>
    );
  if (me.via === "session")
    return (
      <div className="flex items-center gap-2 text-xs">
        <span className={pillCls} data-testid="viewer-name">
          <UserRound className="h-3 w-3" aria-hidden="true" />
          {me.display_name}
          {me.role === "owner" && <span className="text-emerald-300/70">· owner</span>}
        </span>
        <button
          type="button"
          className={ghostCls}
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            await logout();
            setBusy(false);
          }}
        >
          <LogOut className="h-3 w-3" aria-hidden="true" />
          Log out
        </button>
      </div>
    );
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className={pillCls}>
        <Lock className="h-3 w-3" aria-hidden="true" />
        Private view: {me.display_name}
      </span>
      {me.via === "token" && (
        <button
          type="button"
          className={ghostCls}
          disabled={busy}
          title="Start a login session from this link, so the link isn't kept in this browser"
          onClick={async () => {
            setBusy(true);
            setConvertError(null);
            try {
              await convertTokenToSession();
            } catch (e) {
              setConvertError(e instanceof Error ? e.message : "Failed");
            } finally {
              setBusy(false);
            }
          }}
        >
          <LogIn className="h-3 w-3" aria-hidden="true" />
          Stay logged in
        </button>
      )}
      {convertError && <span className="text-red-400">{convertError}</span>}
      <button
        type="button"
        className={ghostCls}
        onClick={() => {
          setAdminToken("");
          setUserToken("");
        }}
      >
        <LogOut className="h-3 w-3" aria-hidden="true" />
        Public view
      </button>
    </div>
  );
}
