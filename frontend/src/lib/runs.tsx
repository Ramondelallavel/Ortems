"use client";
import Link from "next/link";
import type { ReactNode } from "react";
import { api, ApiError } from "./api";

type Confirm = (title: string, opts?: { body?: ReactNode; danger?: boolean; reason?: boolean }) => Promise<{ ok: boolean; reason?: string }>;
type Issue = { code: string; title: string; count: number };

/** Queue a planning run. Critical data problems block planning on the server; planning anyway is an
 * explicit decision on the server's own list of problems, with a reason that is recorded on the run
 * and in the audit log. Returns the run id, or null when the user declined. */
export async function startRun(body: Record<string, unknown>, confirm: Confirm, t: (k: string, v?: Record<string, string | number>) => string): Promise<string | null> {
  try {
    const r = await api<{ run_id: string }>("/planning/run", { body });
    return r.run_id;
  } catch (e) {
    if (!(e instanceof ApiError) || e.code !== "DATA_QUALITY_BLOCK") throw e;
    const issues = ((e.context as { issues?: Issue[] }).issues || []) as Issue[];
    const o = await confirm(t("run.blockedTitle"), {
      danger: true,
      reason: true,
      body: (
        <div className="space-y-1 text-[12.5px]">
          <p>{t("run.blockedBody")}</p>
          <ul className="space-y-0.5">
            {issues.map((i) => (
              <li key={i.code} className="text-red-600">
                ▲ {t(i.title)} ({i.count})
              </li>
            ))}
          </ul>
          <p>
            <Link className="text-blue-500 hover:underline" href="/admin/data-quality">
              {t("nav.dataQuality")}
            </Link>
          </p>
          <p className="text-slate-600">{t("run.blockedOverride")}</p>
        </div>
      ),
    });
    if (!o.ok) return null;
    const r = await api<{ run_id: string }>("/planning/run", { body: { ...body, force: true, force_reason: o.reason } });
    return r.run_id;
  }
}
