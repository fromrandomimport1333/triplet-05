"""Run logs (R2): one JSON-lines file per scenario run, a live file outside runs.

    logs/<scenario_id>-<YYYYMMDD-HHMMSS>.log   from a brain/control/run `start` to its `stop`
    logs/live-<YYYYMMDD>.log                    any time no run is active

Every line is flushed at once (optionally fsync'ed), so a crash never loses a written line.
The file name uses the run's own start time when the start message carries a timestamp, and
files are opened in append mode: a timeline restarted in the middle of a run (the start
message is retained) continues the same file, so one run still gives exactly one file.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Callable

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_name(text: str, default: str = "run") -> str:
    out = _SAFE.sub("-", str(text)).strip("-.")
    return out[:80] or default


def parse_iso(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class RunLog:
    def __init__(self, directory: Path, *, clock: Callable[[], float], live_prefix: str = "live",
                 fsync: bool = False):
        self.dir = Path(directory)
        self.clock = clock
        self.live_prefix = live_prefix
        self.fsync = fsync
        self._lock = threading.Lock()
        self._fh: IO[str] | None = None
        self.path: Path | None = None
        self.run: dict[str, Any] | None = None       # {run_id, scenario_id, started, file}
        self._live_day: str | None = None

    # --- runs ---------------------------------------------------------------------------

    def start_run(self, run_id: str | None, scenario_id: str | None, started_iso: str | None = None) -> Path:
        with self._lock:
            if self.run is not None and run_id and self.run.get("run_id") == run_id:
                return self.path                      # same run announced again (retained, reconnect)
            started = parse_iso(started_iso) or datetime.fromtimestamp(self.clock(), timezone.utc)
            utc = started.astimezone(timezone.utc)       # same name as the runner's log (UTC), so they pair up
            name = f"{safe_name(scenario_id or 'scenario')}-{utc:%Y%m%d-%H%M%S}.log"
            self._open(self.dir / name)
            self.run = {"run_id": run_id, "scenario_id": scenario_id,
                        "started": started.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                        "file": name}
            return self.path

    def stop_run(self, run_id: str | None = None) -> bool:
        """Close the run file (if `run_id` matches the active run, or is not given)."""
        with self._lock:
            if self.run is None or (run_id and self.run.get("run_id") and self.run["run_id"] != run_id):
                return False
            self._close()
            self.run = None
            return True

    # --- writing ------------------------------------------------------------------------

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            if self.run is None:
                day = datetime.fromtimestamp(self.clock()).strftime("%Y%m%d")
                if self._fh is None or self._live_day != day:
                    self._open(self.dir / f"{self.live_prefix}-{day}.log")
                    self._live_day = day
            if self._fh is None:
                return
            self._fh.write(line + "\n")
            self._fh.flush()
            if self.fsync:
                os.fsync(self._fh.fileno())

    def close(self) -> None:
        with self._lock:
            self._close()

    @property
    def file_name(self) -> str | None:
        return self.path.name if self.path else None

    # --- helpers ------------------------------------------------------------------------

    def _open(self, path: Path) -> None:
        self._close()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8", newline="\n")
        self.path = path

    def _close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None
                self.path = None
                self._live_day = None
