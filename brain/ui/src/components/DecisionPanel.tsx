import { Check } from "lucide-react";
import { cn } from "@/lib/utils";
import type { Decision } from "@/lib/types";

export function DecisionPanel({ decision: d }: { decision: Decision | null | undefined }) {
  if (!d) {
    return <div className="px-5 py-4 text-muted-foreground">No decision yet. The Brain chooses a response once an incident opens.</div>;
  }
  const opts = [{ origin: d.origin, score: d.score, win: true }].concat((d.alternatives || []).map((a) => ({ origin: a.origin, score: a.score, win: false })));
  const max = Math.max(...opts.map((o) => Number(o.score) || 0), 1e-9);
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-x-8 gap-y-2 px-5 py-2.5">
      <div>
        <div className="text-xs text-muted-foreground">Chosen response{d.mode === "safe" ? " (safe mode)" : ""}</div>
        <ul className="mt-1 space-y-0.5 text-base font-semibold">
          {(d.actions || []).length ? d.actions!.map((a) => <li key={a}>{a}</li>) : <li className="text-muted-foreground">No action</li>}
        </ul>
        {d.explanation && <p className="mt-1 text-xs leading-snug text-muted-foreground">{d.explanation}</p>}
      </div>
      <div>
        <div className="text-xs text-muted-foreground">Why this one: score of each proposal</div>
        <div className="mt-1.5 space-y-1.5">
          {opts.map((o, i) => (
            <div key={i} className={cn("grid grid-cols-[4.5rem_1fr_3rem] items-center gap-3", o.win ? "font-semibold" : "text-muted-foreground")}>
              <span className="flex items-center gap-1">{o.origin === "llm" ? "LLM" : o.origin === "rules" ? "Rules" : o.origin}{o.win && <Check className="h-4 w-4 text-ok" />}</span>
              <div className="h-2 rounded-sm bg-secondary">
                <div className={cn("h-full rounded-sm", o.win ? "bg-ok" : "bg-muted-foreground/40")} style={{ width: `${Math.max(4, ((Number(o.score) || 0) / max) * 100)}%` }} />
              </div>
              <span className="text-right">{(Number(o.score) || 0).toFixed(2)}</span>
            </div>
          ))}
        </div>
        <div className="mt-1 text-xs text-muted-foreground">Risk {d.risk || "unknown"}</div>
      </div>
    </div>
  );
}
