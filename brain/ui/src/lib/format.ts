// Time and layer formatting shared by the screen.
export const pad = (n: number | string, w = 2) => String(n).padStart(w, "0");

/** [HH:MM:SS, .mmm] in local time; placeholders when the timestamp is not valid. */
export function fmtTime(ts: string | number | Date): [string, string] {
  const d = new Date(ts);
  if (isNaN(d.getTime())) return ["--:--:--", ".---"];
  return [`${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`, "." + pad(d.getMilliseconds(), 3)];
}

export const layerClass: Record<string, string> = {
  TWIN: "text-twin", GUARDIAN: "text-guardian", BRAIN: "text-brain", LLM: "text-llm",
};
