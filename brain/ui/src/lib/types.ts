// Shapes of the timeline server's data (/events, /api/recent, /api/status, /api/chain/{cid}).
// Rebuilt from the components' usage: only the fields the screen reads are named; the rest pass through.
export const LAYERS = ["TWIN", "GUARDIAN", "BRAIN", "LLM"] as const;
export type Layer = (typeof LAYERS)[number];

export interface Row {
  seq: number;
  ts: string;
  layer: string;
  text: string;
  level?: "ok" | "warn" | "error" | string;
  kind?: string;
  cid?: string | null;
  incident_id?: string;
  topic?: string;
  json?: unknown;
  [k: string]: any;
}

export interface Incident {
  incident_id: string;
  correlation_id?: string;
  type?: string;
  zone_id?: string;
  zone_name?: string;
  severity?: string;
  status?: string;
  confidence?: number;
  verified?: boolean;
  updated?: number;
  [k: string]: any;
}

export interface Decision {
  incident_id?: string;
  actions?: string[];
  explanation?: string;
  origin?: string;
  score?: number;
  risk?: string;
  mode?: string;
  alternatives?: any[];
  [k: string]: any;
}

export interface State {
  incidents?: Incident[];
  decision?: Decision | null;
  layers?: Record<string, { status?: string; [k: string]: any }>;
  run?: { scenario_id?: string; [k: string]: any } | null;
  safe_mode?: boolean;
  log_file?: string;
  [k: string]: any;
}

export interface ChainReply {
  rows?: Row[];
  graph?: any;
  local?: Row[];
  graph_error?: string;
  [k: string]: any;
}
