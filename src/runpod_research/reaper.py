#!/usr/bin/env python3
"""One-shot RunPod lifecycle reaper for a single launched pod."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from runpod_research import launcher as runpod
from runpod_research.lifecycle import (
    ArtifactVerificationError,
    LifecycleError,
    PodEndpoint,
    append_event,
    archive_destination,
    decision_for_status,
    discover_remote_run_root,
    endpoint_from_pod,
    pull_artifacts,
    read_remote_status,
    require_launch_manifest_entry,
    utc_now,
    verify_required_artifacts,
    volume_delete_blocker,
    write_checksums,
    write_json,
    write_status_cache,
)


def load_runpod_module() -> Any:
    return runpod


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod-id", required=True)
    parser.add_argument("--manifest-root", type=Path, default=Path("build/runpod-launch-manifests"))
    parser.add_argument("--archive-root", type=Path, default=Path("artifacts/runpod-lifecycle/sweeps"))
    parser.add_argument("--status-cache", type=Path, default=Path("build/runpod-monitor/status-cache"))
    parser.add_argument("--events-path", type=Path, default=Path("build/runpod-lifecycle/events.jsonl"))
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--ssh-key", type=Path, default=Path.home() / ".ssh" / "id_ed25519")
    parser.add_argument("--ssh-user", default="root")
    parser.add_argument("--ssh-host")
    parser.add_argument("--ssh-port", type=int)
    parser.add_argument("--collector-pod-id")
    parser.add_argument("--remote-run-root")
    parser.add_argument("--include-checkpoints", action="store_true")
    parser.add_argument("--delete-success-volume", action="store_true")
    parser.add_argument("--delete-failed-volume", action="store_true")
    parser.add_argument(
        "--force-cleanup-unreachable",
        action="store_true",
        help=(
            "If SSH endpoint or remote status discovery fails, write a failed "
            "receipt and apply stop/delete pod actions instead of failing the reaper."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-stop", action="store_true")
    parser.add_argument("--confirm-delete-pod", action="store_true")
    parser.add_argument("--confirm-delete-volume", action="store_true")
    parser.add_argument("--confirm-stop-collector", action="store_true")
    parser.add_argument("--confirm-delete-collector", action="store_true")
    return parser.parse_args(argv)


def call_api(runpod: Any, method: str, path: str, *, api_key: str, api_base: str) -> Any:
    return runpod.api_request(method, path, api_key=api_key, api_base=api_base)


def maybe_call_api(
    runpod: Any,
    method: str,
    path: str,
    *,
    api_key: str,
    api_base: str,
    enabled: bool,
    dry_run: bool,
) -> dict[str, Any]:
    if dry_run or not enabled:
        return {"action": f"{method} {path}", "executed": False, "dry_run": dry_run}
    response = call_api(runpod, method, path, api_key=api_key, api_base=api_base)
    redact = getattr(runpod, "redact_for_manifest", lambda value: value)
    return {"action": f"{method} {path}", "executed": True, "response": redact(response)}


def endpoint_from_args_or_pod(args: argparse.Namespace, pod: dict[str, Any]) -> PodEndpoint:
    if args.ssh_host or args.ssh_port:
        if not args.ssh_host or not args.ssh_port:
            raise LifecycleError("--ssh-host and --ssh-port must be provided together")
        return PodEndpoint(host=args.ssh_host, port=args.ssh_port, user=args.ssh_user)
    return endpoint_from_pod(pod, user=args.ssh_user)


def force_cleanup_unreachable(
    *,
    args: argparse.Namespace,
    runpod: Any,
    api_key: str,
    api_base: str,
    entry: Any,
    reason: str,
) -> int:
    stamp = "unreachable-" + utc_now().replace("-", "").replace(":", "")
    remote_run_root = f"<unreachable:{args.pod_id}>"
    local_archive = args.archive_root / entry.sweep_name / entry.job_name / stamp
    local_archive.mkdir(parents=True, exist_ok=True)
    status = {
        "status": "FAILED",
        "updated_at": utc_now(),
        "message": reason,
        "run_root": remote_run_root,
    }
    if not args.dry_run:
        write_json(local_archive / "status.json", status)
    cache_path = write_status_cache(
        args.status_cache,
        entry=entry,
        status=status,
        local_archive=local_archive,
        remote_run_root=remote_run_root,
    )
    actions = [
        maybe_call_api(
            runpod,
            "POST",
            f"/pods/{args.pod_id}/stop",
            api_key=api_key,
            api_base=api_base,
            enabled=args.confirm_stop,
            dry_run=args.dry_run,
        ),
        maybe_call_api(
            runpod,
            "DELETE",
            f"/pods/{args.pod_id}",
            api_key=api_key,
            api_base=api_base,
            enabled=args.confirm_delete_pod,
            dry_run=args.dry_run,
        ),
    ]
    checksums = write_checksums(local_archive) if not args.dry_run else {}
    receipt = {
        "created_at": utc_now(),
        "pod_id": args.pod_id,
        "pod_name": entry.pod_name,
        "job_name": entry.job_name,
        "sweep_name": entry.sweep_name,
        "remote_run_root": remote_run_root,
        "artifact_endpoint_pod_id": args.pod_id,
        "collector_pod_id": None,
        "local_archive": str(local_archive),
        "status_cache": str(cache_path),
        "lane_status": "FAILED",
        "decision": {
            "lane_status": "FAILED",
            "sync_artifacts": False,
            "stop_pod": True,
            "delete_pod": True,
            "delete_volume": False,
            "reason": reason,
        },
        "actions": actions,
        "checksums": checksums,
        "dry_run": args.dry_run,
        "forced_unreachable_cleanup": True,
    }
    receipt_path = local_archive / "archive-receipt.json"
    if not args.dry_run:
        write_json(receipt_path, receipt)
    append_event(args.events_path, "reap.finish", **receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runpod = load_runpod_module()
    runpod.load_optional_dotenv(args.env_file)
    api_key = runpod.api_key_from_env()
    api_base = args.api_base or os.environ.get("RUNPOD_API_BASE", runpod.DEFAULT_API_BASE)

    entry = require_launch_manifest_entry(args.manifest_root, args.pod_id)
    append_event(
        args.events_path,
        "reap.start",
        pod_id=args.pod_id,
        job_name=entry.job_name,
        sweep_name=entry.sweep_name,
        dry_run=args.dry_run,
    )

    pod = call_api(
        runpod,
        "GET",
        f"/pods/{args.pod_id}?includeMachine=true&includeNetworkVolume=true&includeTemplate=true",
        api_key=api_key,
        api_base=api_base,
    )
    if not isinstance(pod, dict):
        raise LifecycleError("RunPod pod response was not an object")

    try:
        if args.collector_pod_id:
            collector_pod = call_api(
                runpod,
                "GET",
                f"/pods/{args.collector_pod_id}?includeMachine=true&includeNetworkVolume=true&includeTemplate=true",
                api_key=api_key,
                api_base=api_base,
            )
            if not isinstance(collector_pod, dict):
                raise LifecycleError("RunPod collector pod response was not an object")
            artifact_endpoint = endpoint_from_pod(collector_pod, user=args.ssh_user)
        else:
            artifact_endpoint = endpoint_from_args_or_pod(args, pod)
        artifact_root = entry.artifact_root
        if not artifact_root and not args.remote_run_root:
            raise LifecycleError(
                "launch manifest did not contain RPR_ARTIFACT_ROOT; pass --remote-run-root"
            )

        remote_run_root = args.remote_run_root or discover_remote_run_root(
            endpoint=artifact_endpoint,
            ssh_key=args.ssh_key,
            artifact_root=str(artifact_root),
            sweep_name=entry.sweep_name,
            job_name=entry.job_name,
        )
        status = read_remote_status(
            endpoint=artifact_endpoint,
            ssh_key=args.ssh_key,
            remote_run_root=remote_run_root,
        )
    except Exception as exc:  # noqa: BLE001 - force cleanup is an operator-confirmed fallback.
        if not args.force_cleanup_unreachable:
            raise
        return force_cleanup_unreachable(
            args=args,
            runpod=runpod,
            api_key=api_key,
            api_base=api_base,
            entry=entry,
            reason=f"forced unreachable cleanup after reaper discovery failed: {exc}",
        )
    local_archive = archive_destination(args.archive_root, entry=entry, remote_run_root=remote_run_root)
    decision = decision_for_status(
        str(status.get("status", "")),
        has_volume=bool(entry.volume_id),
        delete_success_volume=args.delete_success_volume,
        delete_failed_volume=args.delete_failed_volume,
    )

    if decision.sync_artifacts:
        pull_artifacts(
            endpoint=artifact_endpoint,
            ssh_key=args.ssh_key,
            remote_run_root=remote_run_root,
            destination=local_archive,
            include_checkpoints=args.include_checkpoints,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            if decision.lane_status == "DONE":
                verify_required_artifacts(local_archive)
            checksums = write_checksums(local_archive)
        else:
            checksums = {}
    else:
        local_archive.mkdir(parents=True, exist_ok=True)
        checksums = {}

    cache_path = write_status_cache(
        args.status_cache,
        entry=entry,
        status=status,
        local_archive=local_archive,
        remote_run_root=remote_run_root,
    )

    actions: list[dict[str, Any]] = []
    if decision.stop_pod:
        actions.append(
            maybe_call_api(
                runpod,
                "POST",
                f"/pods/{args.pod_id}/stop",
                api_key=api_key,
                api_base=api_base,
                enabled=args.confirm_stop,
                dry_run=args.dry_run,
            )
        )
    if decision.delete_pod:
        actions.append(
            maybe_call_api(
                runpod,
                "DELETE",
                f"/pods/{args.pod_id}",
                api_key=api_key,
                api_base=api_base,
                enabled=args.confirm_delete_pod,
                dry_run=args.dry_run,
            )
        )
    if args.collector_pod_id and args.confirm_stop_collector:
        actions.append(
            maybe_call_api(
                runpod,
                "POST",
                f"/pods/{args.collector_pod_id}/stop",
                api_key=api_key,
                api_base=api_base,
                enabled=args.confirm_stop_collector,
                dry_run=args.dry_run,
            )
        )
    if args.collector_pod_id and args.confirm_delete_collector:
        actions.append(
            maybe_call_api(
                runpod,
                "DELETE",
                f"/pods/{args.collector_pod_id}",
                api_key=api_key,
                api_base=api_base,
                enabled=args.confirm_delete_collector,
                dry_run=args.dry_run,
            )
        )
    if decision.delete_volume and entry.volume_id:
        blocker = volume_delete_blocker(
            volume_id=entry.volume_id,
            protected_volume_id=os.environ.get("RUNPOD_NETWORK_VOLUME_ID", "").strip() or None,
            collector_pod_id=args.collector_pod_id,
            confirm_delete_collector=args.confirm_delete_collector,
        )
        if blocker:
            actions.append(
                {
                    "action": f"DELETE /networkvolumes/{entry.volume_id}",
                    "executed": False,
                    "dry_run": args.dry_run,
                    "blocked": blocker,
                }
            )
        else:
            actions.append(
                maybe_call_api(
                    runpod,
                    "DELETE",
                    f"/networkvolumes/{entry.volume_id}",
                    api_key=api_key,
                    api_base=api_base,
                    enabled=args.confirm_delete_volume,
                    dry_run=args.dry_run,
                )
            )

    receipt = {
        "created_at": utc_now(),
        "pod_id": args.pod_id,
        "pod_name": entry.pod_name,
        "job_name": entry.job_name,
        "sweep_name": entry.sweep_name,
        "remote_run_root": remote_run_root,
        "artifact_endpoint_pod_id": args.collector_pod_id or args.pod_id,
        "collector_pod_id": args.collector_pod_id,
        "local_archive": str(local_archive),
        "status_cache": str(cache_path),
        "lane_status": decision.lane_status,
        "decision": decision.__dict__,
        "actions": actions,
        "checksums": checksums,
        "dry_run": args.dry_run,
    }
    receipt_path = local_archive / "archive-receipt.json"
    if not args.dry_run:
        write_json(receipt_path, receipt)
    append_event(args.events_path, "reap.finish", **receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ArtifactVerificationError as exc:
        raise SystemExit(f"artifact verification failed: {exc}") from exc
    except LifecycleError as exc:
        raise SystemExit(f"lifecycle failed: {exc}") from exc
