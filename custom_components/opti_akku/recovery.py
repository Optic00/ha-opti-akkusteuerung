"""SMA reconnect readiness; never substitutes the driver's transaction checks."""


class RecoveryState:
    def __init__(self):
        self.pending = False
        self.good_reads = 0
        self.since = None
        self.last_monotonic = None

    def update(self, online, status, now, allowed=(235, 2119)):
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
            return {
                "status": "offline" if not online else "not_ready",
                "write_ready": False,
                "elapsed_seconds": max(0, now - self.since),
                "good_reads": 0,
            }
        if self.pending:
            self.good_reads += 1
            if self.good_reads < 2:
                return {
                    "status": "recovering",
                    "write_ready": False,
                    "elapsed_seconds": max(0, now - self.since),
                    "good_reads": self.good_reads,
                }
        self.pending = False
        self.since = None
        return {
            "status": "ready",
            "write_ready": True,
            "elapsed_seconds": 0,
            "good_reads": self.good_reads,
        }
