import { useEffect, useRef } from "react";
import { Info } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { drawMap, mountMap } from "@/map/city";
import type { State } from "@/lib/types";

function Dot({ c }: { c: string }) { return <span className="mr-2 inline-block h-2.5 w-2.5 rounded-full align-middle" style={{ background: c }} />; }

export function CityMap({ state }: { state: State | null }) {
  const el = useRef<HTMLDivElement>(null);
  useEffect(() => { if (el.current) mountMap(el.current); }, []);
  useEffect(() => { if (state) drawMap(state); }, [state]);
  return (
    <div className="relative flex min-h-0 flex-col">
      <div className="flex items-center gap-3 px-5 py-2">
        <h2 className="text-lg font-bold">City</h2>
        <span className="text-sm text-muted-foreground">As the Brain sees it</span>
        <Popover>
          <PopoverTrigger className="ml-auto flex items-center gap-1.5 rounded-md px-2 py-1 text-sm text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <Info className="h-4 w-4" /> How to read the map
          </PopoverTrigger>
          <PopoverContent className="w-96 text-sm leading-relaxed">
            <p className="mb-2 font-semibold">The city as the Brain believes it</p>
            <p className="mb-3 text-muted-foreground">Water and smoke rise with the readings the Brain trusts. A forged reading never floods a zone.</p>
            <ul className="space-y-1.5">
              <li><Dot c="#62c08f" />Sensor trusted by Guardian</li>
              <li><Dot c="#e9a93b" />Sensor suspicious</li>
              <li><Dot c="#e5584f" />Sensor rejected or quarantined</li>
              <li><Dot c="#475569" />Not checked (Guardian offline)</li>
              <li><b className="mr-2">A F P R</b>Ambulance, fire truck, police, rescue unit</li>
              <li>Road colour shows traffic: green flowing, amber busy, red jammed</li>
              <li>A line from the Brain to a device is a command in flight: blue sent, green acknowledged, red failed</li>
            </ul>
          </PopoverContent>
        </Popover>
      </div>
      <div ref={el} className="flex min-h-0 flex-1 px-3 pb-2 [&_svg]:h-full [&_svg]:w-full">
        <div className="m-auto text-muted-foreground">Waiting for the city description…</div>
      </div>
    </div>
  );
}
