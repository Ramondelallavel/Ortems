// Formatting in the user's locale and the plant's time zone.

let _locale = "en";
let _tz = "UTC";
export function setFormatContext(locale: string, tz: string) {
  _locale = locale === "es" ? "es-ES" : locale === "fr" ? "fr-FR" : locale === "de" ? "de-DE" : locale === "pt" ? "pt-PT" : "en-GB";
  _tz = tz || "UTC";
}
export const tz = () => _tz;

export function num(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return new Intl.NumberFormat(_locale, { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(v);
}
export function pct(v: number | null | undefined, digits = 1, fraction = false): string {
  if (v === null || v === undefined) return "—";
  return `${num(fraction ? v * 100 : v, digits)} %`;
}
export function hours(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined) return "—";
  return `${num(minutes / 60, 1)} h`;
}
export function duration(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined) return "—";
  const m = Math.round(Math.abs(minutes));
  const d = Math.floor(m / 1440);
  const h = Math.floor((m % 1440) / 60);
  const mi = m % 60;
  const s = [d ? `${d} d` : "", h ? `${h} h` : "", mi && !d ? `${mi} min` : ""].filter(Boolean).join(" ");
  return (minutes < 0 ? "−" : "") + (s || "0 min");
}
function toDate(v: string | number | Date): Date {
  return v instanceof Date ? v : new Date(v);
}
export function dt(v: string | Date | null | undefined, opts: Intl.DateTimeFormatOptions = { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }): string {
  if (!v) return "—";
  return new Intl.DateTimeFormat(_locale, { ...opts, timeZone: _tz }).format(toDate(v));
}
export function date(v: string | Date | null | undefined): string {
  return dt(v, { day: "2-digit", month: "short", year: "numeric" });
}
export function time(v: string | Date | null | undefined): string {
  return dt(v, { hour: "2-digit", minute: "2-digit" });
}
export function weekday(v: string | Date): string {
  return dt(v, { weekday: "short", day: "2-digit", month: "short" });
}
/** Plant-local wall-clock parts of an instant (used by the Gantt for day/shift grid). */
export function localParts(ms: number): { y: number; mo: number; d: number; h: number; mi: number; wd: number } {
  const f = new Intl.DateTimeFormat("en-US", { timeZone: _tz, year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric", weekday: "short", hourCycle: "h23" });
  const p: Record<string, string> = {};
  for (const x of f.formatToParts(new Date(ms))) p[x.type] = x.value;
  const wd = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].indexOf(p.weekday);
  return { y: +p.year, mo: +p.month, d: +p.day, h: +p.hour, mi: +p.minute, wd };
}
/** ISO string for a <input type="datetime-local"> in plant time → UTC instant. */
export function localInputToIso(value: string): string {
  // value "YYYY-MM-DDTHH:MM" in plant tz: find the UTC instant whose plant-local time matches
  const [d, t] = value.split("T");
  const [y, mo, da] = d.split("-").map(Number);
  const [h, mi] = t.split(":").map(Number);
  let guess = Date.UTC(y, mo - 1, da, h, mi);
  for (let k = 0; k < 3; k++) {
    const p = localParts(guess);
    const diff = Date.UTC(p.y, p.mo - 1, p.d, p.h, p.mi) - Date.UTC(y, mo - 1, da, h, mi);
    if (diff === 0) break;
    guess -= diff;
  }
  return new Date(guess).toISOString();
}
export function isoToLocalInput(iso: string): string {
  const p = localParts(new Date(iso).getTime());
  const z = (n: number) => String(n).padStart(2, "0");
  return `${p.y}-${z(p.mo)}-${z(p.d)}T${z(p.h)}:${z(p.mi)}`;
}
