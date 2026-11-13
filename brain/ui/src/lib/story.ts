// The story the banner tells (one incident, its stations from reading to effect) and the activity
// threads (rows grouped by cause and effect chain). Pure functions of the timeline state and rows.
import { fmtTime } from "./format";
import type { Incident, Row, State } from "./types";

export type StationState = "pending" | "active" | "done" | "failed" | "skipped";
export interface Station { id: string; label: string; state: StationState; time?: string; title?: string; note?: string }
export interface Story {
  tone: "calm" | "watch" | "alarm" | "resolved";
  headline: string;
  subline: string;
  incident: Incident | null;
  others: Incident[];
  stations: Station[];
}

const SEV: Record<string, number> = { critical: 4, high: 3, medium: 2, low: 1 };
const STATIONS: [string, string][] = [["reading", "Reading"], ["verdict", "Verdict"], ["incident", "Incident"],
  ["decision", "Decision"], ["command", "Command"], ["ack", "Acknowledged"], ["effect", "Effect"]];
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
const clip = (s: string, n = 150) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

function pickIncident(st: State | null, selected?: string | null) {
  const all = st?.incidents || [];
  const open = all.filter((i) => i.status !== "closed")
    .sort((a, b) => (SEV[b.severity || ""] || 0) - (SEV[a.severity || ""] || 0) || (b.updated || 0) - (a.updated || 0));
  const closed = all.filter((i) => i.status === "closed").sort((a, b) => (b.updated || 0) - (a.updated || 0));
  const pick = all.find((i) => i.incident_id === selected) || open[0] || closed[0] || null;
  return { pick, others: open.filter((i) => i !== pick) };
}

function rowsOf(rows: Row[], inc: Incident) {
  return rows.filter((r) => (inc.correlation_id && r.cid === inc.correlation_id) || r.incident_id === inc.incident_id);
}

