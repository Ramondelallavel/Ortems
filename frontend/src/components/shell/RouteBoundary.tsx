"use client";
import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button, Icon } from "@/components/ui";
import { useSession } from "@/lib/session";

/** A screen that throws (data of an unexpected shape, a bug) must not take the whole application with
 * it: the navigation stays, the screen says what happened and can be retried. It starts afresh when
 * the route changes. Used by both the Next app and the single-page browser edition. */
export class RouteBoundary extends Component<{ children: ReactNode; resetKey: string }, { error: Error | null; attempt: number }> {
  state = { error: null as Error | null, attempt: 0 };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // stays in the browser console for support: the screen itself only shows a short message
    console.error("screen failed", error, info.componentStack);
  }

  componentDidUpdate(prev: { resetKey: string }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
  }

  render() {
    if (this.state.error) {
      return <Failed error={this.state.error} onRetry={() => this.setState((s) => ({ error: null, attempt: s.attempt + 1 }))} />;
    }
    // a new key remounts the screen on retry, so its own state and requests start again
    return <div key={this.state.attempt} className="contents">{this.props.children}</div>;
  }
}

function Failed({ error, onRetry }: { error: Error; onRetry: () => void }) {
  const { t } = useSession();
  return (
    <div role="alert" className="m-3 p-4 border border-red-600/30 bg-red-100 rounded-[3px] text-[13px] max-w-3xl">
      <div className="font-semibold text-red-600 flex items-center gap-1.5">
        <Icon name="alert" /> {t("This screen could not be displayed")}
      </div>
      <p className="mt-1 text-graphite-800">{t("The data it received is not in the expected form. The rest of the application keeps working: try again, or open another screen.")}</p>
      <pre className="mt-2 text-[11px] text-slate-600 whitespace-pre-wrap break-words">{error.message}</pre>
      <div className="flex gap-2 mt-2">
        <Button size="sm" icon="refresh" onClick={onRetry}>
          {t("common.retry")}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => window.location.reload()}>
          {t("Reload the page")}
        </Button>
      </div>
    </div>
  );
}
