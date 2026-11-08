import { useCallback, useEffect, useRef, useState } from "react";
import type { ChainReply, Row, State } from "@/lib/types";

const MAX_ROWS = 600;     // rows kept on screen
const MAX_MEMORY = 3000;  // rows kept for the chain view

/** Rows (history + SSE), the latest state snapshot and the connection flag. Same routes as ever:
 *  /api/recent, /api/status, /events (row + state events), /api/chain/{cid}. */
export function useTimeline() {
  const [rows, setRows] = useState<Row[]>([]);
  const [state, setState] = useState<State | null>(null);
  const [connected, setConnected] = useState(true);
  const memory = useRef<Row[]>([]);
  const lastSeq = useRef(0);
  const pending = useRef<Row[]>([]);
  const raf = useRef<number | null>(null);

  const flush = useCallback(() => {
    raf.current = null;
    const batch = pending.current;
    pending.current = [];
    if (!batch.length) return;
    setRows((prev) => {
      const next = prev.concat(batch);
      return next.length > MAX_ROWS ? next.slice(next.length - MAX_ROWS) : next;
    });
  }, []);

  const addRows = useCallback((list: Row[]) => {
    for (const r of list) {
      if (r.seq <= lastSeq.current) continue;
      lastSeq.current = r.seq;
      memory.current.push(r);
      if (memory.current.length > MAX_MEMORY) memory.current.shift();
      pending.current.push(r);
    }
    if (pending.current.length && raf.current === null) raf.current = requestAnimationFrame(flush);
  }, [flush]);

  useEffect(() => {
    let es: EventSource | null = null;
    let closed = false;
    const connect = () => {
      if (closed) return;
      es?.close();
      es = new EventSource("/events?after=" + lastSeq.current);
      es.addEventListener("row", (e) => { try { addRows([JSON.parse((e as MessageEvent).data)]); } catch { /* ignore */ } });
      es.addEventListener("state", (e) => {
        setConnected(true);
        try { setState(JSON.parse((e as MessageEvent).data)); } catch (err) { console.error(err); }
      });
      es.onerror = () => setConnected(false);
    };
    fetch("/api/recent?limit=400").then((r) => r.json()).then((l: Row[]) => addRows(l)).catch(() => {})
      .finally(() => {
        fetch("/api/status").then((r) => r.json()).then(setState).catch(() => {});
        connect();
      });
    return () => { closed = true; es?.close(); if (raf.current !== null) cancelAnimationFrame(raf.current); };
  }, [addRows]);

  /** Rows of one chain: those on screen plus what the server still knows. */
  const loadChain = useCallback(async (cid: string): Promise<{ local: Row[]; graph: unknown; graphError: string | null }> => {
    const have = memory.current.filter((r) => r.cid === cid);
    try {
      const d: ChainReply = await fetch("/api/chain/" + encodeURIComponent(cid)).then((r) => r.json());
      const seen = new Set(have.map((r) => r.seq));
      const local = have.concat((d.local || []).filter((r) => !seen.has(r.seq)));
      return { local, graph: d.graph ?? null, graphError: d.graph_error || (d.graph ? null : "no answer") };
    } catch (e) {
      return { local: have, graph: null, graphError: String(e) };
    }
  }, []);

  const localChain = useCallback((cid: string) => memory.current.filter((r) => r.cid === cid), []);

  return { rows, state, connected, loadChain, localChain };
}
