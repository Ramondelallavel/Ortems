"use client";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, setUnauthorizedHandler } from "./api";
import { setFormatContext } from "./format";
import { translate, type Locale } from "./i18n";

export type Plant = { id: string; code: string; name: string; timezone: string; live_scenario_id: string | null; published_plan_id: string | null };
export type Me = {
  id: string;
  username: string;
  full_name: string;
  email: string | null;
  locale: Locale;
  tenant_id: string;
  roles: string[];
  permissions: string[];
  plants: Plant[];
  default_plant_id: string | null;
};

type SessionValue = {
  me: Me | null;
  loading: boolean;
  plant: Plant | null;
  setPlantId: (id: string) => void;
  locale: Locale;
  setLocale: (l: Locale) => void;
  t: (key: string, vars?: Record<string, string | number>) => string;
  can: (perm: string) => boolean;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
};

const Ctx = createContext<SessionValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const [plantId, setPlantIdState] = useState<string | null>(null);
  const [locale, setLocaleState] = useState<Locale>("en");

  const refresh = useCallback(async () => {
    try {
      const m = await api<Me>("/auth/me");
      setMe(m);
      let stored: string | null = null;
      let storedLocale: string | null = null;
      try {
        stored = localStorage.getItem("mx.plant");
        storedLocale = localStorage.getItem("mx.locale");
      } catch {
        /* storage unavailable */
      }
      const pid = m.plants.find((p) => p.id === stored)?.id || m.default_plant_id || m.plants[0]?.id || null;
      setPlantIdState(pid);
      setLocaleState(((storedLocale as Locale) || m.locale || "en") as Locale);
    } catch {
      setMe(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
      }
    });
    if (window.location.pathname.startsWith("/login")) setLoading(false);
    else refresh();
  }, [refresh]);

  const plant = useMemo(() => me?.plants.find((p) => p.id === plantId) || null, [me, plantId]);
  useEffect(() => {
    setFormatContext(locale, plant?.timezone || "UTC");
    if (typeof document !== "undefined") document.documentElement.lang = locale;
  }, [locale, plant]);
  setFormatContext(locale, plant?.timezone || "UTC");

  const value: SessionValue = {
    me,
    loading,
    plant,
    setPlantId: (id) => {
      setPlantIdState(id);
      try {
        localStorage.setItem("mx.plant", id);
      } catch {
        /* ignore */
      }
    },
    locale,
    setLocale: (l) => {
      setLocaleState(l);
      try {
        localStorage.setItem("mx.locale", l);
      } catch {
        /* ignore */
      }
    },
    t: (key, vars) => translate(locale, key, vars),
    can: (perm) => !!me?.permissions.includes(perm),
    refresh,
    logout: async () => {
      try {
        await api("/auth/logout", { method: "POST" });
      } finally {
        window.location.href = "/login";
      }
    },
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSession(): SessionValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useSession outside SessionProvider");
  return v;
}
