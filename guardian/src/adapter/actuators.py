"""Actuator / unit acknowledgement consistency (Phase 1 "actuator failures" requirement).

Watches brain/cmd/<target> (what the Brain asked) and twin/actuator/<id>, twin/unit/<id>
(what the Twin says it did). Each of these counts as ONE inconsistency for a device:
  * an ack whose correlation_id matches no known command      (device = the ack's device)
  * an ack for a different target than the command           (device = the ack's device)
  * a command with no ack after ACK_TIMEOUT_S + ACK_GRACE_S   (device = the command target)
  * optionally a brain/alert actuator_no_effect               (device = target_id)
A repeat of the same command id (the Brain's retry) is NOT an anomaly.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class _Cmd:
    target: str
    action: str
    t: float
    acked: bool = False
    timed_out: bool = False


@dataclass
class AckTracker:
    timeout_s: float = 8.0          # ACK_TIMEOUT_S + ACK_GRACE_S
    startup_grace_s: float = 10.0   # unknown correlation ids are not counted right after start
    started_at: float = 0.0
    cmds: dict[str, _Cmd] = field(default_factory=dict)
    max_cmds: int = 10_000

    def on_command(self, cmd_id: str, target: str, action: str, now: float) -> str:
        """Record a command. Returns 'new' or 'retry'."""
        if cmd_id in self.cmds:
            return "retry"
        self.cmds[cmd_id] = _Cmd(target, action, now)
        if len(self.cmds) > self.max_cmds:  # bounded memory: drop the oldest
            self.cmds.pop(next(iter(self.cmds)))
        return "new"

    def on_ack(self, device_id: str, correlation_id: str, now: float) -> tuple[str, str] | None:
        """Check an acknowledgement. Returns (device, why) when it is an inconsistency."""
        c = self.cmds.get(correlation_id)
        if c is None:
            if now - self.started_at < self.startup_grace_s:
                return None
            return device_id, f"ack {correlation_id} matches no known command"
        if c.target != device_id:
            return device_id, f"ack from {device_id} for command {correlation_id} sent to {c.target}"
        c.acked = True  # a repeated ack for the same id is harmless
        return None

    def expire(self, now: float) -> list[tuple[str, str]]:
        """Commands without ack after the timeout -> [(target, why)] (each reported once)."""
        out = []
        for cid, c in self.cmds.items():
            if not c.acked and not c.timed_out and now - c.t > self.timeout_s:
                c.timed_out = True
                out.append((c.target, f"no ack for command {cid} ({c.action}) after {self.timeout_s:.0f}s"))
        return out

    def reset(self, now: float) -> None:
        self.cmds.clear()
        self.started_at = now
