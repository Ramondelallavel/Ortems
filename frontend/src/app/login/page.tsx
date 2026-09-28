"use client";
import { useState } from "react";
import { Logo } from "@/components/shell/Logo";
import { Button, Icon } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { LOCALES, translate, type Locale } from "@/lib/i18n";
import { loc } from "@/lib/loc";

export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [tenant, setTenant] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [locale, setLocale] = useState<Locale>(() => {
    if (typeof window === "undefined") return "en";
    try {
      return (localStorage.getItem("mx.locale") as Locale) || (navigator.language.startsWith("es") ? "es" : "en");
    } catch {
      return "en";
    }
  });
  const t = (k: string) => translate(locale, k);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/auth/login", { body: { username, password, tenant: tenant || null } });
      const next = new URLSearchParams(loc.search()).get("next");
      loc.go(next && next.startsWith("/") && !next.startsWith("//") ? next : "/dashboard");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      setBusy(false);
    }
  };

  return (
    <main className="min-h-screen grid md:grid-cols-[1fr_460px] bg-navy-950">
      <section className="hidden md:flex flex-col justify-between p-10 text-white/90">
        <div className="flex items-center gap-2 text-[18px] font-semibold tracking-tight">
          <Logo /> MonxuPlan
        </div>
        <div className="max-w-lg">
          <p className="text-[26px] leading-tight font-semibold text-white">Finite-capacity planning you can explain.</p>
          <p className="mt-3 text-white/70 text-[14px]">Machines, people, tools and materials in one feasible schedule — with the reason behind every position, and scenarios before every decision.</p>
        </div>
        <div className="text-[11px] text-white/50">© MonxuPlan</div>
      </section>
      <section className="bg-white flex flex-col justify-center px-8 py-10">
        <form onSubmit={submit} className="w-full max-w-[340px] mx-auto flex flex-col gap-3" aria-describedby={error ? "login-error" : undefined}>
          <div className="md:hidden flex items-center gap-2 font-semibold text-navy-900 mb-2">
            <Logo dark /> MonxuPlan
          </div>
          <h1 className="text-[18px] font-semibold">{t("login.title")}</h1>
          <div>
            <label className="mx-label" htmlFor="u">
              {t("login.username")}
            </label>
            <input id="u" className="mx-input w-full" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required autoFocus />
          </div>
          <div>
            <label className="mx-label" htmlFor="p">
              {t("login.password")}
            </label>
            <input id="p" type="password" className="mx-input w-full" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </div>
          <div>
            <label className="mx-label" htmlFor="tn">
              {t("login.tenant")}
            </label>
            <input id="tn" className="mx-input w-full" value={tenant} onChange={(e) => setTenant(e.target.value)} placeholder="monxu" />
          </div>
          {error && (
            <div id="login-error" role="alert" className="text-red-600 text-[12.5px] flex gap-1.5 items-start">
              <Icon name="alert" size={14} /> {error}
            </div>
          )}
          <Button type="submit" variant="primary" busy={busy} className="justify-center h-9">
            {t("login.submit")}
          </Button>
          <p className="text-[11.5px] text-slate-600 border-t border-gray-200 pt-3">{t("login.demo")}</p>
          <div className="flex items-center gap-2">
            <label className="mx-label !mb-0" htmlFor="lang">
              {t("common.language")}
            </label>
            <select
              id="lang"
              className="mx-select"
              value={locale}
              onChange={(e) => {
                setLocale(e.target.value as Locale);
                try {
                  localStorage.setItem("mx.locale", e.target.value);
                } catch {
                  /* ignore */
                }
              }}
            >
              {LOCALES.map((l) => (
                <option key={l.code} value={l.code}>
                  {l.label}
                </option>
              ))}
            </select>
          </div>
        </form>
      </section>
    </main>
  );
}
