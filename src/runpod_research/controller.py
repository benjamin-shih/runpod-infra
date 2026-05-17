"""Minimal queue tick controller for RunPod lanes."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .lifecycle import append_event
from .queue import LaneRecord, LaneState, QueueStore, queued_lanes, running_lanes, utc_now


class CapacityUnavailable(RuntimeError):
    """Raised when RunPod cannot place a lane because capacity is unavailable."""


@dataclass(frozen=True)
class LaunchResult:
    pod_id: str
    pod_name: str
    cost_per_hr: float = 0.0
    volume_id: str | None = None
    data_center_id: str | None = None
    manifest_path: str | None = None


@dataclass(frozen=True)
class ReapResult:
    lane_status: str
    final_state: LaneState
    local_archive: str | None = None
    receipt: dict[str, Any] = field(default_factory=dict)


class LaneLauncher(Protocol):
    def launch(self, lane: LaneRecord) -> LaunchResult:
        """Launch one queued lane."""


class LaneReaper(Protocol):
    def reap(self, lane: LaneRecord, *, confirm_cleanup: bool) -> ReapResult:
        """Inspect and possibly reap one running lane."""


@dataclass(frozen=True)
class TickSummary:
    launched: int = 0
    would_launch: int = 0
    recovered_launches: int = 0
    reaped: int = 0
    still_running: int = 0
    failed_launches: int = 0
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "launched": self.launched,
            "would_launch": self.would_launch,
            "recovered_launches": self.recovered_launches,
            "reaped": self.reaped,
            "still_running": self.still_running,
            "failed_launches": self.failed_launches,
            "errors": list(self.errors),
        }


def tick_queue(
    *,
    store: QueueStore,
    launcher: LaneLauncher,
    reaper: LaneReaper,
    confirm_spend: bool,
    confirm_cleanup: bool,
    events_path: Path,
) -> TickSummary:
    """Run one idempotent controller tick.

    A tick first inspects running lanes, then launches all queued lanes if
    billable launch is explicitly confirmed.
    """

    lanes = store.load()
    launched = 0
    would_launch = 0
    recovered_launches = 0
    reaped = 0
    still_running = 0
    failed_launches = 0
    errors: list[str] = []
    append_event(events_path, "tick.start", lane_count=len(lanes), confirm_spend=confirm_spend)

    for lane in lanes:
        if lane.state != LaneState.LAUNCHING:
            continue
        if lane.pod_id and lane.last_error is None:
            continue
        recovered_launches += 1
        reason = lane.last_error or "controller recovered stale LAUNCHING lane before pod_id was recorded"
        replacement = lane.with_updates(
            state=LaneState.QUEUED,
            retry_count=lane.retry_count + 1,
            pod_id=None,
            pod_name=None,
            volume_id=None,
            data_center_id=None,
            manifest_path=None,
            cost_per_hr=0.0,
            launched_at_utc="",
            last_error=reason,
        )
        _replace_in_memory(lanes, replacement)
        append_event(
            events_path,
            "tick.recovered_launching",
            lane_name=lane.lane_name,
            retry_count=replacement.retry_count,
            reason=reason,
        )
    if recovered_launches:
        store.save(lanes)

    for lane in running_lanes(lanes):
        if lane.state == LaneState.SYNCING:
            syncing = lane
            append_event(events_path, "tick.recovered_syncing", lane_name=lane.lane_name)
        else:
            syncing = lane.with_updates(state=LaneState.SYNCING, last_error=None)
            _replace_in_memory(lanes, syncing)
            store.save(lanes)
            append_event(events_path, "tick.syncing", lane_name=lane.lane_name)
        try:
            result = reaper.reap(syncing, confirm_cleanup=confirm_cleanup)
        except Exception as exc:  # noqa: BLE001 - keep the controller moving lane-by-lane.
            errors.append(f"{syncing.lane_name}: reap failed: {exc}")
            replacement = syncing.with_updates(state=LaneState.RUNNING, last_error=str(exc))
        else:
            final_state = (
                LaneState.FAILED_RUNNING
                if result.final_state == LaneState.FAILED_SYNCING
                else result.final_state
            )
            if final_state == LaneState.RUNNING:
                still_running += 1
                replacement = syncing.with_updates(state=LaneState.RUNNING, last_error=None)
            else:
                reaped += 1
                replacement = syncing.with_updates(
                    state=final_state,
                    last_error=None,
                )
        _replace_in_memory(lanes, replacement)
        store.save(lanes)

    for lane in queued_lanes(lanes):
        if not confirm_spend:
            would_launch += 1
            append_event(events_path, "tick.would_launch", lane_name=lane.lane_name)
            continue
        launching = lane.with_updates(state=LaneState.LAUNCHING, last_error=None)
        _replace_in_memory(lanes, launching)
        store.save(lanes)
        try:
            result = launcher.launch(launching)
        except CapacityUnavailable as exc:
            errors.append(f"{lane.lane_name}: capacity unavailable: {exc}")
            replacement = launching.with_updates(
                state=LaneState.QUEUED,
                retry_count=launching.retry_count + 1,
                last_error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            failed_launches += 1
            errors.append(f"{lane.lane_name}: launch failed: {exc}")
            replacement = launching.with_updates(
                state=LaneState.FAILED_LAUNCHING,
                last_error=str(exc),
            )
        else:
            launched += 1
            replacement = launching.with_updates(
                state=LaneState.RUNNING,
                pod_id=result.pod_id,
                pod_name=result.pod_name,
                volume_id=result.volume_id,
                data_center_id=result.data_center_id,
                manifest_path=result.manifest_path,
                cost_per_hr=result.cost_per_hr,
                launched_at_utc=utc_now(),
                last_error=None,
            )
            append_event(
                events_path,
                "tick.launched",
                lane_name=lane.lane_name,
                pod_id=result.pod_id,
                pod_name=result.pod_name,
            )
        _replace_in_memory(lanes, replacement)
        store.save(lanes)

    summary = TickSummary(
        launched=launched,
        would_launch=would_launch,
        recovered_launches=recovered_launches,
        reaped=reaped,
        still_running=still_running,
        failed_launches=failed_launches,
        errors=tuple(errors),
    )
    append_event(events_path, "tick.finish", **summary.__dict__)
    return summary


def _replace_in_memory(lanes: list[LaneRecord], replacement: LaneRecord) -> None:
    for index, lane in enumerate(lanes):
        if lane.lane_name == replacement.lane_name:
            lanes[index] = replacement
            return
    raise KeyError(f"lane {replacement.lane_name!r} is not in queue")


def result_from_reaper_receipt(receipt: dict[str, Any]) -> ReapResult:
    lane_status = str(receipt.get("lane_status", "")).upper()
    if lane_status == "DONE":
        final_state = LaneState.CLEANED if not receipt.get("dry_run") else LaneState.RUNNING
    elif lane_status == "FAILED":
        final_state = LaneState.FAILED_RUNNING if not receipt.get("dry_run") else LaneState.RUNNING
    else:
        final_state = LaneState.RUNNING
    return ReapResult(
        lane_status=lane_status,
        final_state=final_state,
        local_archive=receipt.get("local_archive"),
        receipt=receipt,
    )


class ScriptReaper:
    """Adapter around the packaged one-shot reaper CLI."""

    def __init__(
        self,
        *,
        repo_root: Path,
        manifest_root: Path = Path("build/runpod-launch-manifests"),
        archive_root: Path = Path("artifacts/runpod-lifecycle/sweeps"),
        status_cache: Path = Path("build/runpod-monitor/status-cache"),
        events_path: Path = Path("build/runpod-lifecycle/events.jsonl"),
        confirm_delete_volume: bool = False,
        include_checkpoints: bool = False,
        unreachable_grace_seconds: float | None = None,
    ):
        self.repo_root = repo_root
        self.manifest_root = manifest_root
        self.archive_root = archive_root
        self.status_cache = status_cache
        self.events_path = events_path
        self.confirm_delete_volume = confirm_delete_volume
        self.include_checkpoints = include_checkpoints
        self.unreachable_grace_seconds = unreachable_grace_seconds

    def reap(self, lane: LaneRecord, *, confirm_cleanup: bool) -> ReapResult:
        if not lane.pod_id:
            raise ValueError(f"lane {lane.lane_name} has no pod_id")
        command = [
            sys.executable,
            "-m",
            "runpod_research.reaper",
            "--pod-id",
            lane.pod_id,
            "--manifest-root",
            str(self.manifest_root),
            "--archive-root",
            str(self.archive_root),
            "--status-cache",
            str(self.status_cache),
            "--events-path",
            str(self.events_path),
        ]
        if confirm_cleanup:
            command.extend(["--confirm-stop", "--confirm-delete-pod"])
            if self.confirm_delete_volume:
                command.extend(["--delete-success-volume", "--confirm-delete-volume"])
        else:
            command.append("--dry-run")
        if self.include_checkpoints:
            command.append("--include-checkpoints")
        if self._should_force_unreachable_cleanup(lane=lane, confirm_cleanup=confirm_cleanup):
            command.append("--force-cleanup-unreachable")
        completed = subprocess.run(
            command,
            cwd=self.repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        return result_from_reaper_receipt(json.loads(completed.stdout))

    def _should_force_unreachable_cleanup(self, *, lane: LaneRecord, confirm_cleanup: bool) -> bool:
        if not confirm_cleanup or self.unreachable_grace_seconds is None or not lane.launched_at_utc:
            return False
        launched_at = datetime.strptime(lane.launched_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        return (datetime.now(UTC) - launched_at).total_seconds() >= self.unreachable_grace_seconds
