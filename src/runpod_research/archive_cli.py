#!/usr/bin/env python3
"""Promote local RunPod artifacts to a configured archive volume."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any

from runpod_research import data_path
from runpod_research import launcher as runpod
from runpod_research.archive_sync import (
    remote_verify_command,
    upload_path_via_rsync,
    validate_remote_subdir,
    write_archive_manifest,
)
from runpod_research.lifecycle import endpoint_from_pod, run_ssh, utc_now


DEFAULT_REMOTE_ROOT = "/workspace/archives/runpod-research"
DEFAULT_SYNC_SPEC = data_path("archive-sync-pod.json")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-path", type=Path, action="append", required=True)
    parser.add_argument("--remote-subdir", required=True)
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--sync-pod-id")
    parser.add_argument("--launch-sync-pod", action="store_true")
    parser.add_argument("--sync-spec", type=Path, default=DEFAULT_SYNC_SPEC)
    parser.add_argument("--manifest-out-root", type=Path, default=Path("build/runpod-archive-sync"))
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--ssh-key", type=Path, default=Path.home() / ".ssh" / "id_ed25519")
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--confirm-spend", action="store_true")
    parser.add_argument("--confirm-sync", action="store_true")
    parser.add_argument("--confirm-stop", action="store_true")
    parser.add_argument("--confirm-delete-pod", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def call_api(
    runpod: Any,
    method: str,
    path: str,
    *,
    api_key: str,
    api_base: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    return runpod.api_request(method, path, api_key=api_key, api_base=api_base, payload=payload)


def launch_sync_pod(
    *,
    runpod: Any,
    spec_path: Path,
    api_key: str,
    api_base: str,
    confirm_spend: bool,
) -> tuple[str, Path]:
    if not confirm_spend:
        raise runpod.ConfigError("launching an archive sync pod is billable; pass --confirm-spend")
    spec = runpod.load_spec(spec_path)
    payloads, _ = runpod.build_payloads(spec, allow_unresolved=False)
    payloads = runpod.limit_payloads(payloads, 1)
    item = payloads[0]
    response = call_api(runpod, "POST", "/pods", api_key=api_key, api_base=api_base, payload=item["payload"])
    pod_id = runpod.pod_id_from_response(response)
    out_dir = Path("build/runpod-launch-manifests") / f"{runpod.utc_timestamp()}_{spec_path.stem}_archive_sync"
    manifest_path = out_dir / "launch_manifest.json"
    runpod.write_json(
        manifest_path,
        {
            "schema_version": 1,
            "action": "archive-sync",
            "created_at_utc": runpod.utc_timestamp(),
            "spec_path": str(spec_path),
            "api_base": api_base,
            "jobs": [
                {
                    "job_name": item["job_name"],
                    "pod_name": item["pod_name"],
                    "pod_id": pod_id,
                    "payload": runpod.redact_for_manifest(item["payload"]),
                    "response": runpod.redact_for_manifest(response),
                }
            ],
        },
    )
    return pod_id, manifest_path


def wait_for_endpoint(
    *,
    runpod: Any,
    pod_id: str,
    api_key: str,
    api_base: str,
    ssh_key: Path,
    timeout_seconds: float,
    poll_seconds: float,
):
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        pod = call_api(
            runpod,
            "GET",
            f"/pods/{pod_id}?includeMachine=true&includeNetworkVolume=true&includeTemplate=true",
            api_key=api_key,
            api_base=api_base,
        )
        try:
            endpoint = endpoint_from_pod(pod)
            run_ssh(endpoint, ssh_key, "true")
            return endpoint
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(poll_seconds)
    raise TimeoutError(f"sync pod {pod_id} did not expose SSH in time: {last_error}")


def cleanup_sync_pod(
    *,
    runpod: Any,
    pod_id: str,
    api_key: str,
    api_base: str,
    confirm_stop: bool,
    confirm_delete_pod: bool,
) -> list[Any]:
    actions = []
    if confirm_stop:
        actions.append(call_api(runpod, "POST", f"/pods/{pod_id}/stop", api_key=api_key, api_base=api_base))
    if confirm_delete_pod:
        actions.append(call_api(runpod, "DELETE", f"/pods/{pod_id}", api_key=api_key, api_base=api_base))
    return actions


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if bool(args.sync_pod_id) == bool(args.launch_sync_pod):
        raise SystemExit("provide exactly one of --sync-pod-id or --launch-sync-pod")
    if not args.confirm_sync and not args.dry_run:
        raise SystemExit("uploading to the archive requires --confirm-sync or --dry-run")
    if args.dry_run and args.launch_sync_pod:
        raise SystemExit("dry-run cannot launch a sync pod; pass --sync-pod-id for a dry upload plan")
    remote_subdir = validate_remote_subdir(args.remote_subdir)
    runpod.load_optional_dotenv(args.env_file)
    runpod.ensure_default_public_key()
    api_key = runpod.api_key_from_env()
    api_base = args.api_base or os.environ.get("RUNPOD_API_BASE", runpod.DEFAULT_API_BASE)

    pod_id = args.sync_pod_id
    launch_manifest = None
    if args.launch_sync_pod:
        pod_id, launch_manifest = launch_sync_pod(
            runpod=runpod,
            spec_path=args.sync_spec,
            api_key=api_key,
            api_base=api_base,
            confirm_spend=args.confirm_spend,
        )
    assert pod_id is not None

    cleanup_actions = []
    try:
        endpoint = wait_for_endpoint(
            runpod=runpod,
            pod_id=pod_id,
            api_key=api_key,
            api_base=api_base,
            ssh_key=args.ssh_key,
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
        )
        manifest_path = args.manifest_out_root / f"{utc_now().replace(':', '').replace('-', '')}_archive_sync_manifest.json"
        manifest = write_archive_manifest(
            manifest_path,
            local_paths=args.local_path,
            remote_subdir=remote_subdir,
        )

        if not args.dry_run:
            run_ssh(
                endpoint,
                args.ssh_key,
                f"mkdir -p {shlex.quote(args.remote_root.rstrip('/') + '/' + remote_subdir)}",
            )
        uploaded = []
        for local_path in args.local_path:
            upload_path_via_rsync(
                endpoint=endpoint,
                ssh_key=args.ssh_key,
                local_path=local_path,
                remote_root=args.remote_root,
                remote_subdir=remote_subdir,
                dry_run=args.dry_run,
            )
            uploaded.append(str(local_path))
        upload_path_via_rsync(
            endpoint=endpoint,
            ssh_key=args.ssh_key,
            local_path=manifest_path,
            remote_root=args.remote_root,
            remote_subdir=remote_subdir,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            run_ssh(
                endpoint,
                args.ssh_key,
                remote_verify_command(
                    remote_root=args.remote_root,
                    remote_subdir=remote_subdir,
                    manifest_name=manifest_path.name,
                ),
            )
    finally:
        if args.confirm_stop or args.confirm_delete_pod:
            pending_exception = sys.exc_info()[0] is not None
            try:
                cleanup_actions = cleanup_sync_pod(
                    runpod=runpod,
                    pod_id=pod_id,
                    api_key=api_key,
                    api_base=api_base,
                    confirm_stop=args.confirm_stop,
                    confirm_delete_pod=args.confirm_delete_pod,
                )
            except Exception as exc:  # noqa: BLE001
                if pending_exception:
                    print(f"sync pod cleanup failed for {pod_id}: {exc}", file=sys.stderr)
                else:
                    raise

    print(
        json.dumps(
            {
                "created_at_utc": utc_now(),
                "sync_pod_id": pod_id,
                "launch_manifest": str(launch_manifest) if launch_manifest else None,
                "remote_root": args.remote_root,
                "remote_subdir": remote_subdir,
                "manifest_path": str(manifest_path),
                "file_count": len(manifest["files"]),
                "uploaded": uploaded,
                "cleanup_actions": cleanup_actions,
                "dry_run": args.dry_run,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
