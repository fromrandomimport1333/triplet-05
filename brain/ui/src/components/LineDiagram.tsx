import { Check, Minus, X } from "lucide-react";
import { cn } from "@/lib/utils";
import type { Station } from "@/lib/story";

/** The response drawn like a transit line: one station per step, filled as it is reached. */
export function LineDiagram({ stations, idle }: { stations: Station[]; idle: boolean }) {
  return (
    <ol className="grid grid-cols-7 px-5 pb-2 pt-3" aria-label="Response steps">
      {stations.map((s, i) => {
        const prev = stations[i - 1], reached = (x?: Station) => !!x && (x.state === "done" || x.state === "failed" || x.state === "skipped");
        const leftOn = reached(prev) && (s.state !== "pending");
        const rightOn = reached(s) && !!stations[i + 1] && stations[i + 1].state !== "pending";
        return (
          <li key={s.id} className="relative flex flex-col items-center text-center" title={s.title}>
            <div className="relative flex h-6 w-full items-center justify-center">
              {i > 0 && <span className={cn("absolute left-0 right-1/2 h-0.5", leftOn ? (s.state === "failed" ? "bg-destructive" : "bg-ok") : "bg-border")} />}
              {i < stations.length - 1 && <span className={cn("absolute left-1/2 right-0 h-0.5", rightOn ? "bg-ok" : "bg-border")} />}
              <span
                className={cn(
                  "relative z-10 flex h-6 w-6 items-center justify-center rounded-full border-2 bg-background transition-colors",
                  s.state === "done" && "station-settle border-ok bg-ok text-background",
                  s.state === "active" && "station-live border-warn",
                  s.state === "failed" && "station-settle border-destructive bg-destructive text-background",
                  s.state === "skipped" && "border-dashed border-muted-foreground/60 text-muted-foreground",
                  s.state === "pending" && "border-border",
                  idle && "opacity-60",
                )}
              >
                {s.state === "done" && <Check className="h-3.5 w-3.5" strokeWidth={3.5} />}
                {s.state === "failed" && <X className="h-3.5 w-3.5" strokeWidth={3.5} />}
                {s.state === "skipped" && <Minus className="h-3.5 w-3.5" />}
                {s.state === "active" && <span className="h-2 w-2 rounded-full bg-warn" />}
              </span>
            </div>
            <div className={cn("mt-1 text-sm font-semibold", s.state === "pending" && "text-muted-foreground", s.state === "failed" && "text-destructive", s.state === "active" && "text-warn")}>
              {s.label}
            </div>
            <div className="min-h-[2rem] max-w-full px-1 text-xs leading-tight text-muted-foreground">
              {s.note && <div className={cn("truncate font-medium", s.state === "failed" ? "text-destructive" : "text-foreground/80")}>{s.note}</div>}
              {s.time && <div>{s.time}</div>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
