// MonxuPlan browser edition: the web app's pages, served by the in-memory router, talking to the real
// MonxuPlan API running in a Web Worker (Pyodide). No page or business logic is re-implemented here.
import { StrictMode, useEffect, useState, type ComponentType, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { apiFetch, installEventSource, installFetch, onBoot, startBackend } from "./bridge";
import { useRouterState } from "./router";
import { resolveRoute } from "./routes";
import { Providers } from "@/app/providers";
import AppLayout from "@/app/(app)/layout";
import LoginPage from "@/app/login/page";

installFetch();
installEventSource();

const ES = typeof navigator !== "undefined" && navigator.language.toLowerCase().startsWith("es");
const TXT = ES
  ? {
      steps: ["Entorno Python (WebAssembly)", "API de MonxuPlan y motor de planificación", "Fábrica de demostración", "Primer plan con el motor real", "Inicio de sesión"],
      title: "Arrancando el servidor de planificación en tu navegador",
      failed: "MonxuPlan no ha podido arrancar en este navegador",
      needs: "El entorno Python necesita WebAssembly y Web Workers. Error:",
      note: "La primera visita descarga unos 40 MB, crea la fábrica de demostración y calcula un primer plan (alrededor de un minuto). Las visitas siguientes reabren tus datos guardados.",
      noStore: "El almacenamiento del navegador está bloqueado aquí: tus cambios duran hasta que cierres la página.",
      detail: {
        runtime: "Cargando el entorno Python",
        code: "Cargando MonxuPlan",
        seed: "Creando la fábrica de demostración (solo la primera vez)",
        open: "Abriendo tus datos guardados",
        plan: "Planificando la planta de Sevilla",
        login: "Entrando como planificador de demostración",
      } as Record<string, string>,
    }
  : {
      steps: ["Python runtime (WebAssembly)", "MonxuPlan API and planning engine", "Demo factory", "First plan with the real engine", "Signing in"],
      title: "Starting the planning server in your browser",
      failed: "MonxuPlan could not start in this browser",
      needs: "The Python runtime needs WebAssembly and Web Workers. The error was:",
      note: "The first visit downloads about 40 MB, builds the demo factory and computes a first plan (about a minute). Later visits reopen your saved data.",
      noStore: "Browser storage is blocked here: your changes last until you close this page.",
      detail: {
        runtime: "Loading the Python runtime",
        code: "Loading MonxuPlan",
        seed: "Creating the demo factory (first visit only)",
        open: "Opening your saved data",
        plan: "Planning the Sevilla plant",
        login: "Signing in as the demo planner",
      } as Record<string, string>,
    };
const STEP_KEYS = ["runtime", "code", "seed", "plan", "login"];

function Boot({ onReady }: { onReady: () => void }) {
  const [step, setStep] = useState("runtime");
  const [detail, setDetail] = useState(TXT.detail.runtime);
  const [fatal, setFatal] = useState<string | null>(null);
  const [started] = useState(() => Date.now());
  const [elapsed, setElapsed] = useState(0);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    const t = setInterval(() => setElapsed(Math.round((Date.now() - started) / 1000)), 500);
    const off = onBoot(async (m) => {
      if (m.kind === "boot") {
        if (m.step && m.step !== "ready") {
          setStep(m.step === "open" ? "seed" : m.step);
          setDetail(TXT.detail[m.step] || m.detail || "");
        }
      } else if (m.kind === "fatal") {
        setFatal(m.text || "Unknown error");
      } else if (m.kind === "ready") {
        if (!m.persist) setNote(TXT.noStore);
        try {
          await signIn();
          if (m.fresh) {
            setStep("plan");
            setDetail(TXT.detail.plan);
            await firstPlan((s) => setDetail(`${TXT.detail.plan} · ${s}`));
          }
          setStep("login");
          setDetail(TXT.detail.login);
          onReady();
        } catch (e) {
          setFatal(String(e));
        }
      }
    });
    startBackend(document.baseURI);
    return () => {
      clearInterval(t);
      off();
    };
  }, [onReady, started]);

  const idx = STEP_KEYS.indexOf(step);
  return (
    <div className="boot">
      <div className="boot-card" role="status" aria-live="polite">
        <div className="boot-brand">
          <svg width="28" height="28" viewBox="0 0 32 32" aria-hidden="true">
            <rect width="32" height="32" rx="4" fill="#12233f" />
            <rect x="6" y="8" width="12" height="4" rx="1" fill="#6fa8ff" />
            <rect x="10" y="14" width="14" height="4" rx="1" fill="#ffffff" />
            <rect x="8" y="20" width="9" height="4" rx="1" fill="#6fa8ff" />
          </svg>
          <span>MonxuPlan</span>
        </div>
        {fatal ? (
          <>
            <h1>{TXT.failed}</h1>
            <p className="boot-detail">{TXT.needs}</p>
            <pre className="boot-error">{fatal.slice(0, 1200)}</pre>
          </>
        ) : (
          <>
            <h1>{TXT.title}</h1>
            <ol className="boot-steps">
              {TXT.steps.map((label, i) => (
                <li key={label} data-state={i < idx ? "done" : i === idx ? "active" : "todo"}>
                  <span className="boot-dot" aria-hidden="true">
                    {i < idx ? "✓" : ""}
                  </span>
                  {label}
                </li>
              ))}
            </ol>
            <p className="boot-detail">
              {detail} · {elapsed} s
            </p>
            <p className="boot-note">{TXT.note}</p>
            {note && <p className="boot-note">{note}</p>}
          </>
        )}
      </div>
    </div>
  );
}

