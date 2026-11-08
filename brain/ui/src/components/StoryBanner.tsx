import { CheckCircle2, ShieldAlert } from "lucide-react";
import { cn } from "@/lib/utils";
import type { Story } from "@/lib/story";
import type { Incident, State } from "@/lib/types";

const RULE: Record<Story["tone"], string> = {
  calm: "bg-ok/50", watch: "bg-warn", alarm: "bg-destructive", resolved: "bg-ok",
};

export function StoryBanner({ story, state, onSelect }: { story: Story; state: State | null; onSelect: (id: string) => void }) {
  const inc = story.incident;
  const conf = inc?.confidence == null ? null : Math.round(inc.confidence * 100);
  return (
    <section className="flex items-stretch gap-5 px-5 pt-1" aria-live="polite">
      <div className={cn("w-1 shrink-0 rounded-full", RULE[story.tone])} />
      <div className="min-w-0 flex-1">
        <h2 className="flex items-center gap-3 text-[1.75rem] font-semibold leading-tight tracking-tight">
          {story.tone === "calm" && <CheckCircle2 className="h-6 w-6 text-ok" aria-hidden />}
          {story.headline}
          {inc && story.tone !== "resolved" && inc.severity && (
            <span className={cn("rounded-md px-2 py-0.5 text-sm font-medium", inc.severity === "high" || inc.severity === "critical" ? "bg-destructive/15 text-destructive" : "bg-warn/15 text-warn")}>
              {inc.severity === "critical" ? "Critical" : inc.severity === "high" ? "High" : inc.severity === "medium" ? "Medium" : "Low"} severity
            </span>
          )}
          {story.tone === "resolved" && <span className="rounded-md bg-ok/15 px-2 py-0.5 text-sm font-medium text-ok">Resolved</span>}
        </h2>
        <p className="mt-0.5 max-w-[80ch] text-base text-muted-foreground">{story.subline}</p>
      </div>
      <div className="flex flex-col items-end justify-between gap-2 text-right">
        {state?.safe_mode && (
          <div className="flex max-w-[24rem] items-start gap-2 rounded-md bg-warn/10 px-2.5 py-1.5 text-left text-xs text-warn">
            <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
            <span><b>Safe mode.</b> Guardian is offline, so risky actions need two kinds of sensor.</span>
          </div>
        )}
        {inc && (
          <div className="text-xs text-muted-foreground">
            {conf !== null && <span>Confidence {conf}%</span>}
            {inc.verified === false ? <span className="ml-3 text-warn">Unverified</span> : inc.verified ? <span className="ml-3 text-ok">Verified by Guardian</span> : null}
          </div>
        )}
        {story.others.length > 0 && (
          <div className="flex flex-wrap justify-end gap-2">
            {story.others.map((o: Incident) => (
              <button key={o.incident_id} onClick={() => onSelect(o.incident_id)}
                className="rounded-full border px-3 py-1 text-sm transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                {(o.type || "").replace("_", " ")} at {o.zone_name || o.zone_id}
              </button>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
