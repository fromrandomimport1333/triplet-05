import { useEffect, useMemo, useState } from "react";
import { ActivityLog } from "@/components/ActivityLog";
import { ChainDialog } from "@/components/ChainDialog";
import { CityMap } from "@/components/CityMap";
import { DecisionPanel } from "@/components/DecisionPanel";
import { Header } from "@/components/Header";
import { LineDiagram } from "@/components/LineDiagram";
import { StoryBanner } from "@/components/StoryBanner";
import { Totals } from "@/components/Totals";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useTimeline } from "@/hooks/useTimeline";
import { deriveStory } from "@/lib/story";

const KEY = "cb-present";

export default function App() {
  const { rows, state, connected, loadChain, localChain } = useTimeline();
  const [chain, setChain] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [present, setPresent] = useState(() => { try { return localStorage.getItem(KEY) === "1"; } catch { return false; } });

  useEffect(() => {
    document.documentElement.classList.toggle("present", present);
    try { localStorage.setItem(KEY, present ? "1" : "0"); } catch { /* ignore */ }
  }, [present]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "p" || e.key === "P") setPresent((v) => !v);
      if (e.key === "f" || e.key === "F") {
        if (document.fullscreenElement) void document.exitFullscreen(); else void document.documentElement.requestFullscreen().catch(() => {});
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const story = useMemo(() => deriveStory(state, rows, selected), [state, rows, selected]);

  return (
    <TooltipProvider delayDuration={150}>
      <div className="flex h-screen flex-col">
        <Header state={state} connected={connected} present={present} onPresent={() => setPresent((v) => !v)} />
        <StoryBanner story={story} state={state} onSelect={setSelected} />
        <LineDiagram stations={story.stations} idle={!story.incident} />
        <main className="grid min-h-0 flex-1 grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)] border-t">
          <div className="grid min-h-0 grid-rows-[minmax(0,1fr)_auto]">
            <CityMap state={state} />
            <div className="border-t"><DecisionPanel decision={state?.decision} /></div>
          </div>
          <div className="min-h-0 border-l"><ActivityLog rows={rows} incidents={state?.incidents || []} present={present} onOpenChain={setChain} /></div>
        </main>
        {!present && <Totals state={state} />}
        <ChainDialog cid={chain} onClose={() => setChain(null)} loadChain={loadChain} localChain={localChain} />
      </div>
    </TooltipProvider>
  );
}
