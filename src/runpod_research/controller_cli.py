#!/usr/bin/env python3
"""Minimal RunPod lifecycle queue controller."""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from runpod_research import data_path

ROOT = Path.cwd()

from runpod_research import launcher as runpod  # noqa: E402
from runpod_research.aggregate import aggregate_sweep_results  # noqa: E402
from runpod_research.controller import (  # noqa: E402
    CapacityUnavailable,
    LaunchResult,
    ScriptReaper,
    tick_queue,
)
from runpod_research.daemon import (  # noqa: E402
    build_loop_argv,
    daemon_metadata,
    pid_running,
    stop_daemon_metadata,
)
from runpod_research.lifecycle import append_event  # noqa: E402
from runpod_research.queue import QueueStore, initialize_lanes, queue_is_terminal  # noqa: E402


DEFAULT_QUEUE = Path("build/runpod-queues/default/queue.json")
DEFAULT_EVENTS = Path("build/runpod-lifecycle/events.jsonl")
DEFAULT_MANIFEST_ROOT = Path("build/runpod-launch-manifests")
DEFAULT_ARCHIVE_ROOT = Path("artifacts/runpod-lifecycle/sweeps")
DEFAULT_ARCHIVE_SYNC_SPEC = data_path("archive-sync-pod.json")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


class RunPodLauncher:
    def __init__(
        self,
        *,
        api_key: str,
        api_base: str,
        out_root: Path = DEFAULT_MANIFEST_ROOT,
    ):
        self.api_key = api_key
        self.api_base = api_base
        self.out_root = out_root

    def launch(self, lane) -> LaunchResult:
        spec_path = Path(lane.spec_path)
        spec = runpod.load_spec(spec_path)
        try:
            job = spec["jobs"][lane.job_index]
        except IndexError as exc:
            raise runpod.ConfigError(f"{spec_path}: no job at index {lane.job_index}") from exc

        single_spec = copy.deepcopy(spec)
        single_spec["jobs"] = [job]
        payloads, _ = runpod.build_payloads(single_spec, allow_unresolved=False)
        if len(payloads) != 1:
            raise runpod.ConfigError("expected exactly one payload")
        payload_item = payloads[0]

        try:
            response = runpod.api_request(
                "POST",
                "/pods",
                api_key=self.api_key,
                api_base=self.api_base,
                payload=payload_item["payload"],
            )
        except RuntimeError as exc:
            if _looks_like_capacity_error(str(exc)):
                raise CapacityUnavailable(str(exc)) from exc
            raise

        pod_id = runpod.pod_id_from_response(response)
        out_dir = (
            self.out_root
            / f"{runpod.utc_timestamp()}_{spec_path.stem}_{payload_item['job_name']}_tick_launch"
        )
        record = {
            "job_name": payload_item["job_name"],
            "pod_name": payload_item["pod_name"],
            "pod_id": pod_id,
            "payload": runpod.redact_for_manifest(payload_item["payload"]),
            "response": runpod.redact_for_manifest(response),
        }
        manifest_path = out_dir / "launch_manifest.json"
        write_json(
            manifest_path,
            {
                "schema_version": 1,
                "action": "lifecycle-tick-launch",
                "created_at_utc": runpod.utc_timestamp(),
                "spec_path": str(spec_path),
                "api_base": self.api_base,
                "jobs": [record],
            },
        )
        return LaunchResult(
            pod_id=pod_id,
            pod_name=str(payload_item["pod_name"]),
            cost_per_hr=float(_response_value(response, "costPerHr") or 0.0),
            volume_id=_response_value(response, "networkVolumeId"),
            data_center_id=_response_value(response, "dataCenterId"),
            manifest_path=str(manifest_path),
        )


def _response_value(response: Any, key: str) -> Any:
    if not isinstance(response, dict):
        return None
    if key in response:
        return response[key]
    for nested_key in ("pod", "machine", "networkVolume"):
        nested = response.get(nested_key)
        if isinstance(nested, dict) and key in nested:
            return nested[key]
    return None


