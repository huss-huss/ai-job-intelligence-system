"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { loginWithLink, loginWithTelegram, logout, resolveViewer, type Me, type TelegramUser } from "@/lib/api";
import { AdminTokenLogin } from "@/components/Tracker";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ArrowLeft, Briefcase, Loader2, LogOut, Send, ShieldCheck } from "lucide-react";

// Set on the dashboard's Vercel project; the bot's domain must be set with BotFather's /setdomain.
const BOT_USERNAME = (process.env.NEXT_PUBLIC_TELEGRAM_BOT_USERNAME ?? "").replace(/^@/, "");
const WIDGET_SRC = "https://telegram.org/js/telegram-widget.js?22";
// The bot answers /start login with a one-time button back to /login#link=<token>.
const BOT_LOGIN_URL = BOT_USERNAME ? `https://t.me/${BOT_USERNAME}?start=login` : "";

/** The one-time token from the bot's button, removed from the address bar so it isn't kept in history. */
function takeLinkToken(): string | null {
  const match = window.location.hash.match(/^#link=([A-Za-z0-9_-]{20,128})$/);
  if (!match) return null;
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
  return match[1];
}

declare global {
  interface Window {
    onTelegramAuth?: (user: TelegramUser) => void;
  }
}

/** The official Telegram Login Widget, in callback mode (data-onauth). */
function TelegramLoginWidget({ onAuth }: { onAuth: (user: TelegramUser) => void }) {
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = container.current;
    if (!el || !BOT_USERNAME) return;
    window.onTelegramAuth = onAuth;
    const script = document.createElement("script");
    script.async = true;
    script.src = WIDGET_SRC;
    script.setAttribute("data-telegram-login", BOT_USERNAME);
    script.setAttribute("data-size", "large");
    script.setAttribute("data-radius", "8");
    script.setAttribute("data-onauth", "onTelegramAuth(user)");
    el.replaceChildren(script);
    return () => {
      el.replaceChildren();
      delete window.onTelegramAuth;
    };
  }, [onAuth]);
  return <div ref={container} className="flex min-h-[48px] items-center justify-center" data-testid="telegram-widget" />;
}

export default function LoginPage() {
  const router = useRouter();
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const token = takeLinkToken();
    if (!token) {
      resolveViewer().then(setMe, () => setMe(null));
      return;
    }
    setBusy(true);
    loginWithLink(token).then(
      () => router.replace("/"),
      (e) => {
        setError(e instanceof Error ? e.message : "Login failed");
        setBusy(false);
        resolveViewer().then(setMe, () => setMe(null));
      },
    );
  }, [router]);

  const onAuth = useRef(async (user: TelegramUser) => {
    setBusy(true);
    setError(null);
    try {
      await loginWithTelegram(user);
      router.replace("/");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Login failed");
      setBusy(false);
    }
  }).current;

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b border-border/50 bg-gradient-to-r from-card via-card/95 to-card/90 shadow-lg shadow-primary/5">
        <div className="container mx-auto flex min-h-16 flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-3">
          <div className="flex min-w-0 items-center gap-3">
            <div className="p-2 rounded-lg bg-primary/10 border border-primary/20">
              <Briefcase className="h-5 w-5 text-primary" />
            </div>
            <div>
              <h1 className="text-lg font-bold sm:text-xl">AI Job Intelligence</h1>
              <p className="text-xs text-muted-foreground">Smart Job Discovery Platform</p>
            </div>
          </div>
          {me && (
            <Link href="/" className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
              <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
              Back to jobs
            </Link>
          )}
        </div>
      </header>

      <main className="container mx-auto flex justify-center px-4 py-10 sm:py-16">
        <Card className="w-full max-w-md border-border/50 shadow-lg shadow-primary/5 animate-fade-in">
          <CardHeader className="items-center text-center">
            <div className="mb-2 rounded-full border border-primary/20 bg-primary/10 p-3">
              <ShieldCheck className="h-6 w-6 text-primary" aria-hidden="true" />
            </div>
            <CardTitle className="text-2xl">Log in</CardTitle>
            <p className="text-sm text-muted-foreground">
              This dashboard is private. Log in to see jobs ranked for your own profile, with your statuses and
              talent-network progress.
            </p>
          </CardHeader>
          <CardContent className="space-y-5">
            {me === undefined ? (
              <div className="flex items-center justify-center gap-2 py-6 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin text-primary" />
                Checking your login…
              </div>
            ) : me ? (
              <div className="space-y-4 text-center">
                <p className="text-sm">
                  You&apos;re logged in as <span className="font-semibold text-emerald-300">{me.display_name}</span>.
                </p>
                <div className="flex justify-center gap-2">
                  <Link href="/" className="rounded-md bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:opacity-90">
                    Open your dashboard
                  </Link>
                  <button
                    type="button"
                    className="inline-flex items-center gap-1 rounded-md border border-border/50 px-3 py-2 text-sm text-muted-foreground hover:text-foreground"
                    onClick={async () => {
                      await logout();
                      setMe(null);
                    }}
                  >
                    <LogOut className="h-4 w-4" aria-hidden="true" />
                    Log out
                  </button>
                </div>
              </div>
            ) : BOT_USERNAME ? (
              <>
                {busy ? (
                  <div className="flex items-center justify-center gap-2 rounded-lg border border-border/50 bg-secondary/30 px-4 py-6 text-sm text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin text-primary" />
                    Signing you in…
                  </div>
                ) : (
                  <div className="space-y-4">
                    <a
                      href={BOT_LOGIN_URL}
                      className="flex w-full items-center justify-center gap-2 rounded-lg bg-[#2AABEE] px-4 py-3 text-sm font-semibold text-white shadow-md hover:opacity-90"
                      data-testid="telegram-open"
                    >
                      <Send className="h-4 w-4" aria-hidden="true" />
                      Open Telegram to log in
                    </a>
                    <p className="text-center text-xs text-muted-foreground">
                      Press <span className="font-semibold">Start</span> in the bot (or send /login), then tap the
                      button it sends you.
                    </p>
                    <details className="rounded-lg border border-border/50 bg-secondary/20 px-3 py-2 text-sm">
                      <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground">
                        Or log in with your phone number (Telegram widget)
                      </summary>
                      <div className="mt-3">
                        <TelegramLoginWidget onAuth={onAuth} />
                      </div>
                    </details>
                  </div>
                )}
                {error && (
                  <div role="alert" className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-red-400">
                    {error}
                  </div>
                )}
              </>
            ) : (
              <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-300">
                Web login isn&apos;t configured on this deployment (NEXT_PUBLIC_TELEGRAM_BOT_USERNAME).
              </div>
            )}
            {me === null && (
              <details className="rounded-lg border border-border/50 bg-secondary/20 px-3 py-2 text-sm">
                <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground">Owner: use the admin token instead</summary>
                <div className="mt-3">
                  <AdminTokenLogin onLogin={() => router.replace("/")} />
                  <p className="mt-2 text-xs text-muted-foreground">Kept in this browser only. Log out forgets it.</p>
                </div>
              </details>
            )}
            <ul className="space-y-1.5 text-xs text-muted-foreground">
              <li>• Only people the owner invited to the Telegram bot can log in.</li>
              <li>• Telegram shares your name and user id with this site; nothing is posted for you.</li>
              <li>• Already have a link from the bot&apos;s /dashboard? It still works.</li>
            </ul>
          </CardContent>
        </Card>
      </main>
    </div>
  );
}
