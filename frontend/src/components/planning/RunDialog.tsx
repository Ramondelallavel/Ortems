"use client";
import { useEffect, useState } from "react";
import { Button, Dialog, Field, Select, useConfirm, useToast } from "@/components/ui";
import { useApi } from "@/lib/hooks";
import { startRun } from "@/lib/runs";
import { useSession } from "@/lib/session";

/** Optimise / plan dialog: objective preset, solver provider and time profile. Nothing hidden:
 * the chosen settings are sent as-is and stored with the plan version. */
export function RunDialog({ open, onClose, scenarioId, onStarted }: { open: boolean; onClose: () => void; scenarioId: string; onStarted: (runId: string) => void }) {
  const presets = useApi<any>(open ? "/planning/presets" : null);
  const providers = useApi<any[]>(open ? "/planning/providers" : null);
  const toast = useToast();
  const { t } = useSession();
  const { confirm, node: confirmNode } = useConfirm();
  const [mode, setMode] = useState("OPTIMIZE");
  const [preset, setPreset] = useState("");
  const [provider, setProvider] = useState("hybrid");
  const [profile, setProfile] = useState("QUICK");
  const [custom, setCustom] = useState("");
  const [reproducible, setReproducible] = useState(true);
  const [overtime, setOvertime] = useState(false);
  const [materials, setMaterials] = useState("HARD");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (open) setBusy(false);
  }, [open]);

  const limits = presets.data?.time_limits || { QUICK: 10, NORMAL: 60, DEEP: 600 };
  const start = async () => {
    setBusy(true);
    try {
      const solver: any = { provider, profile, reproducible };
      if (custom) solver.time_limit_s = Number(custom);
      const body: any = { scenario_id: scenarioId, mode, solver, constraints: { allow_overtime: overtime, materials }, note: note || undefined };
      if (preset) body.objectives = { preset };
      const runId = await startRun(body, confirm, t);
      if (!runId) {
        setBusy(false);
        return;
      }
      onStarted(runId);
      onClose();
    } catch (e) {
      toast.error(e);
      setBusy(false);
    }
  };
  const detailed = (providers.data || []).filter((p) => p.detailed_scheduling);
  // only offer providers installed in this runtime (the browser edition has the heuristic only)
  useEffect(() => {
    if (detailed.length && !detailed.some((p) => p.name === provider)) setProvider(detailed.some((p) => p.name === "hybrid") ? "hybrid" : detailed[0].name);
  }, [detailed, provider]);
  return (
    <>
    <Dialog
      open={open}
      onClose={onClose}
      title={t("Plan / optimise scenario")}
      width={560}
      footer={
        <>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button variant="primary" icon="play" busy={busy} onClick={start}>
            {t("common.start")}
          </Button>
        </>
      }
    >
      <div className="grid grid-cols-2 gap-3">
        <Field label={t("Run type")} hint={mode === "PLAN" ? t("Fast constructive schedule, no optimisation") : t("Build and improve the schedule")}>
          <Select value={mode} onChange={setMode} options={[{ value: "OPTIMIZE", label: t("Optimise") }, { value: "PLAN", label: t("Plan (no optimisation)") }]} className="w-full" />
        </Field>
        <Field label={t("Objective")} hint={t("Empty = the scenario's optimisation profile")}>
          <Select
            value={preset}
            onChange={setPreset}
            className="w-full"
            options={[{ value: "", label: t("Scenario profile") }, ...Object.entries(presets.data?.presets || {}).map(([k, v]: any) => ({ value: k, label: t(v.label) }))]}
          />
        </Field>
        <Field label={t("Solver")}>
          <Select value={provider} onChange={setProvider} className="w-full" options={detailed.length ? detailed.map((p) => ({ value: p.name, label: `${p.name}${p.proves_optimality ? ` (${t("can prove optimality")})` : ""}` })) : [{ value: "hybrid", label: "hybrid" }]} />
        </Field>
        <Field label={t("Time budget")} hint={custom ? t("{s} s (custom)", { s: custom }) : `${limits[profile]} s`}>
          <div className="flex gap-2">
            <Select value={profile} onChange={setProfile} className="flex-1" options={Object.keys(limits).map((k) => ({ value: k, label: `${t(k.charAt(0) + k.slice(1).toLowerCase())} · ${limits[k]} s` }))} />
            <input className="mx-input w-[80px]" type="number" min={1} max={3600} placeholder="s" aria-label={t("Custom time limit in seconds")} value={custom} onChange={(e) => setCustom(e.target.value)} />
          </div>
        </Field>
        <Field label={t("nav.materials")} hint={t("A hard constraint never consumes stock that does not exist")}>
          <Select value={materials} onChange={setMaterials} className="w-full" options={[{ value: "HARD", label: t("Hard constraint") }, { value: "ALLOW_SHORTAGE", label: t("Allow shortage (flagged)") }, { value: "IGNORE", label: t("Ignore materials") }]} />
        </Field>
        <div className="flex flex-col gap-2 pt-5">
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={overtime} onChange={(e) => setOvertime(e.target.checked)} /> {t("Allow overtime windows")}
          </label>
          <label className="flex items-center gap-2" title={t("Same data + same settings → same plan")}>
            <input type="checkbox" checked={reproducible} onChange={(e) => setReproducible(e.target.checked)} /> {t("Reproducible (deterministic)")}
          </label>
        </div>
        <div className="col-span-2">
          <Field label={t("Note")}>
            <input className="mx-input w-full" value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("e.g. after CNC-03 repair")} />
          </Field>
        </div>
      </div>
    </Dialog>
    {confirmNode}
    </>
  );
}