def _looks_like_capacity_error(message: str) -> bool:
    lowered = message.lower()
    return any(token in lowered for token in ("capacity", "unavailable", "no instances", "out of stock"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--api-base", default=runpod.DEFAULT_API_BASE)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-queue", help="create a queue from a sweep spec")
    init_parser.add_argument("--spec", type=Path, required=True)
    init_parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    init_parser.add_argument("--start-index", type=int, default=0)
    init_parser.add_argument("--count", type=int)
    init_parser.set_defaults(func=init_queue)

    tick_parser = subparsers.add_parser("tick", help="run one queue controller tick")
    tick_parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    tick_parser.add_argument("--events-path", type=Path, default=DEFAULT_EVENTS)
    tick_parser.add_argument("--manifest-root", type=Path, default=DEFAULT_MANIFEST_ROOT)
    tick_parser.add_argument("--confirm-spend", action="store_true")
    tick_parser.add_argument("--confirm-cleanup", action="store_true")
    tick_parser.add_argument("--confirm-delete-temp-volumes", action="store_true")
    tick_parser.add_argument("--include-checkpoints", action="store_true")
    tick_parser.add_argument("--unreachable-grace-seconds", type=float)
    tick_parser.add_argument("--aggregate-on-complete", action="store_true")
    tick_parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT)
    add_promotion_args(tick_parser)
    tick_parser.set_defaults(func=tick)

    loop_parser = subparsers.add_parser("loop", help="run queue ticks until completion")
    loop_parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    loop_parser.add_argument("--events-path", type=Path, default=DEFAULT_EVENTS)
    loop_parser.add_argument("--manifest-root", type=Path, default=DEFAULT_MANIFEST_ROOT)
    loop_parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT)
    loop_parser.add_argument("--interval-seconds", type=float, default=60.0)
    loop_parser.add_argument("--max-ticks", type=int)
    loop_parser.add_argument("--confirm-spend", action="store_true")
    loop_parser.add_argument("--confirm-cleanup", action="store_true")
    loop_parser.add_argument("--confirm-delete-temp-volumes", action="store_true")
    loop_parser.add_argument("--include-checkpoints", action="store_true")
    loop_parser.add_argument("--unreachable-grace-seconds", type=float)
    loop_parser.add_argument("--no-aggregate-on-complete", action="store_true")
    add_promotion_args(loop_parser)
    loop_parser.set_defaults(func=loop)

    daemon_start_parser = subparsers.add_parser("daemon-start", help="start loop in background")
    daemon_start_parser.add_argument("--name", required=True)
    daemon_start_parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    daemon_start_parser.add_argument("--events-path", type=Path, default=DEFAULT_EVENTS)
    daemon_start_parser.add_argument("--manifest-root", type=Path, default=DEFAULT_MANIFEST_ROOT)
    daemon_start_parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT)
    daemon_start_parser.add_argument("--interval-seconds", type=float, default=60.0)
    daemon_start_parser.add_argument("--max-ticks", type=int)
    daemon_start_parser.add_argument("--confirm-spend", action="store_true")
    daemon_start_parser.add_argument("--confirm-cleanup", action="store_true")
    daemon_start_parser.add_argument("--confirm-delete-temp-volumes", action="store_true")
    daemon_start_parser.add_argument("--include-checkpoints", action="store_true")
    daemon_start_parser.add_argument("--unreachable-grace-seconds", type=float)
    add_promotion_args(daemon_start_parser)
    daemon_start_parser.set_defaults(func=daemon_start)

    daemon_status_parser = subparsers.add_parser("daemon-status", help="show background loop status")
    daemon_status_parser.add_argument("--name", required=True)
    daemon_status_parser.set_defaults(func=daemon_status)

    daemon_stop_parser = subparsers.add_parser("daemon-stop", help="stop background loop with SIGINT")
    daemon_stop_parser.add_argument("--name", required=True)
    daemon_stop_parser.add_argument("--timeout-seconds", type=float, default=10.0)
    daemon_stop_parser.set_defaults(func=daemon_stop)

    list_parser = subparsers.add_parser("list", help="print queue state")
    list_parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    list_parser.set_defaults(func=list_queue)
    return parser.parse_args()


def add_promotion_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--promote-to-archive", action="store_true")
    parser.add_argument("--archive-remote-subdir")
    parser.add_argument("--archive-sync-pod-id")
    parser.add_argument("--archive-sync-spec", type=Path, default=DEFAULT_ARCHIVE_SYNC_SPEC)
    parser.add_argument("--confirm-archive-sync", action="store_true")
    parser.add_argument(
        "--confirm-sync",
        dest="confirm_archive_sync",
        action="store_true",
        help="Alias for --confirm-archive-sync.",
    )
    parser.add_argument("--confirm-delete-sync-pod", action="store_true")