const call = async (path: string, body?: unknown) => {
  const r = await apiFetch(
    new Request("https://monxuplan.local/api/v1" + path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  );
  return { ok: r.ok, json: await r.json().catch(() => null) };
};

async function signIn() {
  try {
    if (!localStorage.getItem("mx.locale") && ES) localStorage.setItem("mx.locale", "es");
  } catch {
    /* storage unavailable */
  }
  if ((await call("/auth/me")).ok) return;
  const r = await call("/auth/login", { username: "planner", password: "Monxu-Demo-2026" });
  if (!r.ok) throw new Error("Demo sign-in failed: " + JSON.stringify(r.json));
}

/** First visit: one real planning run of the main plant, so the screens open on an actual plan. */
async function firstPlan(onStep: (s: string) => void) {
  const me = (await call("/auth/me")).json;
  const plant = me?.plants?.find((p: any) => p.code === "SEV") || me?.plants?.[0];
  if (!plant) return;
  const scenarios = (await call(`/scenarios?plant_id=${plant.id}`)).json || [];
  const live = scenarios.find((s: any) => s.is_live);
  if (!live) return;
  const es = new EventSource(`/api/v1/events/stream?plant_id=${plant.id}`);
  es.addEventListener("planning.run.progress", (ev) => {
    try {
      const d = JSON.parse((ev as MessageEvent).data).data;
      if (d?.current_step) onStep(d.current_step);
    } catch {
      /* ignore */
    }
  });
  const run = await call("/planning/run", { scenario_id: live.id, mode: "OPTIMIZE", solver: { provider: "heuristic", profile: "QUICK", reproducible: true }, note: "First plan of the browser edition" });
  // the worker executes queued runs before it answers the next request
  if (run.ok) await call(`/planning/runs/${run.json.run_id}`);
  es.close();
}

function Routed() {
  const { path } = useRouterState();
  if (path.startsWith("/login")) return <LoginPage />;
  const Page: ComponentType = resolveRoute(path);
  return (
    <AppLayout>
      <Page key={path} />
    </AppLayout>
  );
}

function App() {
  const [ready, setReady] = useState(false);
  const { reloads } = useRouterState();
  if (!ready) return <Boot onReady={() => setReady(true)} />;
  return (
    <Providers key={reloads}>
      <Routed />
    </Providers>
  );
}

function Root({ children }: { children: ReactNode }) {
  return <StrictMode>{children}</StrictMode>;
}

const el = document.getElementById("monxuplan-root")!;
createRoot(el).render(
  <Root>
    <App />
  </Root>,
);
