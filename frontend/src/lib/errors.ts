import { ApiError } from "./api";

type T = (k: string, v?: Record<string, string | number>) => string;

/** The text of an error in the user's language: well-known error codes have a translation (filled
 * with the values the server sends in the error context); anything else shows the server's message. */
export function errorText(t: T, e: unknown): string {
  if (!(e instanceof Error)) return String(e);
  if (!(e instanceof ApiError)) return e.message;
  const c = e.context || {};
  const vars: Record<string, string | number> = {};
  if (typeof c.permission === "string") {
    const label = t(`perm.${c.permission}`);
    vars.permission = label === `perm.${c.permission}` ? c.permission : label;
  }
  if (typeof c.locked_by === "string") vars.who = c.locked_by;
  if (Array.isArray(c.problems)) vars.problems = c.problems.map((p) => t(String(p))).join(", ");
  const key = `err.${e.code}`;
  const tr = t(key, vars);
  // a translation that needs a value the server did not send would show a bare placeholder
  if (tr === key || /\{\w+\}/.test(tr)) return e.message;
  return tr;
}
