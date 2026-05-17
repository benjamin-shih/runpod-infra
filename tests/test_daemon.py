from __future__ import annotations

import signal
import sys
from pathlib import Path

from runpod_research.daemon import (
    ProcessStopResult,
    build_loop_argv,
    daemon_metadata,
    stop_daemon_metadata,
    stop_daemon_process,
)


def test_build_loop_argv_preserves_queue_and_archive_flags() -> None:
    argv = build_loop_argv(
        script_path=None,
        env_file=Path(".env.runpod"),
        api_base="https://api.runpod.example",
        queue=Path("build/runpod-queues/demo/queue.json"),
        events_path=Path("build/runpod-queues/demo/events.jsonl"),
        interval_seconds=20.0,
        max_ticks=None,
        confirm_spend=True,
        confirm_cleanup=True,
        confirm_delete_temp_volumes=True,
        include_checkpoints=True,
        unreachable_grace_seconds=900.0,
        promote_to_archive=True,
        archive_remote_subdir="runpod-smoke/demo",
    )

    assert argv[:3] == [sys.executable, "-m", "runpod_research.controller_cli"]
    assert ["--env-file", ".env.runpod"] == argv[
        argv.index("--env-file") : argv.index("--env-file") + 2
    ]
    assert ["--api-base", "https://api.runpod.example"] == argv[
        argv.index("--api-base") : argv.index("--api-base") + 2
    ]
    assert "loop" in argv
    assert ["--queue", "build/runpod-queues/demo/queue.json"] == argv[
        argv.index("--queue") : argv.index("--queue") + 2
    ]
    assert "--confirm-spend" in argv
    assert "--confirm-cleanup" in argv
    assert "--confirm-delete-temp-volumes" in argv
    assert "--include-checkpoints" in argv
    assert ["--unreachable-grace-seconds", "900.0"] == argv[
        argv.index("--unreachable-grace-seconds") : argv.index("--unreachable-grace-seconds") + 2
    ]
    assert "--promote-to-archive" in argv
    assert ["--archive-remote-subdir", "runpod-smoke/demo"] == argv[
        argv.index("--archive-remote-subdir") : argv.index("--archive-remote-subdir") + 2
    ]


def test_daemon_metadata_points_under_build_root() -> None:
    meta = daemon_metadata("demo-smoke")

    assert meta.pid_file == Path("build/runpod-daemons/demo-smoke.pid")
    assert meta.log_file == Path("build/runpod-daemons/demo-smoke.log")
    assert meta.metadata_file == Path("build/runpod-daemons/demo-smoke.json")


def test_stop_daemon_process_waits_for_sigint_exit(monkeypatch) -> None:
    calls: list[int] = []
    running_checks = [True, True, False]

    def fake_kill(pid: int, sig: int) -> None:
        assert pid == 4321
        calls.append(sig)
        if sig == 0 and not running_checks.pop(0):
            raise ProcessLookupError

    monkeypatch.setattr("runpod_research.daemon.os.kill", fake_kill)
    monkeypatch.setattr("runpod_research.daemon.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("runpod_research.daemon.time.monotonic", lambda: 0.0)

    result = stop_daemon_process(4321, timeout_seconds=10.0)

    assert result == ProcessStopResult(
        pid=4321,
        was_running=True,
        stopped=True,
        killed=False,
        timed_out=False,
        signal_sent="SIGINT",
    )
    assert calls == [0, signal.SIGINT, 0, 0]


def test_stop_daemon_process_sends_sigkill_after_timeout(monkeypatch) -> None:
    calls: list[int] = []

    def fake_kill(pid: int, sig: int) -> None:
        assert pid == 4321
        calls.append(sig)

    monkeypatch.setattr("runpod_research.daemon.os.kill", fake_kill)

    result = stop_daemon_process(4321, timeout_seconds=0.0)

    assert result == ProcessStopResult(
        pid=4321,
        was_running=True,
        stopped=True,
        killed=True,
        timed_out=True,
        signal_sent="SIGKILL",
    )
    assert calls == [0, signal.SIGINT, 0, signal.SIGKILL]


def test_stop_daemon_metadata_unlinks_pid_after_wait(monkeypatch, tmp_path: Path) -> None:
    meta = daemon_metadata("demo-smoke", root=tmp_path)
    meta.pid_file.write_text("4321\n")
    observations: list[tuple[int, bool, float]] = []

    def fake_stop_daemon_process(pid: int, *, timeout_seconds: float) -> ProcessStopResult:
        observations.append((pid, meta.pid_file.exists(), timeout_seconds))
        return ProcessStopResult(
            pid=pid,
            was_running=True,
            stopped=True,
            killed=False,
            timed_out=False,
            signal_sent="SIGINT",
        )

    monkeypatch.setattr(
        "runpod_research.daemon.stop_daemon_process",
        fake_stop_daemon_process,
    )

    result = stop_daemon_metadata(meta, timeout_seconds=2.5)

    assert observations == [(4321, True, 2.5)]
    assert result.pid == 4321
    assert not meta.pid_file.exists()
