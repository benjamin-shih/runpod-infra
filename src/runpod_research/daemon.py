"""Small foreground daemon helpers for the RunPod lifecycle controller."""

from __future__ import annotations

import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DaemonMetadata:
    name: str
    pid_file: Path
    log_file: Path
    metadata_file: Path


@dataclass(frozen=True)
class ProcessStopResult:
    pid: int
    was_running: bool
    stopped: bool
    killed: bool
    timed_out: bool
    signal_sent: str | None


def daemon_metadata(name: str, *, root: Path = Path("build/runpod-daemons")) -> DaemonMetadata:
    safe_name = name.strip().replace("/", "-")
    if not safe_name:
        raise ValueError("daemon name must not be empty")
    return DaemonMetadata(
        name=safe_name,
        pid_file=root / f"{safe_name}.pid",
        log_file=root / f"{safe_name}.log",
        metadata_file=root / f"{safe_name}.json",
    )


def build_loop_argv(
    *,
    script_path: Path | None,
    queue: Path,
    events_path: Path,
    interval_seconds: float,
    max_ticks: int | None,
    confirm_spend: bool,
    confirm_cleanup: bool,
    confirm_delete_temp_volumes: bool,
    include_checkpoints: bool,
    unreachable_grace_seconds: float | None,
    max_concurrent: int | None,
    max_launches_per_tick: int | None,
    promote_to_archive: bool,
    archive_remote_subdir: str | None,
    env_file: Path | None = None,
    api_base: str | None = None,
) -> list[str]:
    if script_path is None:
        argv = [sys.executable, "-m", "runpod_research.controller_cli"]
    else:
        argv = ["uv", "run", "python", str(script_path)]
    if env_file is not None:
        argv.extend(["--env-file", str(env_file)])
    if api_base is not None:
        argv.extend(["--api-base", api_base])
    argv.extend(
        [
            "loop",
            "--queue",
            str(queue),
            "--events-path",
            str(events_path),
            "--interval-seconds",
            str(interval_seconds),
        ]
    )
    if max_ticks is not None:
        argv.extend(["--max-ticks", str(max_ticks)])
    if confirm_spend:
        argv.append("--confirm-spend")
    if confirm_cleanup:
        argv.append("--confirm-cleanup")
    if confirm_delete_temp_volumes:
        argv.append("--confirm-delete-temp-volumes")
    if include_checkpoints:
        argv.append("--include-checkpoints")
    if unreachable_grace_seconds is not None:
        argv.extend(["--unreachable-grace-seconds", str(unreachable_grace_seconds)])
    if max_concurrent is not None:
        argv.extend(["--max-concurrent", str(max_concurrent)])
    if max_launches_per_tick is not None:
        argv.extend(["--max-launches-per-tick", str(max_launches_per_tick)])
    if promote_to_archive:
        argv.append("--promote-to-archive")
        if not archive_remote_subdir:
            raise ValueError("--archive-remote-subdir is required with promotion")
        argv.extend(["--archive-remote-subdir", archive_remote_subdir])
    return argv


def pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop_daemon_process(
    pid: int,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float = 0.25,
) -> ProcessStopResult:
    if not pid_running(pid):
        return ProcessStopResult(
            pid=pid,
            was_running=False,
            stopped=True,
            killed=False,
            timed_out=False,
            signal_sent=None,
        )

    os.kill(pid, signal.SIGINT)
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while time.monotonic() < deadline:
        if not pid_running(pid):
            return ProcessStopResult(
                pid=pid,
                was_running=True,
                stopped=True,
                killed=False,
                timed_out=False,
                signal_sent="SIGINT",
            )
        remaining = max(deadline - time.monotonic(), 0.0)
        time.sleep(min(poll_interval_seconds, remaining))

    if not pid_running(pid):
        return ProcessStopResult(
            pid=pid,
            was_running=True,
            stopped=True,
            killed=False,
            timed_out=False,
            signal_sent="SIGINT",
        )

    os.kill(pid, signal.SIGKILL)
    return ProcessStopResult(
        pid=pid,
        was_running=True,
        stopped=True,
        killed=True,
        timed_out=True,
        signal_sent="SIGKILL",
    )


def stop_daemon_metadata(
    metadata: DaemonMetadata,
    *,
    timeout_seconds: float,
) -> ProcessStopResult:
    pid = int(metadata.pid_file.read_text().strip())
    result = stop_daemon_process(pid, timeout_seconds=timeout_seconds)
    metadata.pid_file.unlink(missing_ok=True)
    return result
