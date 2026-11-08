import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { ChevronRight } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { ScrollArea } from "@/components/ui/scroll-area";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { cn } from "@/lib/utils";
import { fmtTime, layerClass } from "@/lib/format";
import { groupRows } from "@/lib/story";
import { LAYERS, type Incident, type Row } from "@/lib/types";

function RowLine({ r, dim, onHover, onOpen }: { r: Row; dim: boolean; onHover: (cid: string | null) => void; onOpen: (cid: string) => void }) {
  const [t, ms] = fmtTime(r.ts);
  return (
    <div
      onMouseOver={() => onHover(r.cid || null)}
      onClick={() => r.cid && onOpen(r.cid)}
      title={r.topic}
      className={cn(
        "row-new grid cursor-pointer grid-cols-[5.6rem_4.6rem_1fr] gap-3 px-3 py-1 text-[0.92rem] leading-snug transition-opacity hover:bg-accent/50",
        dim && "opacity-40", r.level === "error" && "bg-destructive/10",
      )}
    >
      <span className="text-xs text-muted-foreground">{t}<span className="opacity-60">{ms}</span></span>
      <span className={cn("text-xs font-semibold", layerClass[r.layer])}>{r.layer === "LLM" ? "LLM" : r.layer.charAt(0) + r.layer.slice(1).toLowerCase()}</span>
      <span className={cn("[overflow-wrap:anywhere]", r.level === "error" && "font-medium text-red-200", r.level === "warn" && "text-warn")}>{r.text}</span>
    </div>
  );
}

export function ActivityLog({ rows, incidents, present, onOpenChain }: {
  rows: Row[]; incidents: Incident[]; present: boolean; onOpenChain: (cid: string) => void;
}) {
  const [visible, setVisible] = useState<string[]>([...LAYERS]);
  const [paused, setPaused] = useState(false);
  const [hover, setHover] = useState<string | null>(null);
  const [userOpen, setUserOpen] = useState<Record<string, boolean>>({});
  const viewport = useRef<HTMLDivElement>(null);
  const shown = useMemo(() => rows.filter((r) => visible.includes(r.layer)), [rows, visible]);
  const { groups, system } = useMemo(() => groupRows(shown, incidents), [shown, incidents]);
  const lastSeq = rows.length ? rows[rows.length - 1].seq : 0;

  // follow the newest row, unless the pointer is on the list (hover = pause, to read)
  useLayoutEffect(() => {
    const el = viewport.current;
    if (el && !paused) el.scrollTop = el.scrollHeight;
  }, [lastSeq, paused, visible, userOpen]);

  return (
    <section className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-3 px-5 py-2.5">
        <h2 className="text-base font-semibold">Activity</h2>
        <Badge variant={paused ? "warn" : "ok"}>{paused ? "Paused for reading" : "Live"}</Badge>
        <ToggleGroup type="multiple" value={visible} onValueChange={setVisible} size="sm" className="ml-auto" aria-label="Layers shown">
          {LAYERS.map((l) => (
            <ToggleGroupItem key={l} value={l} className={cn("h-7 rounded-full px-2.5 text-xs font-semibold data-[state=off]:opacity-45", layerClass[l])}>
              {l === "LLM" ? "LLM" : l.charAt(0) + l.slice(1).toLowerCase()}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      </div>
      {!present && system.length > 0 && (
        <div className="mx-5 mb-2 rounded-md bg-secondary/50 py-1">
          {system.slice(-3).map((r) => <RowLine key={r.seq} r={r} dim={false} onHover={() => {}} onOpen={onOpenChain} />)}
        </div>
      )}
      <ScrollArea className="min-h-0 flex-1" viewportRef={viewport}>
        <div className="px-5 pb-4" onMouseEnter={() => setPaused(true)} onMouseLeave={() => { setPaused(false); setHover(null); }}>
          {groups.length === 0 && <p className="py-10 text-center text-muted-foreground">Nothing to respond to yet. Incident threads appear here, one per cause and effect chain.</p>}
          {groups.map((g, gi) => {
            const open = userOpen[g.key] ?? (g.open || g.hasError || gi === groups.length - 1);
            const status = g.hasError ? { v: "destructive" as const, t: "Needs attention" } : g.open ? { v: "warn" as const, t: "Open" } : { v: "ok" as const, t: "Resolved" };
            return (
              <Collapsible key={g.key} open={open} onOpenChange={(o) => setUserOpen((u) => ({ ...u, [g.key]: o }))} className="mb-3 border-l-2 border-border pl-0">
                <CollapsibleTrigger className="group sticky top-0 z-10 flex w-full items-center gap-3 bg-background px-3 py-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                  <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
                  <span className="text-base font-semibold">{g.title}</span>
                  <Badge variant={status.v}>{status.t}</Badge>
                  <span className="ml-auto text-xs text-muted-foreground">{g.rows.length} steps since {fmtTime(g.first)[0]}</span>
                </CollapsibleTrigger>
                <CollapsibleContent>
                  {g.rows.map((r) => <RowLine key={r.seq} r={r} dim={!!hover && r.cid !== hover} onHover={setHover} onOpen={onOpenChain} />)}
                </CollapsibleContent>
              </Collapsible>
            );
          })}
        </div>
      </ScrollArea>
    </section>
  );
}
