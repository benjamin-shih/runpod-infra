from __future__ import annotations

from pathlib import Path

import pytest

from runpod_research import launcher
from runpod_research.controller import (
    CapacityUnavailable,
    LaunchResult,
    ReapResult,
    ScriptReaper,
    tick_queue,
)
from runpod_research.controller_cli import maybe_promote_complete_queue
from runpod_research.queue import LaneRecord, LaneState, QueueStore, queue_is_terminal


class FakeLauncher:
    def __init__(self, *results):
        self.results = list(results)
        self.launched: list[str] = []

    def launch(self, lane: LaneRecord):
        self.launched.append(lane.lane_name)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeReaper:
    def __init__(self, results):
        self.results = dict(results)
        self.reaped: list[tuple[str, bool]] = []

    def reap(self, lane: LaneRecord, *, confirm_cleanup: bool):
        self.reaped.append((lane.lane_name, confirm_cleanup))
        return self.results[lane.lane_name]


class CrashAfterObservingSyncingReaper:
    def __init__(self, store: QueueStore):
        self.store = store
        self.observed_states: list[tuple[LaneState, LaneState]] = []

    def reap(self, lane: LaneRecord, *, confirm_cleanup: bool):
        persisted = self.store.load()[0]
        self.observed_states.append((lane.state, persisted.state))
        raise KeyboardInterrupt


def make_store(tmp_path: Path, lanes: list[LaneRecord]) -> QueueStore:
    store = QueueStore(tmp_path / "queue.json")
    store.save(lanes)
    return store


def test_queue_round_trip(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [
            LaneRecord(
                lane_name="lane-a",
                sweep_name="sweep",
                spec_path="configs/runpod/example.json",
                job_index=3,
            )
        ],
    )

    [lane] = store.load()

    assert lane.lane_name == "lane-a"
    assert lane.state == LaneState.QUEUED
    assert lane.job_index == 3


def test_tick_dry_run_does_not_launch_queued_lanes(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [
            LaneRecord(
                lane_name="lane-a",
                sweep_name="sweep",
                spec_path="configs/runpod/example.json",
                job_index=0,
            )
        ],
    )
    launcher = FakeLauncher(LaunchResult(pod_id="pod-a", pod_name="pod-a"))

    summary = tick_queue(
        store=store,
        launcher=launcher,
        reaper=FakeReaper({}),
        confirm_spend=False,
        confirm_cleanup=False,
        events_path=tmp_path / "events.jsonl",
    )

    assert summary.would_launch == 1
    assert launcher.launched == []
    assert store.load()[0].state == LaneState.QUEUED


def test_tick_launches_all_queued_lanes_when_confirmed(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [
            LaneRecord("lane-a", "sweep", "configs/runpod/example.json", 0),
            LaneRecord("lane-b", "sweep", "configs/runpod/example.json", 1),
        ],
    )

    summary = tick_queue(
        store=store,
        launcher=FakeLauncher(
            LaunchResult(pod_id="pod-a", pod_name="pod-a", cost_per_hr=1.0),
            LaunchResult(pod_id="pod-b", pod_name="pod-b", cost_per_hr=1.0),
        ),
        reaper=FakeReaper({}),
        confirm_spend=True,
        confirm_cleanup=False,
        events_path=tmp_path / "events.jsonl",
    )

    lanes = store.load()
    assert summary.launched == 2
    assert [lane.state for lane in lanes] == [LaneState.RUNNING, LaneState.RUNNING]
    assert [lane.pod_id for lane in lanes] == ["pod-a", "pod-b"]
    assert all(lane.launched_at_utc for lane in lanes)


def test_capacity_error_returns_lane_to_queue(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [LaneRecord("lane-a", "sweep", "configs/runpod/example.json", 0)],
    )

    summary = tick_queue(
        store=store,
        launcher=FakeLauncher(CapacityUnavailable("no instances")),
        reaper=FakeReaper({}),
        confirm_spend=True,
        confirm_cleanup=False,
        events_path=tmp_path / "events.jsonl",
    )

    [lane] = store.load()
    assert summary.launched == 0
    assert lane.state == LaneState.QUEUED
    assert lane.retry_count == 1
    assert "no instances" in str(lane.last_error)


