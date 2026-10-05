// Lists every t("…") / t(`…`) literal of the interface that has no Spanish translation (neither a
// dictionary key nor an entry of the phrase dictionary), and phrases translated but no longer used.
// Usage: node scripts/i18n-check.mjs   (exit code 1 when something is missing)
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SRC = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "src");
const read = (f) => fs.readFileSync(path.join(SRC, f), "utf8");
const keysOf = (src) => new Set([...src.matchAll(/^\s*"((?:[^"\\]|\\.)+)":/gm)].map((m) => JSON.parse(`"${m[1]}"`)));
const dict = keysOf(read("lib/i18n/es.ts"));
const phrases = keysOf(read("lib/i18n/es-phrases.ts"));

const used = new Map();
let allSource = "";
const walk = (d) => {
  for (const e of fs.readdirSync(path.join(SRC, d), { withFileTypes: true })) {
    const rel = path.join(d, e.name);
    if (e.isDirectory()) walk(rel);
    else if (/\.tsx?$/.test(e.name) && (!rel.startsWith(path.join("lib", "i18n")) || rel.endsWith("server-terms.ts"))) {
      const src = read(rel);
      allSource += src;
      for (const m of src.matchAll(/\bt\(\s*"((?:[^"\\]|\\.)*)"/g)) used.set(JSON.parse(`"${m[1]}"`), rel);
    }
  }
};
walk(".");
const missing = [...used].filter(([k]) => !dict.has(k) && !phrases.has(k));
// a phrase passed through a variable (t(z.label)) counts as used when its literal is in the source
const unused = [...phrases].filter((k) => !used.has(k) && !allSource.includes(JSON.stringify(k)));
for (const [k, f] of missing) console.log(`missing  ${f}: ${JSON.stringify(k)}`);
for (const k of unused) console.log(`unused   ${JSON.stringify(k)}`);
console.log(`${used.size} literals · ${missing.length} without Spanish · ${unused.length} unused phrases`);
process.exit(missing.length ? 1 : 0);
