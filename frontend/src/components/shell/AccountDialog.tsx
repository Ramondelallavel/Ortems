"use client";
import { useEffect, useState } from "react";
import { Badge, Button, Dialog, Field, Select, useConfirm, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { LOCALES, type Locale } from "@/lib/i18n";
import { loc } from "@/lib/loc";
import { useSession } from "@/lib/session";

/** The signed-in user's own account: language and default plant (stored on the account), password,
 * and ending every session on every device. */
export function AccountDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { me, t, locale, setLocale, refresh } = useSession();
  const toast = useToast();
  const { confirm, node } = useConfirm();
  const [lang, setLang] = useState<string>(locale);
  const [plantId, setPlantId] = useState<string>("");
  const [cur, setCur] = useState("");
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  useEffect(() => {
    if (open) {
      setLang(locale);
      setPlantId(me?.default_plant_id || "");
      setCur("");
      setPw("");
      setPw2("");
    }
  }, [open, locale, me?.default_plant_id]);
  if (!me) return null;

  const savePrefs = async () => {
    setBusy("prefs");
    try {
      await api("/auth/me", { method: "PATCH", body: { locale: lang, default_plant_id: plantId || undefined } });
      setLocale(lang as Locale);
      await refresh();
      toast.ok(t("acct.saved"));
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(null);
    }
  };
  const mismatch = !!pw2 && pw !== pw2;
  const changePassword = async () => {
    setBusy("pw");
    try {
      await api("/auth/password", { body: { current_password: cur, new_password: pw } });
      setCur("");
      setPw("");
      setPw2("");
      toast.ok(t("acct.pwChanged"));
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(null);
    }
  };
  const everywhere = async () => {
    const r = await confirm(t("acct.everywhereQ"), { danger: true, body: t("acct.everywhereBody") });
    if (!r.ok) return;
    try {
      await api("/auth/logout-everywhere", { method: "POST" });
      loc.go("/login");
    } catch (e) {
      toast.error(e);
    }
  };

  return (
    <>
      <Dialog open={open} onClose={onClose} title={t("acct.title")} width={560} footer={<Button onClick={onClose}>{t("common.close")}</Button>}>
        <div className="space-y-4 text-[12.5px]">
          <section>
            <div className="font-semibold">{me.full_name}</div>
            <div className="text-slate-600">
              {me.username}
              {me.email ? ` · ${me.email}` : ""}
            </div>
            <div className="flex flex-wrap gap-1 mt-1">
              {me.roles.map((r) => (
                <Badge key={r} tone="neutral" glyph={false}>
                  {r}
                </Badge>
              ))}
            </div>
          </section>
          <section className="space-y-2">
            <h3 className="mx-label">{t("acct.prefs")}</h3>
            <div className="grid grid-cols-2 gap-3">
              <Field label={t("common.language")}>
                <Select value={lang} onChange={setLang} className="w-full" options={LOCALES.map((l) => ({ value: l.code, label: l.complete ? l.label : `${l.label} (${t("acct.partial")})` }))} />
              </Field>
              <Field label={t("acct.defaultPlant")}>
                <Select value={plantId} onChange={setPlantId} className="w-full" options={[{ value: "", label: "—" }, ...me.plants.map((p) => ({ value: p.id, label: `${p.code} · ${p.name}` }))]} />
              </Field>
            </div>
            <Button size="sm" variant="primary" busy={busy === "prefs"} onClick={savePrefs}>
              {t("common.save")}
            </Button>
          </section>
          <section className="space-y-2">
            <h3 className="mx-label">{t("acct.password")}</h3>
            <div className="grid grid-cols-3 gap-3">
              <Field label={t("acct.current")}>
                <input className="mx-input w-full" type="password" autoComplete="current-password" value={cur} onChange={(e) => setCur(e.target.value)} />
              </Field>
              <Field label={t("acct.new")} hint={t("acct.rules")}>
                <input className="mx-input w-full" type="password" autoComplete="new-password" value={pw} onChange={(e) => setPw(e.target.value)} />
              </Field>
              <Field label={t("acct.repeat")} error={mismatch ? t("acct.mismatch") : undefined}>
                <input className="mx-input w-full" type="password" autoComplete="new-password" value={pw2} onChange={(e) => setPw2(e.target.value)} />
              </Field>
            </div>
            <Button size="sm" busy={busy === "pw"} disabled={!cur || pw.length < 10 || pw !== pw2} onClick={changePassword}>
              {t("acct.change")}
            </Button>
            <p className="text-slate-600">{t("acct.pwNote")}</p>
          </section>
          <section className="space-y-1">
            <h3 className="mx-label">{t("acct.sessions")}</h3>
            <Button size="sm" variant="danger" onClick={everywhere}>
              {t("acct.everywhere")}
            </Button>
          </section>
        </div>
      </Dialog>
      {node}
    </>
  );
}
