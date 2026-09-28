import en, { type Dict } from "./en";
import es from "./es";

export type Locale = "en" | "es" | "fr" | "de" | "pt";
export const LOCALES: { code: Locale; label: string; complete: boolean }[] = [
  { code: "en", label: "English", complete: true },
  { code: "es", label: "Español", complete: true },
  { code: "fr", label: "Français", complete: false },
  { code: "de", label: "Deutsch", complete: false },
  { code: "pt", label: "Português", complete: false },
];

// French, German and Portuguese: navigation translated; everything else falls back to English.
const partial: Record<string, Partial<Dict>> = {
  fr: { "nav.commandCenter": "Centre de pilotage", "nav.board": "Tableau de planification", "nav.orders": "Ordres", "nav.capacity": "Capacité", "nav.materials": "Matières", "nav.scenarios": "Scénarios", "nav.analytics": "Analyses", "nav.masterData": "Données de base", "nav.admin": "Administration", "common.signOut": "Se déconnecter" },
  de: { "nav.commandCenter": "Leitstand", "nav.board": "Planungstafel", "nav.orders": "Aufträge", "nav.capacity": "Kapazität", "nav.materials": "Material", "nav.scenarios": "Szenarien", "nav.analytics": "Analysen", "nav.masterData": "Stammdaten", "nav.admin": "Administration", "common.signOut": "Abmelden" },
  pt: { "nav.commandCenter": "Centro de comando", "nav.board": "Quadro de planeamento", "nav.orders": "Ordens", "nav.capacity": "Capacidade", "nav.materials": "Materiais", "nav.scenarios": "Cenários", "nav.analytics": "Análises", "nav.masterData": "Dados mestre", "nav.admin": "Administração", "common.signOut": "Terminar sessão" },
};
const dicts: Record<string, Partial<Dict>> = { en, es, ...partial };

export type TKey = keyof Dict;
export function translate(locale: string, key: string, vars?: Record<string, string | number>): string {
  const d = dicts[locale] || en;
  let s = (d as Record<string, string>)[key] ?? (en as Record<string, string>)[key] ?? key;
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.replace(`{${k}}`, String(v));
  return s;
}
