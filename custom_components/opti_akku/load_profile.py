"""Time-weighted rolling load profile without Home Assistant dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil, isfinite

_SNAPSHOT_VERSION = 1


@dataclass(frozen=True, slots=True)
class LoadProfileResult:
    """Current rolling profile values."""

    raw_mean_w: float | None
    forecast_w: float | None
    coverage_seconds: float
    warming_up: bool


@dataclass(frozen=True, slots=True)
class _Sample:
    at: datetime
    value_w: float | None


class LoadProfile:
    """Calculate a bounded, time-weighted rolling mean from point observations."""

    def __init__(
        self,
        *,
        window_seconds: float = 3600,
        max_gap_seconds: float = 60,
        max_samples: int = 4096,
    ) -> None:
        if not isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("window_seconds must be finite and positive")
        if not isfinite(max_gap_seconds) or max_gap_seconds <= 0:
            raise ValueError("max_gap_seconds must be finite and positive")
        minimum_samples = ceil(window_seconds / max_gap_seconds) + 2
        if max_samples < minimum_samples:
            raise ValueError(f"max_samples must be at least {minimum_samples}")
        self.window_seconds = float(window_seconds)
        self.max_gap_seconds = float(max_gap_seconds)
        self.max_samples = max_samples
        self._fingerprint: str | None = None
        self._samples: list[_Sample] = []

    def observe(
        self,
        value_w: float | None,
        now: datetime,
        fingerprint: str,
        min_load_w: float = 0,
    ) -> LoadProfileResult:
        """Record one observation and return the rolling result at ``now``."""
        self._validate_time(now)
        now = now.astimezone(UTC)
        if not isinstance(fingerprint, str):
            raise TypeError("fingerprint must be a string")
        if (
            isinstance(min_load_w, bool)
            or not isinstance(min_load_w, (int, float))
            or not isfinite(min_load_w)
            or not 0 <= min_load_w <= 5000
        ):
            raise ValueError("min_load_w must be a finite number between 0 and 5000")

        if fingerprint != self._fingerprint:
            self._fingerprint = fingerprint
            self._samples.clear()
        elif self._samples and now < self._samples[-1].at:
            self._samples.clear()

        normalized = self._finite_value(value_w)
        sample = _Sample(now, normalized)
        if self._samples and now == self._samples[-1].at:
            self._samples[-1] = sample
        else:
            self._samples.append(sample)
        self._trim(now)

        # An explicitly invalid latest observation must never surface a stale mean.
        if normalized is None:
            return LoadProfileResult(None, None, 0.0, True)

        raw_mean, coverage = self._integrate(now)
        raw_mean = raw_mean if raw_mean is not None else normalized
        forecast = max(raw_mean, float(min_load_w))
        return LoadProfileResult(raw_mean, forecast, coverage, coverage < self.window_seconds)

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-serializable snapshot."""
        return {
            "version": _SNAPSHOT_VERSION,
            "fingerprint": self._fingerprint,
            "samples": [
                {"timestamp": sample.at.isoformat(), "value_w": sample.value_w}
                for sample in self._samples
            ],
        }

    def restore(self, snapshot: object, *, now: datetime, fingerprint: str) -> bool:
        """Restore valid history while explicitly cutting any offline interval."""
        self._validate_time(now)
        now = now.astimezone(UTC)
        if not isinstance(fingerprint, str):
            raise TypeError("fingerprint must be a string")
        samples = self._parse_snapshot(snapshot, fingerprint, now)
        if samples is None:
            self._fingerprint = fingerprint
            self._samples.clear()
            return False

        self._fingerprint = fingerprint
        self._samples = samples
        if self._samples:
            # End the predecessor at its own timestamp. The next observation therefore
            # cannot integrate the Home Assistant downtime, even if it arrives quickly.
            self._samples[-1] = _Sample(self._samples[-1].at, None)
        self._trim(now)
        return True

    def _integrate(self, now: datetime) -> tuple[float | None, float]:
        cutoff = now - timedelta(seconds=self.window_seconds)
        mean: float | None = None
        coverage = 0.0
        for index, sample in enumerate(self._samples):
            if sample.value_w is None:
                continue
            next_at = self._samples[index + 1].at if index + 1 < len(self._samples) else now
            valid_until = min(next_at, sample.at + timedelta(seconds=self.max_gap_seconds), now)
            start = max(sample.at, cutoff)
            seconds = (valid_until - start).total_seconds()
            if seconds > 0:
                new_coverage = coverage + seconds
                mean = (
                    sample.value_w
                    if mean is None
                    else mean + (sample.value_w - mean) * (seconds / new_coverage)
                )
                coverage = new_coverage
        return mean, coverage

    def _trim(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.window_seconds)
        first_relevant = 0
        while (
            first_relevant + 1 < len(self._samples)
            and self._samples[first_relevant + 1].at <= cutoff
        ):
            first_relevant += 1
        if first_relevant:
            del self._samples[:first_relevant]

        if len(self._samples) > self.max_samples:
            # Prefer removing samples which cannot change the piecewise-constant curve.
            index = 1
            while len(self._samples) > self.max_samples and index < len(self._samples) - 1:
                previous = self._samples[index - 1]
                following = self._samples[index + 1]
                if (
                    previous.value_w == self._samples[index].value_w
                    and (following.at - previous.at).total_seconds() <= self.max_gap_seconds
                ):
                    del self._samples[index]
                else:
                    index += 1
            if len(self._samples) > self.max_samples:
                del self._samples[: len(self._samples) - self.max_samples]

    def _parse_snapshot(
        self, snapshot: object, fingerprint: str, now: datetime
    ) -> list[_Sample] | None:
        if not isinstance(snapshot, dict):
            return None
        if snapshot.get("version") != _SNAPSHOT_VERSION:
            return None
        if snapshot.get("fingerprint") != fingerprint:
            return None
        rows = snapshot.get("samples")
        if not isinstance(rows, list) or len(rows) > self.max_samples:
            return None
        parsed: list[_Sample] = []
        try:
            for row in rows:
                if not isinstance(row, dict) or set(row) != {"timestamp", "value_w"}:
                    return None
                timestamp = datetime.fromisoformat(row["timestamp"])
                self._validate_time(timestamp)
                timestamp = timestamp.astimezone(UTC)
                value = self._finite_value(row["value_w"])
                if row["value_w"] is not None and value is None:
                    return None
                if timestamp > now or (parsed and timestamp <= parsed[-1].at):
                    return None
                parsed.append(_Sample(timestamp, value))
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed

    @staticmethod
    def _finite_value(value: object) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        # Snapshots are JSON data; accepting arbitrary objects here would make
        # restore behavior depend on their custom numeric conversion hooks.
        if not isinstance(value, (int, float, str)):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if isfinite(number) and number >= 0 else None

    @staticmethod
    def _validate_time(value: datetime) -> None:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware datetimes")