export function deriveStory(st: State | null, rows: Row[], selected?: string | null): Story {
  const { pick: inc, others } = pickIncident(st, selected);
  if (!inc) {
    return {
      tone: "calm", headline: "All clear",
      subline: st?.run ? `Scenario ${st.run.scenario_id || ""} is running. Waiting for a reading that needs a response.`
        : "Waiting for a reading that needs a response.",
      incident: null, others: [], stations: STATIONS.map(([id, label]) => ({ id, label, state: "pending" })),
    };
  }
  const mine = rowsOf(rows, inc), find = (f: (r: Row) => boolean) => mine.find(f);
  const hit: Record<string, Row | undefined> = {
    reading: find((r) => r.kind === "reading_alert" || r.kind === "evidence"),
    verdict: find((r) => !!r.kind?.startsWith("verdict_")),
    incident: find((r) => r.kind === "incident_opened"),
    decision: find((r) => r.kind === "decision"),
    command: find((r) => r.kind === "command" || r.kind === "command_sent"),
    ack: find((r) => (r.kind === "ack" && r.level === "ok") || r.kind === "command_acknowledged"),
    effect: find((r) => r.kind === "effect_confirmed"),
  };
  const refused = find((r) => r.kind === "action_refused");
  const bad = find((r) => r.kind === "command_failed" || (r.kind === "ack" && r.level === "error"));
  const noEffect = bad && /no.?effect/i.test(bad.text) ? bad : undefined;
  const failed = bad && !noEffect ? bad : undefined;
  const rejected = hit.verdict?.kind === "verdict_rejected";
  const guardianOff = st?.layers?.guardian?.status === "offline";
  const fails: Record<string, { row: Row; note: string } | undefined> = {
    verdict: rejected ? { row: hit.verdict!, note: "Rejected" } : undefined,
    command: refused && !hit.command ? { row: refused, note: "Refused" } : undefined,
    ack: failed ? { row: failed, note: "Failed" } : undefined,
    effect: noEffect ? { row: noEffect, note: "No effect" } : undefined,
  };
  let last = -1;
  STATIONS.forEach(([id], k) => { if (hit[id] && !(id === "verdict" && rejected)) last = k; });
  const closed = inc.status === "closed";
  let activeGiven = false;
  const stations: Station[] = STATIONS.map(([id, label], k) => {
    const f = fails[id], r = hit[id];
    const base = { id, label, time: r ? fmtTime(r.ts)[0] : f ? fmtTime(f.row.ts)[0] : undefined, title: (r || f?.row)?.text };
    if (f) return { ...base, state: "failed", note: f.note };
    if (r || k < last) {
      const note = id === "verdict" && r ? cap(r.kind!.replace("verdict_", ""))
        : id === "decision" && st?.decision?.incident_id === inc.incident_id ? st.decision.actions?.[0] : undefined;
      if (id === "verdict" && !r) return { ...base, state: "skipped", note: guardianOff ? "Not checked" : "No verdict" };
      return { ...base, state: "done", note };
    }
    if (!closed && !activeGiven && !Object.values(fails).some(Boolean)) { activeGiven = true; return { ...base, state: "active" }; }
    return { ...base, state: "pending" };
  });
  const firstFail = stations.find((s) => s.state === "failed");
  const lastDone = [...stations].reverse().find((s) => s.state === "done");
  const effect = hit.effect?.text.replace(/^effect confirmed for\s*/i, "");
  let sub = "Incident confirmed, choosing a response.";
  if (closed) sub = "Resolved: no live evidence left.";
  else if (firstFail) sub = clip(fails[firstFail.id]!.row.text);
  else if (lastDone?.id === "effect" && effect) sub = `Effect confirmed: ${clip(effect, 120)}`;
  else if (lastDone?.id === "ack") sub = "Acknowledged. Checking that it worked.";
  else if (lastDone?.id === "command") sub = "Command sent. Waiting for the acknowledgement.";
  else if (lastDone?.id === "decision") sub = `Response chosen${lastDone.note ? ": " + st?.decision?.actions?.join(", ") : ""}.`;
  else if (lastDone?.id === "verdict") sub = `Guardian says ${lastDone.note?.toLowerCase()}. Confirming the incident.`;
  else if (lastDone?.id === "reading") sub = "Reading received. Waiting for Guardian's verdict.";
  const sev = inc.severity || "";
  return {
    tone: closed ? "resolved" : firstFail || sev === "high" || sev === "critical" ? "alarm" : "watch",
    headline: `${cap((inc.type || "incident").replace("_", " "))} at ${inc.zone_name || inc.zone_id || "unknown zone"}`,
    subline: sub, incident: inc, others, stations,
  };
}

export interface RowGroup { key: string; rows: Row[]; title: string; open: boolean; hasError: boolean; first: string }

const CHAIN = /^(incident_|decision|command|action_refused|effect_|llm_proposal|rules_proposal|verdict_)/;

/** Rows of a decision chain become one thread; everything else is system activity. */
export function groupRows(rows: Row[], incidents: Incident[]): { groups: RowGroup[]; system: Row[] } {
  const byCid = new Map<string, Row[]>(), system: Row[] = [];
  for (const r of rows) {
    if (!r.cid) { system.push(r); continue; }
    (byCid.get(r.cid) || byCid.set(r.cid, []).get(r.cid)!).push(r);
  }
  const groups: RowGroup[] = [];
  for (const [cid, list] of byCid) {
    if (!list.some((r) => r.kind && CHAIN.test(r.kind) && !r.kind.startsWith("verdict_"))) { system.push(...list); continue; }
    const inc = incidents.find((i) => i.correlation_id === cid);
    const opened = list.find((r) => r.kind === "incident_opened" || r.kind === "incident_updated");
    const m = opened?.text.match(/incident \w+: (.+?) (?:in|at) (.+?),/);
    const closedRow = list.some((r) => r.kind === "incident_closed");
    groups.push({
      key: cid, rows: list,
      title: inc ? `${cap((inc.type || "").replace("_", " "))} at ${inc.zone_name || inc.zone_id}` : m ? `${cap(m[1])} at ${m[2]}` : clip(list[0].text, 60),
      open: inc ? inc.status !== "closed" : !closedRow,
      hasError: list.some((r) => r.level === "error"),
      first: list[0].ts,
    });
  }
  groups.sort((a, b) => (a.first < b.first ? -1 : 1));
  system.sort((a, b) => a.seq - b.seq);
  return { groups, system };
}
