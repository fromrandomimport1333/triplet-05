import { useEffect, useState } from "react";
import { Presentation } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { fmtTime, pad } from "@/lib/format";
import type { State } from "@/lib/types";

const LAYER_NAMES: [string, string][] = [["twin", "Twin"], ["guardian", "Guardian"], ["brain", "Brain"], ["llm", "LLM"]];

function LayerStatus({ name, status, reason }: { name: string; status: string; reason?: string }) {
  const dot = status === "online" ? "bg-ok" : status === "offline" ? "bg-destructive" : status === "degraded" ? "bg-warn" : "bg-muted-foreground/40";
  const word = status === "offline" ? "text-destructive" : status === "degraded" ? "text-warn" : "text-muted-foreground";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div className="flex items-center gap-2">
          <span className={cn("h-2 w-2 rounded-full", dot)} />
          <span className="font-semibold">{name}</span>
          {status !== "online" && <span className={cn("text-sm", word)}>{status}</span>}
        </div>
      </TooltipTrigger>
      <TooltipContent>{reason || `${name} is ${status}`}</TooltipContent>
    </Tooltip>
  );
}

export function Header({ state, connected, present, onPresent }: {
  state: State | null; connected: boolean; present: boolean; onPresent: () => void;
}) {
  const [now, setNow] = useState(new Date());
  useEffect(() => { const t = setInterval(() => setNow(new Date()), 500); return () => clearInterval(t); }, []);
  const layers = state?.layers || {};
  const run = state?.run;
  const secs = run?.started ? Math.max(0, Math.round((now.getTime() - new Date(run.started).getTime()) / 1000)) : 0;
  return (
    <header className="flex items-center gap-6 border-b px-5 py-2">
      <h1 className="text-base font-semibold tracking-tight">City Brain</h1>
      <div className="flex items-center gap-6">
        {LAYER_NAMES.map(([id, name]) => (
          <LayerStatus key={id} name={name} status={layers[id]?.status || "unknown"} reason={layers[id]?.reason} />
        ))}
      </div>
      {!connected && <Badge variant="destructive">Connection lost. Reconnecting.</Badge>}
      <div className="flex-1" />
      {run && (
        <div className="flex items-baseline gap-2 text-muted-foreground" title={state?.log_file ? `logs/${state.log_file}` : undefined}>
          <span className="font-semibold text-foreground">Scenario {run.scenario_id || "?"}</span>
          <span>{pad(Math.floor(secs / 60))}:{pad(secs % 60)}</span>
        </div>
      )}
      <Button variant="ghost" size="sm" onClick={onPresent} aria-pressed={present} title="Presentation mode (key P)">
        <Presentation /> {present ? "Exit presentation" : "Present"}
      </Button>
      <div className="min-w-[5.5ch] text-right text-lg font-medium">{fmtTime(now)[0]}</div>
    </header>
  );
}