def test_stale_launching_lane_without_pod_id_is_requeued_and_retried(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [
            LaneRecord(
                "lane-a",
                "sweep",
                "configs/runpod/example.json",
                0,
                state=LaneState.LAUNCHING,
                retry_count=2,
            )
        ],
    )
    launcher = FakeLauncher(LaunchResult(pod_id="pod-a", pod_name="pod-a"))

    summary = tick_queue(
        store=store,
        launcher=launcher,
        reaper=FakeReaper({}),
        confirm_spend=True,
        confirm_cleanup=False,
        events_path=tmp_path / "events.jsonl",
    )

    [lane] = store.load()
    assert summary.recovered_launches == 1
    assert summary.launched == 1
    assert launcher.launched == ["lane-a"]
    assert lane.state == LaneState.RUNNING
    assert lane.retry_count == 3
    assert lane.pod_id == "pod-a"


def test_other_launch_error_marks_failed_launching(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [LaneRecord("lane-a", "sweep", "configs/runpod/example.json", 0)],
    )

    summary = tick_queue(
        store=store,
        launcher=FakeLauncher(RuntimeError("bad spec")),
        reaper=FakeReaper({}),
        confirm_spend=True,
        confirm_cleanup=False,
        events_path=tmp_path / "events.jsonl",
    )

    [lane] = store.load()
    assert summary.failed_launches == 1
    assert lane.state == LaneState.FAILED_LAUNCHING


@pytest.mark.parametrize(
    ("reap_state", "expected_state"),
    [(LaneState.RUNNING, LaneState.RUNNING), (LaneState.CLEANED, LaneState.CLEANED)],
)
def test_tick_reaps_running_lanes(tmp_path: Path, reap_state: LaneState, expected_state: LaneState) -> None:
    store = make_store(
        tmp_path,
        [
            LaneRecord(
                "lane-a",
                "sweep",
                "configs/runpod/example.json",
                0,
                state=LaneState.RUNNING,
                pod_id="pod-a",
            )
        ],
    )
    reaper = FakeReaper(
        {
            "lane-a": ReapResult(
                lane_status="DONE" if reap_state == LaneState.CLEANED else "RUNNING",
                final_state=reap_state,
            )
        }
    )

    summary = tick_queue(
        store=store,
        launcher=FakeLauncher(),
        reaper=reaper,
        confirm_spend=False,
        confirm_cleanup=True,
        events_path=tmp_path / "events.jsonl",
    )

    assert store.load()[0].state == expected_state
    assert reaper.reaped == [("lane-a", True)]
    assert summary.reaped == (1 if expected_state == LaneState.CLEANED else 0)


def test_tick_persists_syncing_before_reaping_running_lane(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [
            LaneRecord(
                "lane-a",
                "sweep",
                "configs/runpod/example.json",
                0,
                state=LaneState.RUNNING,
                pod_id="pod-a",
            )
        ],
    )
    reaper = CrashAfterObservingSyncingReaper(store)

    with pytest.raises(KeyboardInterrupt):
        tick_queue(
            store=store,
            launcher=FakeLauncher(),
            reaper=reaper,
            confirm_spend=False,
            confirm_cleanup=True,
            events_path=tmp_path / "events.jsonl",
        )

    assert reaper.observed_states == [(LaneState.SYNCING, LaneState.SYNCING)]
    assert store.load()[0].state == LaneState.SYNCING


def test_tick_recovers_syncing_lane_after_mid_sync_crash(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [
            LaneRecord(
                "lane-a",
                "sweep",
                "configs/runpod/example.json",
                0,
                state=LaneState.SYNCING,
                pod_id="pod-a",
            )
        ],
    )
    reaper = FakeReaper({"lane-a": ReapResult(lane_status="RUNNING", final_state=LaneState.RUNNING)})

    summary = tick_queue(
        store=store,
        launcher=FakeLauncher(),
        reaper=reaper,
        confirm_spend=False,
        confirm_cleanup=True,
        events_path=tmp_path / "events.jsonl",
    )

    assert reaper.reaped == [("lane-a", True)]
    assert summary.still_running == 1
    assert store.load()[0].state == LaneState.RUNNING


@pytest.mark.parametrize("final_state", [LaneState.CLEANED, LaneState.FAILED_RUNNING])
def test_tick_can_finish_syncing_lane_after_recovery(
    tmp_path: Path, final_state: LaneState
) -> None:
    store = make_store(
        tmp_path,
        [
            LaneRecord(
                "lane-a",
                "sweep",
                "configs/runpod/example.json",
                0,
                state=LaneState.SYNCING,
                pod_id="pod-a",
            )
        ],
    )
    lane_status = "DONE" if final_state == LaneState.CLEANED else "FAILED"
    reaper = FakeReaper({"lane-a": ReapResult(lane_status=lane_status, final_state=final_state)})

    summary = tick_queue(
        store=store,
        launcher=FakeLauncher(),
        reaper=reaper,
        confirm_spend=False,
        confirm_cleanup=True,
        events_path=tmp_path / "events.jsonl",
    )

    assert reaper.reaped == [("lane-a", True)]
    assert summary.reaped == 1
    assert store.load()[0].state == final_state