def init_queue(args: argparse.Namespace) -> int:
    runpod.load_optional_dotenv(args.env_file)
    runpod.ensure_default_public_key()
    spec = runpod.load_spec(args.spec)
    jobs = spec["jobs"][args.start_index :]
    if args.count is not None:
        jobs = jobs[: args.count]
    if not jobs:
        raise runpod.ConfigError("selected zero jobs")
    lanes = initialize_lanes(
        spec_path=args.spec,
        sweep_name=runpod.slugify(str(spec["name"])),
        lane_names=[runpod.slugify(str(job.get("name", f"job-{index}"))) for index, job in enumerate(jobs)],
        start_index=args.start_index,
    )
    QueueStore(args.queue).save(lanes)
    print(args.queue)
    return 0


def tick(args: argparse.Namespace) -> int:
    validate_cleanup_flags(args)
    runpod.load_optional_dotenv(args.env_file)
    runpod.ensure_default_public_key()
    api_key = runpod.api_key_from_env() if args.confirm_spend else ""
    store = QueueStore(args.queue)
    summary = run_tick(
        store=store,
        api_key=api_key,
        api_base=args.api_base,
        manifest_root=args.manifest_root,
        archive_root=args.archive_root,
        events_path=args.events_path,
        confirm_spend=args.confirm_spend,
        confirm_cleanup=args.confirm_cleanup,
        confirm_delete_temp_volumes=args.confirm_delete_temp_volumes,
        include_checkpoints=args.include_checkpoints,
        unreachable_grace_seconds=args.unreachable_grace_seconds,
    )
    aggregate_result = maybe_aggregate_complete_queue(
        store=store,
        archive_root=args.archive_root,
        events_path=args.events_path,
        enabled=args.aggregate_on_complete,
    )
    promotion_result = maybe_promote_complete_queue(
        store=store,
        archive_root=args.archive_root,
        events_path=args.events_path,
        aggregate_result=aggregate_result,
        promote_to_archive=args.promote_to_archive,
        remote_subdir=args.archive_remote_subdir,
        sync_pod_id=args.archive_sync_pod_id,
        sync_spec=args.archive_sync_spec,
        confirm_sync=args.confirm_archive_sync,
        confirm_spend=args.confirm_spend,
        confirm_delete_sync_pod=args.confirm_delete_sync_pod,
    )
    output = {"summary": summary.to_dict(), "aggregate": aggregate_result, "promotion": promotion_result}
    print(json.dumps(output, indent=2, sort_keys=True))
    return 1 if summary.errors else 0


