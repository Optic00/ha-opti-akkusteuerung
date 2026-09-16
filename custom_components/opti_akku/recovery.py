"""SMA reconnect readiness; never substitutes the driver's transaction checks."""

from collections.abc import Collection
from typing import Literal, TypedDict


class RecoveryResult(TypedDict):
    """Connection readiness published to the coordinator and diagnostics."""

    status: Literal["offline", "not_ready", "recovering", "ready"]
    write_ready: bool
    elapsed_seconds: float
    good_reads: int


class RecoveryState:
    def __init__(self) -> None:
        self.pending = False
        self.good_reads = 0
        self.since: float | None = None
        self.last_monotonic: float | None = None

    def update(
        self,
        online: bool,
        status: float | None,
        now: float,
        allowed: Collection[int | float] = (235, 2119),
    ) -> RecoveryResult:
        if self.last_monotonic is not None and now < self.last_monotonic:
            self.pending = True
            self.good_reads = 0
            self.since = now
        self.last_monotonic = now
        if not online or status not in allowed:
            self.pending = True
            self.good_reads = 0
            if self.since is None:
                self.since = now
            since = self.since
            return {
                "status": "offline" if not online else "not_ready",
                "write_ready": False,
                "elapsed_seconds": max(0.0, now - since),
                "good_reads": 0,
            }
        if self.pending:
            self.good_reads += 1
            if self.good_reads < 2:
                since = self.since if self.since is not None else now
                return {
                    "status": "recovering",
                    "write_ready": False,
                    "elapsed_seconds": max(0.0, now - since),
                    "good_reads": self.good_reads,
                }
        self.pending = False
        self.since = None
        return {
            "status": "ready",
            "write_ready": True,
            "elapsed_seconds": 0.0,
            "good_reads": self.good_reads,
        }