def test_queue_is_terminal_only_when_all_lanes_terminal() -> None:
    assert not queue_is_terminal([])
    assert not queue_is_terminal(
        [
            LaneRecord("lane-a", "sweep", "configs/runpod/example.json", 0, state=LaneState.CLEANED),
            LaneRecord("lane-b", "sweep", "configs/runpod/example.json", 1, state=LaneState.RUNNING),
        ]
    )
    assert queue_is_terminal(
        [
            LaneRecord("lane-a", "sweep", "configs/runpod/example.json", 0, state=LaneState.CLEANED),
            LaneRecord(
                "lane-b",
                "sweep",
                "configs/runpod/example.json",
                1,
                state=LaneState.FAILED_RUNNING,
            ),
        ]
    )


def test_archive_promotion_requires_spend_confirmation_to_launch_sync_pod(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [
            LaneRecord(
                "lane-a",
                "sweep",
                "configs/runpod/example.json",
                0,
                state=LaneState.CLEANED,
            )
        ],
    )

    with pytest.raises(launcher.ConfigError, match="archive sync pod is billable"):
        maybe_promote_complete_queue(
            store=store,
            archive_root=tmp_path / "archive",
            events_path=tmp_path / "events.jsonl",
            aggregate_result={"sweeps": []},
            promote_to_archive=True,
            remote_subdir="demo/run",
            sync_pod_id=None,
            sync_spec=tmp_path / "archive-sync-pod.json",
            confirm_sync=True,
            confirm_spend=False,
            confirm_delete_sync_pod=False,
        )


def test_script_reaper_cleanup_does_not_delete_volumes_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)

        class Completed:
            stdout = '{"lane_status": "DONE", "dry_run": false}'

        return Completed()

    monkeypatch.setattr("runpod_research.controller.subprocess.run", fake_run)

    reaper = ScriptReaper(repo_root=Path("."))
    reaper.reap(
        LaneRecord(
            "lane-a",
            "sweep",
            "configs/runpod/example.json",
            0,
            state=LaneState.RUNNING,
            pod_id="pod-a",
        ),
        confirm_cleanup=True,
    )

    command = commands[0]
    assert "--confirm-stop" in command
    assert "--confirm-delete-pod" in command
    assert "--confirm-delete-volume" not in command
    assert "--delete-success-volume" not in command


def test_script_reaper_forces_unreachable_cleanup_after_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)

        class Completed:
            stdout = '{"lane_status": "FAILED", "dry_run": false}'

        return Completed()

    monkeypatch.setattr("runpod_research.controller.subprocess.run", fake_run)

    reaper = ScriptReaper(repo_root=Path("."), unreachable_grace_seconds=1.0)
    reaper.reap(
        LaneRecord(
            "lane-a",
            "sweep",
            "configs/runpod/example.json",
            0,
            state=LaneState.RUNNING,
            pod_id="pod-a",
            launched_at_utc="2000-01-01T00:00:00Z",
        ),
        confirm_cleanup=True,
    )

    assert "--force-cleanup-unreachable" in commands[0]


def test_script_reaper_can_explicitly_delete_success_temp_volumes(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)

        class Completed:
            stdout = '{"lane_status": "DONE", "dry_run": false}'

        return Completed()

    monkeypatch.setattr("runpod_research.controller.subprocess.run", fake_run)

    reaper = ScriptReaper(repo_root=Path("."), confirm_delete_volume=True)
    reaper.reap(
        LaneRecord(
            "lane-a",
            "sweep",
            "configs/runpod/example.json",
            0,
            state=LaneState.RUNNING,
            pod_id="pod-a",
        ),
        confirm_cleanup=True,
    )

    command = commands[0]
    assert "--delete-success-volume" in command
    assert "--confirm-delete-volume" in command


def test_script_reaper_can_include_checkpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)

        class Completed:
            stdout = '{"lane_status": "DONE", "dry_run": false}'

        return Completed()

    monkeypatch.setattr("runpod_research.controller.subprocess.run", fake_run)

    reaper = ScriptReaper(repo_root=Path("."), include_checkpoints=True)
    reaper.reap(
        LaneRecord(
            "lane-a",
            "sweep",
            "configs/runpod/example.json",
            0,
            state=LaneState.RUNNING,
            pod_id="pod-a",
        ),
        confirm_cleanup=True,
    )

    assert "--include-checkpoints" in commands[0]
