"""Start the City Brain with one command (from brain/).

    python run.py                    # all Brain modules (config/launch.yaml)
    python run.py --mocks            # + mock Twin and mock Guardian: the Brain runs alone (R1)
    python run.py --mocks twin       # only the mock Twin (the real Guardian is connected)
    python run.py --mocks --mock-llm # + scripted LLM instead of Ollama (no GPU)
    python run.py --only input,llm   # a subset
    python run.py --without graph

Every module runs as its own process, so a crash in one never stops the others: the launcher
restarts it with back-off and leaves it stopped if it keeps crashing. Output lines are prefixed
with the module name. Ctrl+C stops everything.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from src.common import config  # noqa: E402

COLOURS = ["36", "32", "33", "35", "34", "96", "92", "93", "95", "94"]


class Module:
    def __init__(self, name: str, target: str, colour: str, cfg: dict):
        self.name, self.target, self.colour = name, target, colour
        self.restart_cfg = cfg["restart"]
        self.proc: subprocess.Popen | None = None
        self.crashes: deque[float] = deque()
        self.gave_up = False
        self.stopping = False
        self._restart: threading.Timer | None = None

    def start(self) -> None:
        if self.stopping:                          # a restart that fires after Ctrl+C does nothing
            return
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.proc = subprocess.Popen([sys.executable, "-m", self.target], cwd=HERE, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace", bufsize=1, creationflags=flags)
        threading.Thread(target=self._pump, daemon=True).start()
        say(self, f"started (pid {self.proc.pid})")

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            say(self, line.rstrip())

    def check(self) -> None:
        if self.gave_up or self.proc is None or self.proc.poll() is None:
            return
        now = time.monotonic()
        self.crashes.append(now)
        while self.crashes and now - self.crashes[0] > 60:
            self.crashes.popleft()
        if len(self.crashes) > int(self.restart_cfg["max_per_minute"]):
            self.gave_up = True
            say(self, f"exited with code {self.proc.returncode} too often: left stopped", error=True)
            return
        backoff = self.restart_cfg["backoff_s"]
        delay = backoff[min(len(self.crashes) - 1, len(backoff) - 1)]
        say(self, f"exited with code {self.proc.returncode}, restarting in {delay} s", error=True)
        self.proc = None
        self._restart = threading.Timer(delay, self.start)
        self._restart.daemon = True
        self._restart.start()

    def stop(self) -> None:
        self.stopping = True
        if self._restart is not None:
            self._restart.cancel()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
                self.proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                self.proc.kill()


_print_lock = threading.Lock()


def say(m: Module | None, text: str, error: bool = False) -> None:
    name = m.name if m else "launcher"
    colour = "31" if error else (m.colour if m else "37")
    with _print_lock:
        print(f"\x1b[{colour}m{name:>9}\x1b[0m | {text}", flush=True)


def wait_for_broker(seconds: float) -> bool:
    broker = config.load("broker")
    deadline = time.monotonic() + seconds
    while True:
        try:
            with socket.create_connection((broker["host"], int(broker["port"])), timeout=2):
                return True
        except OSError:
            if time.monotonic() > deadline:
                return False
            time.sleep(1)


def select(args, cfg: dict) -> dict[str, str]:
    mods = dict(cfg["modules"])
    if args.mock_llm:
        mods["llm"] = cfg["mock_llm"]
    if args.mocks:
        wanted = None if args.mocks in (True, "all") else set(args.mocks.split(","))
        unknown = (wanted or set()) - set(cfg["mocks"])
        if unknown:
            raise SystemExit(f"unknown mock(s): {', '.join(sorted(unknown))} (known: {', '.join(cfg['mocks'])})")
        mods.update({k: v for k, v in cfg["mocks"].items() if wanted is None or k in wanted})
    if args.only:
        mods = {k: v for k, v in mods.items() if k in args.only.split(",")}
    for name in (args.without or "").split(","):
        mods.pop(name, None)
    return mods


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mocks", nargs="?", const="all", default=None, metavar="twin,guardian",
                    help="also start the mock layers: all of them, or only those listed")
    ap.add_argument("--mock-llm", action="store_true", help="scripted LLM instead of Ollama")
    ap.add_argument("--only", help="comma-separated module names")
    ap.add_argument("--without", help="comma-separated module names to leave out")
    args = ap.parse_args()
    cfg = config.load("launch")

    if os.name == "nt":
        os.system("")                                   # enable ANSI colours in the Windows console
    if cfg.get("broker_wait_s") and not wait_for_broker(float(cfg["broker_wait_s"])):
        b = config.load("broker")
        say(None, f"no broker at {b['host']}:{b['port']} (start it: docker compose up -d broker)", error=True)
        return 1
    mods = [Module(n, t, COLOURS[i % len(COLOURS)], cfg) for i, (n, t) in enumerate(select(args, cfg).items())]
    for m in mods:
        m.start()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: stop.set())
    try:
        while not stop.wait(1.0):
            for m in mods:
                m.check()
    finally:
        say(None, "stopping")
        for m in mods:
            m.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
