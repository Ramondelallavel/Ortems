"use client";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Badge, Button, Drawer, Icon, Spinner, useToast } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useSession } from "@/lib/session";

type Msg = { role: "user" | "assistant"; content: string; meta?: any };

/** Minimal Markdown (bold, italics, bullet lists, paragraphs) rendered without HTML injection. */
export function Markdown({ text }: { text: string }) {
  const inline = (s: string): ReactNode[] => {
    const out: ReactNode[] = [];
    const re = /(\*\*[^*]+\*\*|\*[^*]+\*)/g;
    let last = 0;
    let m: RegExpExecArray | null;
    let k = 0;
    while ((m = re.exec(s))) {
      if (m.index > last) out.push(s.slice(last, m.index));
      const tok = m[0];
      out.push(tok.startsWith("**") ? <strong key={k++}>{tok.slice(2, -2)}</strong> : <em key={k++}>{tok.slice(1, -1)}</em>);
      last = m.index + tok.length;
    }
    if (last < s.length) out.push(s.slice(last));
    return out;
  };
  const blocks: ReactNode[] = [];
  let list: string[] = [];
  const flush = () => {
    if (list.length) {
      blocks.push(
        <ul key={blocks.length} className="list-disc pl-5 my-1 space-y-0.5">
          {list.map((l, i) => (
            <li key={i}>{inline(l)}</li>
          ))}
        </ul>,
      );
      list = [];
    }
  };
  for (const line of text.split("\n")) {
    const m = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
    if (m) list.push(m[1]);
    else {
      flush();
      if (line.trim()) blocks.push(<p key={blocks.length} className="my-1">{inline(line.replace(/^#+\s*/, ""))}</p>);
    }
  }
  flush();
  return <div className="text-[12.5px] leading-snug">{blocks}</div>;
}

export function AssistantDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t, plant, can } = useSession();
  const toast = useToast();
  const router = useRouter();
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<{ mode: string; model: string | null } | null>(null);
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open && !status) api("/assistant/status").then(setStatus).catch(() => {});
  }, [open, status]);
  useEffect(() => end.current?.scrollIntoView({ block: "end" }), [msgs]);

  const ask = async (question: string) => {
    if (!question.trim() || !plant) return;
    const history = msgs.map((m) => ({ role: m.role, content: m.content }));
    setMsgs((m) => [...m, { role: "user", content: question }]);
    setQ("");
    setBusy(true);
    try {
      const r = await api("/assistant/ask", { body: { question, plant_id: plant.id, history } });
      setMsgs((m) => [...m, { role: "assistant", content: r.answer, meta: r }]);
    } catch (e) {
      setMsgs((m) => [...m, { role: "assistant", content: `▲ ${e instanceof ApiError ? e.message : String(e)}` }]);
    } finally {
      setBusy(false);
    }
  };

  const runAction = async (a: any) => {
    if (a.type === "OPEN" && a.target === "order") {
      router.push(`/planning/orders?q=${encodeURIComponent(a.ref)}`);
      return;
    }
    if (a.type === "WHAT_IF" && plant) {
      try {
        const sc = await api("/scenarios/what-if", { body: { plant_id: plant.id, kind: a.kind, params: a.params, name: `${t("What-if")}: ${a.label}`, run: true } });
        if (sc.run_blocked) toast.warn(t("run.whatIfBlocked"));
        else toast.ok(t("Scenario “{name}” created; planning started.", { name: sc.name }));
        router.push(`/planning/scenarios?id=${sc.id}`);
      } catch (e) {
        toast.error(e);
      }
    }
  };

  const examples = [t("What is the bottleneck?"), t("Which orders are late?"), t("Which materials are short?"), t("How is the plan?")];

  return (
    <Drawer open={open} onClose={onClose} title={t("assistant.title")} width={440}>
      <div className="flex flex-col h-full">
        <div className="px-3 py-2 border-b border-gray-200 text-[11.5px] text-slate-600 flex flex-wrap gap-1.5 items-center">
          <Badge tone={status?.mode === "LLM" ? "info" : "neutral"}>{status?.mode === "LLM" ? t("assistant.llm") : t("assistant.grounded")}</Badge>
          <span>{t("assistant.noChange")}</span>
        </div>
        <div className="flex-1 overflow-auto mx-scroll p-3 space-y-3" aria-live="polite">
          {!msgs.length && (
            <div className="space-y-1.5">
              {examples.map((e) => (
                <button key={e} className="block w-full text-left px-2.5 py-1.5 border border-gray-200 rounded-[3px] hover:bg-gray-50 text-[12.5px]" onClick={() => ask(e)}>
                  {e}
                </button>
              ))}
            </div>
          )}
          {msgs.map((m, i) => (
            <div key={i} className={m.role === "user" ? "ml-10 bg-navy-700 text-white rounded-[4px] px-3 py-2 text-[12.5px]" : "mr-4 bg-gray-50 border border-gray-200 rounded-[4px] px-3 py-2"}>
              {m.role === "user" ? m.content : <Markdown text={m.content} />}
              {m.meta && (
                <div className="mt-2 pt-1.5 border-t border-gray-200 text-[11px] text-slate-600 space-y-1">
                  <div>
                    {m.meta.plan?.number} · {m.meta.mode === "LLM" ? `LLM (${m.meta.model || "model"})` : t("rule-based")} · {m.meta.intent}
                  </div>
                  {!!m.meta.sources?.length && (
                    <div className="flex flex-wrap gap-1">
                      {m.meta.sources.slice(0, 8).map((s: any, j: number) => (
                        <span key={j} className="code bg-white border border-gray-200 rounded px-1">
                          {s.kind}:{s.label}
                        </span>
                      ))}
                    </div>
                  )}
                  {m.meta.actions
                    ?.filter((a: any) => a.type !== "WHAT_IF" || can("scenario:write"))
                    .map((a: any, j: number) => (
                      <Button key={j} size="sm" variant={a.type === "WHAT_IF" ? "primary" : "secondary"} icon={a.type === "WHAT_IF" ? "scenarios" : "orders"} onClick={() => runAction(a)}>
                        {a.type === "WHAT_IF" ? t("Evaluate what-if: {label}", { label: a.label }) : t("Open {ref}", { ref: a.ref })}
                      </Button>
                    ))}
                </div>
              )}
            </div>
          ))}
          {busy && (
            <div className="flex items-center gap-2 text-slate-600 text-[12px]">
              <Spinner /> …
            </div>
          )}
          <div ref={end} />
        </div>
        <form
          className="border-t border-gray-200 p-2 flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            ask(q);
          }}
        >
          <input className="mx-input flex-1" placeholder={t("assistant.placeholder")} aria-label={t("assistant.placeholder")} value={q} onChange={(e) => setQ(e.target.value)} maxLength={2000} />
          <Button type="submit" variant="primary" disabled={!q.trim() || busy} aria-label={t("Send")}>
            <Icon name="chevronRight" />
          </Button>
        </form>
      </div>
    </Drawer>
  );
}
