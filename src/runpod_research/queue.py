"""Persistent RunPod lane queue for lifecycle ticks."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl is unavailable on Windows.
    fcntl = None  # type: ignore[assignment]


class LaneState(str, Enum):
    QUEUED = "QUEUED"
    LAUNCHING = "LAUNCHING"
    LAUNCH_UNKNOWN = "LAUNCH-UNKNOWN"
    RUNNING = "RUNNING"
    SYNCING = "SYNCING"
    CLEANED = "CLEANED"
    FAILED_LAUNCHING = "FAILED-LAUNCHING"
    FAILED_RUNNING = "FAILED-RUNNING"
    FAILED_SYNCING = "FAILED-SYNCING"


ACTIVE_STATES = frozenset({LaneState.LAUNCHING, LaneState.RUNNING, LaneState.SYNCING})
BLOCKING_STATES = ACTIVE_STATES | frozenset({LaneState.LAUNCH_UNKNOWN})
TERMINAL_STATES = frozenset(
    {
        LaneState.CLEANED,
        LaneState.FAILED_LAUNCHING,
        LaneState.FAILED_RUNNING,
        LaneState.FAILED_SYNCING,
    }
)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class LaneRecord:
    lane_name: str
    sweep_name: str
    spec_path: str
    job_index: int
    state: LaneState = LaneState.QUEUED
    retry_count: int = 0
    pod_id: str | None = None
    pod_name: str | None = None
    volume_id: str | None = None
    data_center_id: str | None = None
    manifest_path: str | None = None
    cost_per_hr: float = 0.0
    launched_at_utc: str = ""
    created_at_utc: str = ""
    updated_at_utc: str = ""
    last_error: str | None = None

    def __post_init__(self) -> None:
        now = utc_now()
        if not self.created_at_utc:
            object.__setattr__(self, "created_at_utc", now)
        if not self.updated_at_utc:
            object.__setattr__(self, "updated_at_utc", now)
        if not isinstance(self.state, LaneState):
            object.__setattr__(self, "state", LaneState(self.state))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LaneRecord":
        return cls(
            lane_name=str(payload["lane_name"]),
            sweep_name=str(payload["sweep_name"]),
            spec_path=str(payload["spec_path"]),
            job_index=int(payload["job_index"]),
            state=LaneState(str(payload.get("state", LaneState.QUEUED.value))),
            retry_count=int(payload.get("retry_count", 0)),
            pod_id=payload.get("pod_id"),
            pod_name=payload.get("pod_name"),
            volume_id=payload.get("volume_id"),
            data_center_id=payload.get("data_center_id"),
            manifest_path=payload.get("manifest_path"),
            cost_per_hr=float(payload.get("cost_per_hr") or 0.0),
            launched_at_utc=str(payload.get("launched_at_utc") or ""),
            created_at_utc=str(payload.get("created_at_utc") or ""),
            updated_at_utc=str(payload.get("updated_at_utc") or ""),
            last_error=payload.get("last_error"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane_name": self.lane_name,
            "sweep_name": self.sweep_name,
            "spec_path": self.spec_path,
            "job_index": self.job_index,
            "state": self.state.value,
            "retry_count": self.retry_count,
            "pod_id": self.pod_id,
            "pod_name": self.pod_name,
            "volume_id": self.volume_id,
            "data_center_id": self.data_center_id,
            "manifest_path": self.manifest_path,
            "cost_per_hr": self.cost_per_hr,
            "launched_at_utc": self.launched_at_utc,
            "created_at_utc": self.created_at_utc,
            "updated_at_utc": self.updated_at_utc,
            "last_error": self.last_error,
        }

    def with_updates(self, **updates: Any) -> "LaneRecord":
        updates.setdefault("updated_at_utc", utc_now())
        return replace(self, **updates)


class QueueLockError(RuntimeError):
    """Raised when another controller already holds the queue lock."""


class QueueStore:
    """Small atomic JSON queue store.

    The queue is intentionally a single JSON document instead of a database. A
    tick reads, mutates, and atomically replaces it, which is enough for one
    controller process and keeps recovery inspectable. Mutating controller paths
    take an advisory lock so two controllers do not write the same queue at the
    same time.
    """

    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def exclusive_lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+") as handle:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise QueueLockError(
                        f"queue is locked by another controller: {lock_path}"
                    ) from exc
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load(self) -> list[LaneRecord]:
        if not self.path.exists():
            return []
        payload = json.loads(self.path.read_text())
        lanes = payload.get("lanes", payload)
        if not isinstance(lanes, list):
            raise ValueError(f"{self.path} does not contain a lane list")
        return [LaneRecord.from_dict(item) for item in lanes]

    def save(self, lanes: list[LaneRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "updated_at_utc": utc_now(),
            "lanes": [lane.to_dict() for lane in lanes],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self.path)
        _fsync_directory(self.path.parent)

    def replace_lane(self, lane: LaneRecord) -> None:
        lanes = self.load()
        for index, existing in enumerate(lanes):
            if existing.lane_name == lane.lane_name:
                lanes[index] = lane
                self.save(lanes)
                return
        raise KeyError(f"lane {lane.lane_name!r} is not in queue {self.path}")


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def initialize_lanes(
    *,
    spec_path: Path,
    sweep_name: str,
    lane_names: list[str],
    start_index: int = 0,
) -> list[LaneRecord]:
    return [
        LaneRecord(
            lane_name=lane_name,
            sweep_name=sweep_name,
            spec_path=str(spec_path),
            job_index=start_index + offset,
        )
        for offset, lane_name in enumerate(lane_names)
    ]


def queued_lanes(lanes: list[LaneRecord]) -> list[LaneRecord]:
    return [lane for lane in lanes if lane.state == LaneState.QUEUED]


def running_lanes(lanes: list[LaneRecord]) -> list[LaneRecord]:
    return [
        lane
        for lane in lanes
        if lane.state in {LaneState.RUNNING, LaneState.SYNCING} and lane.pod_id
    ]


def terminal_lanes(lanes: list[LaneRecord]) -> list[LaneRecord]:
    return [lane for lane in lanes if lane.state in TERMINAL_STATES]


def queue_is_terminal(lanes: list[LaneRecord]) -> bool:
    return bool(lanes) and len(terminal_lanes(lanes)) == len(lanes)
