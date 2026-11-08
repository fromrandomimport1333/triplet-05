import { useEffect, useState } from "react";
import { ArrowDown } from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import { fmtTime, layerClass } from "@/lib/format";
import type { Row } from "@/lib/types";

interface Props {
  cid: string | null;
  onClose: () => void;
  loadChain: (cid: string) => Promise<{ local: Row[]; graph: unknown; graphError: string | null }>;
  localChain: (cid: string) => Row[];
}

function Step({ time, layer, text, level }: { time: string; layer?: string; text: string; level?: string }) {
  return (
    <div className="grid grid-cols-[6.5rem_6rem_1fr] gap-3 border-b border-dashed py-2 text-base last:border-0">
      <span className="text-sm text-muted-foreground">{time}</span>
      <span className={cn("text-xs font-semibold tracking-wide", layer && layerClass[layer])}>{layer}</span>
      <span className={cn(level === "error" && "text-red-200", level === "warn" && "text-warn")}>{text}</span>
    </div>
  );
}

export function ChainDialog({ cid, onClose, loadChain, localChain }: Props) {
  const [local, setLocal] = useState<Row[]>([]);
  const [graph, setGraph] = useState<unknown>(null);
  const [graphError, setGraphError] = useState<string | null>(null);
  const [asked, setAsked] = useState(false);

  useEffect(() => {
    if (!cid) return;
    setLocal(localChain(cid)); setGraph(null); setGraphError(null); setAsked(false);
    let alive = true;
    loadChain(cid).then((d) => { if (alive) { setLocal(d.local); setGraph(d.graph); setGraphError(d.graphError); setAsked(true); } });
    return () => { alive = false; };
  }, [cid, loadChain, localChain]);

  const sorted = local.slice().sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : a.seq - b.seq));
  const items = Array.isArray(graph) ? graph : graph && typeof graph === "object"
    ? ((graph as Record<string, unknown>).events || (graph as Record<string, unknown>).nodes || (graph as Record<string, unknown>).chain || null) : null;

  return (
    <Dialog open={!!cid} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[86vh] grid-rows-[auto_1fr]">
        <DialogHeader>
          <DialogTitle>Cause → effect chain <code className="ml-2 text-sm font-normal text-twin">{cid}</code></DialogTitle>
          <DialogDescription>Every step that shares this correlation id, in order.</DialogDescription>
        </DialogHeader>
        <ScrollArea className="max-h-[66vh] pr-3">
          {sorted.length ? sorted.map((r, i) => (
            <div key={r.seq}>
              {i > 0 && <div className="flex justify-center py-0.5 text-muted-foreground/50"><ArrowDown className="h-3.5 w-3.5" /></div>}
              <Step time={fmtTime(r.ts).join("")} layer={r.layer} text={r.text} level={r.level} />
            </div>
          )) : <div className="py-6 text-center text-muted-foreground">No row of this chain on screen</div>}
          <h3 className="mb-1 mt-6 text-sm font-semibold">Graph view</h3>
          {Array.isArray(items) ? items.map((n: Record<string, unknown>, i: number) => (
            <Step key={i} time={fmtTime(String(n.timestamp || n.ts || ""))[0]}
              layer={String(n.layer || n.kind || n.type || "").toUpperCase().slice(0, 9)}
              text={String(n.summary || n.text || n.label || n.id || JSON.stringify(n))} />
          )) : graph ? (
            <pre className="whitespace-pre-wrap rounded-md bg-muted p-3 text-xs text-muted-foreground">{JSON.stringify(graph, null, 2)}</pre>
          ) : (
            <p className="text-sm text-muted-foreground">
              {asked ? `Graph API not available (${graphError}): chain grouped on screen by correlation id.` : "Asking the graph…"}
            </p>
          )}
        </ScrollArea>
      </DialogContent>
    </Dialog>
  );
}