def loop(args: argparse.Namespace) -> int:
    validate_cleanup_flags(args)
    runpod.load_optional_dotenv(args.env_file)
    runpod.ensure_default_public_key()
    if not args.confirm_spend and args.max_ticks is None:
        raise runpod.ConfigError("dry loop without --confirm-spend requires --max-ticks")
    api_key = runpod.api_key_from_env() if args.confirm_spend else ""
    store = QueueStore(args.queue)
    tick_index = 0
    append_event(args.events_path, "loop.start", queue=str(args.queue))
    while True:
        tick_index += 1
        summary = run_tick(
            store=store,
            api_key=api_key,
            api_base=args.api_base,
            manifest_root=args.manifest_root,
            archive_root=args.archive_root,
            events_path=args.events_path,
            confirm_spend=args.confirm_spend,
            confirm_cleanup=args.confirm_cleanup,
            confirm_delete_temp_volumes=args.confirm_delete_temp_volumes,
            include_checkpoints=args.include_checkpoints,
            unreachable_grace_seconds=args.unreachable_grace_seconds,
        )
        aggregate_result = maybe_aggregate_complete_queue(
            store=store,
            archive_root=args.archive_root,
            events_path=args.events_path,
            enabled=not args.no_aggregate_on_complete,
        )
        promotion_result = maybe_promote_complete_queue(
            store=store,
            archive_root=args.archive_root,
            events_path=args.events_path,
            aggregate_result=aggregate_result,
            promote_to_archive=args.promote_to_archive,
            remote_subdir=args.archive_remote_subdir,
            sync_pod_id=args.archive_sync_pod_id,
            sync_spec=args.archive_sync_spec,
            confirm_sync=args.confirm_archive_sync,
            confirm_spend=args.confirm_spend,
            confirm_delete_sync_pod=args.confirm_delete_sync_pod,
        )
        print(
            json.dumps(
                {
                    "tick_index": tick_index,
                    "summary": summary.to_dict(),
                    "aggregate": aggregate_result,
                    "promotion": promotion_result,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        if aggregate_result is not None or queue_is_terminal(store.load()):
            append_event(args.events_path, "loop.complete", tick_index=tick_index)
            return 0 if not summary.errors else 1
        if args.max_ticks is not None and tick_index >= args.max_ticks:
            append_event(args.events_path, "loop.max_ticks", tick_index=tick_index)
            return 0 if not summary.errors else 1
        time.sleep(args.interval_seconds)


def run_tick(
    *,
    store: QueueStore,
    api_key: str,
    api_base: str,
    manifest_root: Path,
    archive_root: Path,
    events_path: Path,
    confirm_spend: bool,
    confirm_cleanup: bool,
    confirm_delete_temp_volumes: bool,
    include_checkpoints: bool,
    unreachable_grace_seconds: float | None,
):
    return tick_queue(
        store=store,
        launcher=RunPodLauncher(api_key=api_key, api_base=api_base, out_root=manifest_root),
        reaper=ScriptReaper(
            repo_root=ROOT,
            manifest_root=manifest_root,
            archive_root=archive_root,
            events_path=events_path,
            confirm_delete_volume=confirm_delete_temp_volumes,
            include_checkpoints=include_checkpoints,
            unreachable_grace_seconds=unreachable_grace_seconds,
        ),
        confirm_spend=confirm_spend,
        confirm_cleanup=confirm_cleanup,
        events_path=events_path,
    )


def validate_cleanup_flags(args: argparse.Namespace) -> None:
    if getattr(args, "confirm_delete_temp_volumes", False) and not getattr(args, "confirm_cleanup", False):
        raise runpod.ConfigError("--confirm-delete-temp-volumes requires --confirm-cleanup")


def maybe_aggregate_complete_queue(
    *,
    store: QueueStore,
    archive_root: Path,
    events_path: Path,
    enabled: bool,
) -> dict[str, Any] | None:
    if not enabled:
        return None
    lanes = store.load()
    if not queue_is_terminal(lanes):
        return None
    sweep_names = sorted({lane.sweep_name for lane in lanes})
    results = []
    for sweep_name in sweep_names:
        result = aggregate_sweep_results(archive_root=archive_root, sweep_name=sweep_name)
        payload = {
            "sweep_name": sweep_name,
            "output_csv": str(result.output_csv),
            "issues_csv": str(result.issues_csv),
            "manifest_json": str(result.manifest_json),
            "archive_count": result.archive_count,
            "metric_row_count": result.metric_row_count,
            "issue_count": result.issue_count,
        }
        append_event(events_path, "aggregate.complete", **payload)
        results.append(payload)
    return {"sweeps": results}


def maybe_promote_complete_queue(
    *,
    store: QueueStore,
    archive_root: Path,
    events_path: Path,
    aggregate_result: dict[str, Any] | None,
    promote_to_archive: bool,
    remote_subdir: str | None,
    sync_pod_id: str | None,
    sync_spec: Path,
    confirm_sync: bool,
    confirm_spend: bool,
    confirm_delete_sync_pod: bool,
) -> dict[str, Any] | None:
    if not promote_to_archive:
        return None
    if aggregate_result is None:
        return None
    if not remote_subdir:
        raise runpod.ConfigError("--archive-remote-subdir is required with --promote-to-archive")
    lanes = store.load()
    sweep_names = sorted({lane.sweep_name for lane in lanes})
    local_paths = [archive_root / sweep_name for sweep_name in sweep_names]
    local_paths.append(store.path)
    # Raw lifecycle event logs can include verbose API/event payloads from old
    # controller runs. Keep them local; promote archives, aggregates, and queue
    # state instead.
    command = [
        sys.executable,
        "-m",
        "runpod_research.archive_cli",
        "--remote-subdir",
        remote_subdir,
    ]
    for local_path in local_paths:
        command.extend(["--local-path", str(local_path)])
    if sync_pod_id:
        command.extend(["--sync-pod-id", sync_pod_id])
    else:
        if confirm_sync and not confirm_spend:
            raise runpod.ConfigError(
                "launching an archive sync pod is billable; pass controller --confirm-spend "
                "or provide --archive-sync-pod-id"
            )
        command.extend(["--launch-sync-pod", "--sync-spec", str(sync_spec)])
    if confirm_sync:
        command.append("--confirm-sync")
        if not sync_pod_id and confirm_spend:
            command.append("--confirm-spend")
    if confirm_delete_sync_pod:
        command.extend(["--confirm-stop", "--confirm-delete-pod"])

    if not confirm_sync:
        payload = {"would_promote": True, "command": command}
        append_event(events_path, "archive.promote.dry_run", **payload)
        return payload

    completed = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = {"stdout": completed.stdout}
    append_event(events_path, "archive.promote.complete", remote_subdir=remote_subdir, details=payload)
    return payload


def list_queue(args: argparse.Namespace) -> int:
    lanes = QueueStore(args.queue).load()
    print(json.dumps([lane.to_dict() for lane in lanes], indent=2, sort_keys=True))
    return 0


def daemon_start(args: argparse.Namespace) -> int:
    validate_cleanup_flags(args)
    meta = daemon_metadata(args.name)
    if meta.pid_file.exists():
        pid = int(meta.pid_file.read_text().strip())
        if pid_running(pid):
            raise runpod.ConfigError(f"daemon {args.name!r} already running with pid {pid}")
    argv = build_loop_argv(
        script_path=None,
        env_file=args.env_file,
        api_base=args.api_base,
        queue=args.queue,
        events_path=args.events_path,
        interval_seconds=args.interval_seconds,
        max_ticks=args.max_ticks,
        confirm_spend=args.confirm_spend,
        confirm_cleanup=args.confirm_cleanup,
        confirm_delete_temp_volumes=args.confirm_delete_temp_volumes,
        include_checkpoints=args.include_checkpoints,
        unreachable_grace_seconds=args.unreachable_grace_seconds,
        promote_to_archive=args.promote_to_archive,
        archive_remote_subdir=args.archive_remote_subdir,
    )
    if args.confirm_archive_sync:
        argv.append("--confirm-archive-sync")
    if args.confirm_delete_sync_pod:
        argv.append("--confirm-delete-sync-pod")
    if args.archive_sync_pod_id:
        argv.extend(["--archive-sync-pod-id", args.archive_sync_pod_id])
    argv.extend(["--manifest-root", str(args.manifest_root)])
    argv.extend(["--archive-root", str(args.archive_root)])
    argv.extend(["--archive-sync-spec", str(args.archive_sync_spec)])

    meta.pid_file.parent.mkdir(parents=True, exist_ok=True)
    log_handle = meta.log_file.open("a")
    try:
        process = subprocess.Popen(
            argv,
            cwd=ROOT,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_handle.close()
    meta.pid_file.write_text(str(process.pid) + "\n")
    write_json(
        meta.metadata_file,
        {
            "name": meta.name,
            "pid": process.pid,
            "argv": argv,
            "queue": str(args.queue),
            "events_path": str(args.events_path),
            "log_file": str(meta.log_file),
            "started_at_utc": runpod.utc_timestamp(),
        },
    )
    print(json.dumps({"pid": process.pid, "log_file": str(meta.log_file)}, indent=2))
    return 0


def daemon_status(args: argparse.Namespace) -> int:
    meta = daemon_metadata(args.name)
    if not meta.pid_file.exists():
        print(json.dumps({"name": meta.name, "running": False, "reason": "no pid file"}, indent=2))
        return 1
    pid = int(meta.pid_file.read_text().strip())
    running = pid_running(pid)
    payload = {"name": meta.name, "pid": pid, "running": running, "log_file": str(meta.log_file)}
    if meta.metadata_file.exists():
        payload["metadata"] = json.loads(meta.metadata_file.read_text())
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if running else 1


def daemon_stop(args: argparse.Namespace) -> int:
    meta = daemon_metadata(args.name)
    if not meta.pid_file.exists():
        print(json.dumps({"name": meta.name, "stopped": False, "reason": "no pid file"}, indent=2))
        return 1
    result = stop_daemon_metadata(meta, timeout_seconds=args.timeout_seconds)
    print(
        json.dumps(
            {
                "name": meta.name,
                "pid": result.pid,
                "stopped": result.stopped,
                "was_running": result.was_running,
                "killed": result.killed,
                "timed_out": result.timed_out,
                "signal_sent": result.signal_sent,
            },
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is not None:
        original_argv = sys.argv
        sys.argv = [original_argv[0], *argv]
        try:
            args = parse_args()
        finally:
            sys.argv = original_argv
    else:
        args = parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
