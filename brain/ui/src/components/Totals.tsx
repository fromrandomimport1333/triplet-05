import { cn } from "@/lib/utils";
import type { State } from "@/lib/types";

function Total({ value, label, tone }: { value: number; label: string; tone?: "bad" | "warn" | "ok" }) {
  const colour = value === 0 ? "text-muted-foreground/60" : tone === "bad" ? "text-destructive" : tone === "warn" ? "text-warn" : tone === "ok" ? "text-ok" : "text-foreground";
  return (
    <div className="flex items-baseline gap-2">
      <dd className={cn("text-base font-semibold", colour)}>{value}</dd>
      <dt className="text-xs text-muted-foreground">{label}</dt>
    </div>
  );
}

export function Totals({ state }: { state: State | null }) {
  const c = state?.counters || {};
  return (
    <footer className="border-t px-5 py-1.5">
      <dl className="flex flex-wrap items-center gap-x-9 gap-y-1">
        <Total value={c.msgs_per_s ?? 0} label="messages per second" />
        <Total value={c.sent ?? 0} label="commands sent" />
        <Total value={c.acked ?? 0} label="acknowledged" tone="ok" />
        <Total value={c.refused ?? 0} label="refused by safety rules" tone="bad" />
        <Total value={c.failed ?? 0} label="failed" tone="bad" />
        <Total value={c.alerts ?? 0} label="alerts" tone="warn" />
        <Total value={c.errors ?? 0} label="inputs rejected" tone="warn" />
      </dl>
    </footer>
  );
}
