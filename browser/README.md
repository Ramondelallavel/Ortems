# MonxuPlan — browser edition

The whole application in one static page: the Next.js app's pages and components, and the MonxuPlan
API (the same Python code as the server: FastAPI app, services, planning engine) running in
[Pyodide](https://pyodide.org) inside a Web Worker. The database is SQLite in the worker, saved to the
browser's IndexedDB. Nothing is simulated: requests go through the real API and every plan is computed
by the real engine.

```
cd browser && npm install && node build.mjs     # → dist/
cd dist && python3 -m http.server 8765          # open http://localhost:8765/ (serve index.html inside a normal HTML document)
```

How it fits together:

| Piece | File |
|---|---|
| In-memory router, `next/link` / `next/navigation` shims, location and download shims | `spa/router.ts`, `spa/shims/*` |
| `fetch('/api/…')` and the live event stream forwarded to the worker, session cookies kept | `spa/bridge.ts` |
| Boot screen, demo sign-in, first plan on the first visit | `spa/main.tsx` |
| Python host: ASGI dispatch without a server, inline thread pool, job pump, event sink | `backend/monxuplan/browser.py` |
| Worker: Pyodide start-up, file bundles, IndexedDB persistence, planning runs between requests | `worker/backend-worker.js` |
| Python files as JSON bundles (artifact hosting serves no archives) | `build_site.py` |

Differences from the server edition (all shown in the app, none hidden):

* Solver: only the **heuristic** provider — OR-Tools (CP-SAT, hybrid LNS, MIP aggregate plan) has no
  WebAssembly build. A run that asks for another provider uses the heuristic and says so in the run
  messages; the aggregate plan returns a clear "server edition only" error.
* A planning run blocks other requests until it finishes (WebAssembly has no threads), so *Stop and
  keep best plan* only takes effect after the run.
* No OpenSSL: passwords use PBKDF2-HMAC-SHA256 instead of scrypt; storing connector or webhook
  credentials is refused (no AES-GCM). Webhooks, e-mail and external connectors cannot reach the network.
* Single user and single browser: the data lives in this browser only.
* Python dependencies: `requirements-browser.txt` (pydantic 2.14 pre-release, the first with a
  WebAssembly build of pydantic-core).
