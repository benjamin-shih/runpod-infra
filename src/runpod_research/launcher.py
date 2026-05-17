#!/usr/bin/env python3
"""Render and launch RunPod sweep pod payloads.

The launcher is intentionally small and dependency-free. It keeps secrets out of
git by requiring API keys and account-specific IDs through environment
variables, and it makes billable actions explicit through confirmation flags.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_API_BASE = "https://rest.runpod.io/v1"
ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
SECRET_KEY_PATTERN = re.compile(r"(TOKEN|SECRET|PASSWORD|PRIVATE_KEY|API_KEY)", re.IGNORECASE)


class ConfigError(ValueError):
    """Raised when the sweep spec or environment is not launchable."""


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def print_redacted_json(payload: Any) -> None:
    print(json.dumps(redact_for_manifest(payload), indent=2, sort_keys=True))


def redact_for_manifest(value: Any, key: str | None = None) -> Any:
    """Redact secrets before writing local launch/debug manifests.

    Redaction is key-based when possible and value-based for expanded secret
    environment variables. The value pass matters for list/string fields such
    as docker commands, where `${RUNPOD_API_KEY}` can be expanded before the
    manifest writer sees any key context.
    """

    secret_values = _secret_env_values()
    return _redact_for_manifest(value, key=key, secret_values=secret_values)


def _redact_for_manifest(
    value: Any,
    *,
    key: str | None,
    secret_values: tuple[str, ...],
) -> Any:
    if key and key != "PUBLIC_KEY" and SECRET_KEY_PATTERN.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {
            item_key: _redact_for_manifest(
                item_value,
                key=str(item_key),
                secret_values=secret_values,
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [
            _redact_for_manifest(item, key=None, secret_values=secret_values) for item in value
        ]
    if isinstance(value, str):
        redacted = value
        for secret in secret_values:
            redacted = redacted.replace(secret, "<redacted>")
        return redacted
    return value


def _secret_env_values() -> tuple[str, ...]:
    values = {
        env_value
        for env_key, env_value in os.environ.items()
        if env_value and len(env_value) >= 4 and SECRET_KEY_PATTERN.search(env_key)
    }
    return tuple(sorted(values, key=len, reverse=True))


def load_dotenv(path: Path) -> list[str]:
    """Load simple KEY=VALUE entries from a local dotenv-style file.

    Existing environment variables win, which lets operators override local
    files from the shell. The parser is intentionally small but accepts spaces
    around '='.
    """
    if not path.exists():
        return []

    loaded: list[str] = []
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        if "=" not in line:
            raise ConfigError(f"{path}:{line_number} is not a KEY=VALUE line")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigError(f"{path}:{line_number} has invalid env key {key!r}")
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def load_optional_dotenv(path: str | Path | None) -> list[str]:
    if path is None:
        return []
    return load_dotenv(Path(path))


def ensure_default_public_key() -> bool:
    """Populate RUNPOD_PUBLIC_KEY from the default SSH public key if absent.

    RunPod workers need a public key for SSH access, but operators often keep
    only secret/account values in `.env`. The public key is not a secret, so it
    is safe to derive it from the standard local path when available.
    """
    if os.environ.get("RUNPOD_PUBLIC_KEY", "").strip():
        return False
    default_key = Path.home() / ".ssh" / "id_ed25519.pub"
    if not default_key.exists():
        return False
    public_key = default_key.read_text().strip()
    if not public_key:
        return False
    os.environ["RUNPOD_PUBLIC_KEY"] = public_key
    return True


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip("-")
    if not slug:
        raise ConfigError("empty name cannot be slugified")
    return slug


def expand_env(value: Any, unresolved: set[str]) -> Any:
    if isinstance(value, dict):
        return {key: expand_env(item, unresolved) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env(item, unresolved) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        env_name = match.group(1)
        if not env_name.startswith("RUNPOD_"):
            return match.group(0)
        env_value = os.environ.get(env_name)
        if env_value is None:
            unresolved.add(env_name)
            return match.group(0)
        return env_value

    return ENV_PATTERN.sub(replace, value)


LIFECYCLE_ONLY_KEYS = {"storage_mode", "artifact_contract", "controller"}


def strip_lifecycle_keys(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove controller-only spec metadata before POSTing to RunPod."""

    return {key: value for key, value in payload.items() if key not in LIFECYCLE_ONLY_KEYS}


def load_spec(path: Path) -> dict[str, Any]:
    spec = read_json(path)
    if spec.get("schema_version") != 1:
        raise ConfigError("expected schema_version=1")
    if not isinstance(spec.get("jobs"), list) or not spec["jobs"]:
        raise ConfigError("spec must contain a non-empty jobs list")
    if not isinstance(spec.get("defaults"), dict):
        raise ConfigError("spec must contain a defaults object")
    return spec


def build_payloads(
    spec: dict[str, Any],
    *,
    allow_unresolved: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    spec_name = slugify(str(spec["name"]))
    root = spec.get("remote_artifact_root")
    if not isinstance(root, str) or not root.startswith("/"):
        raise ConfigError("remote_artifact_root must be an absolute remote path")

    payloads: list[dict[str, Any]] = []
    all_unresolved: set[str] = set()
    for index, job in enumerate(spec["jobs"], start=1):
        if not isinstance(job, dict):
            raise ConfigError(f"job {index} must be an object")
        job_name = slugify(str(job.get("name", f"job-{index}")))
        job_overrides = {key: value for key, value in job.items() if key != "name"}
        payload = strip_lifecycle_keys(deep_merge(spec["defaults"], job_overrides))

        merged_env = deep_merge(spec["defaults"].get("env", {}), job.get("env", {}))
        merged_env.update(
            {
                "RPR_SWEEP_NAME": spec_name,
                "RPR_JOB_NAME": job_name,
                "RPR_ARTIFACT_ROOT": root,
                "RPR_LAUNCH_CREATED_AT_UTC": datetime.now(timezone.utc).isoformat(),
            }
        )
        payload["env"] = merged_env
        payload["name"] = f"{spec_name}-{job_name}"

        unresolved: set[str] = set()
        payload = expand_env(payload, unresolved)
        all_unresolved.update(unresolved)
        payloads.append(
            {
                "job_name": job_name,
                "pod_name": payload["name"],
                "payload": payload,
                "unresolved_env": sorted(unresolved),
            }
        )

    if all_unresolved and not allow_unresolved:
        missing = ", ".join(sorted(all_unresolved))
        raise ConfigError(f"unresolved environment variable(s): {missing}")
    return payloads, sorted(all_unresolved)


def api_request(
    method: str,
    path: str,
    *,
    api_key: str,
    api_base: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    url = f"{api_base.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"RunPod API {method} {url} failed: {exc.code} {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"RunPod API {method} {url} failed: {exc.reason}") from exc
    return json.loads(raw) if raw else {}


def api_key_from_env() -> str:
    api_key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not api_key:
        raise ConfigError("RUNPOD_API_KEY is required for API actions")
    return api_key


def is_gpu_status(status: dict[str, Any]) -> bool:
    pod = status.get("pod")
    if isinstance(pod, dict):
        status = pod
    machine = status.get("machine")
    if not isinstance(machine, dict):
        return False
    gpu_type = str(machine.get("gpuTypeId", "")).strip().lower()
    return bool(gpu_type and gpu_type != "unknown")


def pod_id_from_response(response: Any) -> str:
    if isinstance(response, dict):
        for key in ("id", "podId"):
            value = response.get(key)
            if isinstance(value, str) and value:
                return value
        pod = response.get("pod")
        if isinstance(pod, dict):
            return pod_id_from_response(pod)
    raise ConfigError("RunPod response did not include a pod id")


def limit_payloads(payloads: list[dict[str, Any]], max_jobs: int | None) -> list[dict[str, Any]]:
    if max_jobs is None:
        return payloads
    if max_jobs < 1:
        raise ConfigError("--max-jobs must be at least 1")
    selected = payloads[:max_jobs]
    if not selected:
        raise ConfigError("selected zero jobs")
    return selected


def render(args: argparse.Namespace) -> int:
    spec_path = Path(args.spec)
    out_dir = Path(args.out_dir) / f"{utc_timestamp()}_{spec_path.stem}"
    spec = load_spec(spec_path)
    payloads, unresolved = build_payloads(spec, allow_unresolved=True)

    manifest_jobs = []
    for item in payloads:
        payload_path = out_dir / "payloads" / f"{item['job_name']}.json"
        write_json(payload_path, redact_for_manifest(item["payload"]))
        manifest_jobs.append(
            {
                "job_name": item["job_name"],
                "pod_name": item["pod_name"],
                "payload_path": str(payload_path),
                "payload_redacted": True,
                "unresolved_env": item["unresolved_env"],
            }
        )

    manifest = {
        "action": "render",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "spec_path": str(spec_path),
        "api_base": args.api_base,
        "jobs": manifest_jobs,
        "unresolved_env": unresolved,
        "launch_note": (
            "Dry-run only. Set RUNPOD_API_KEY and RUNPOD_NETWORK_VOLUME_ID, "
            "then use the launch subcommand with --confirm-spend to create pods."
        ),
    }
    manifest_path = out_dir / "manifest.json"
    write_json(manifest_path, manifest)
    print(manifest_path)
    if unresolved:
        print("unresolved env:", ", ".join(unresolved), file=sys.stderr)
    return 0


def launch(args: argparse.Namespace) -> int:
    if not args.confirm_spend:
        raise ConfigError("launch is billable; rerun with --confirm-spend")
    spec_path = Path(args.spec)
    out_dir = Path(args.out_dir) / f"{utc_timestamp()}_{spec_path.stem}_launch"
    spec = load_spec(spec_path)
    payloads, _ = build_payloads(spec, allow_unresolved=False)
    payloads = limit_payloads(payloads, args.max_jobs)
    api_key = api_key_from_env()

    responses = []
    for item in payloads:
        response = api_request(
            "POST",
            "/pods",
            api_key=api_key,
            api_base=args.api_base,
            payload=item["payload"],
        )
        responses.append(
            {
                "job_name": item["job_name"],
                "pod_name": item["pod_name"],
                "payload": redact_for_manifest(item["payload"]),
                "response": redact_for_manifest(response),
            }
        )
        write_json(out_dir / "responses" / f"{item['job_name']}.json", responses[-1])

    manifest_path = out_dir / "launch_manifest.json"
    write_json(
        manifest_path,
        {
            "schema_version": 1,
            "action": "launch",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "spec_path": str(spec_path),
            "api_base": args.api_base,
            "jobs": responses,
        },
    )
    print(manifest_path)
    return 0


def list_resources(args: argparse.Namespace) -> int:
    api_key = api_key_from_env()
    paths = {
        "pods": "/pods",
        "network-volumes": "/networkvolumes",
        "templates": "/templates?includeRunpodTemplates=true",
    }
    path = paths[args.resource]
    response = api_request("GET", path, api_key=api_key, api_base=args.api_base)
    print_redacted_json(response)
    return 0


def create_network_volume(args: argparse.Namespace) -> int:
    if not args.confirm_spend:
        raise ConfigError("creating a network volume is billable; rerun with --confirm-spend")
    api_key = api_key_from_env()
    payload = {
        "name": args.name,
        "size": args.size_gb,
        "dataCenterId": args.data_center_id,
    }
    response = api_request(
        "POST",
        "/networkvolumes",
        api_key=api_key,
        api_base=args.api_base,
        payload=payload,
    )
    print_redacted_json(response)
    return 0


def delete_network_volume(args: argparse.Namespace) -> int:
    if not args.confirm_delete:
        raise ConfigError("deleting a network volume is destructive; rerun with --confirm-delete")
    api_key = api_key_from_env()
    response = api_request(
        "DELETE",
        f"/networkvolumes/{args.network_volume_id}",
        api_key=api_key,
        api_base=args.api_base,
    )
    print_redacted_json(response)
    return 0


def get_resource(args: argparse.Namespace) -> int:
    api_key = api_key_from_env()
    if args.resource == "pod":
        path = (
            f"/pods/{args.resource_id}"
            "?includeMachine=true&includeNetworkVolume=true&includeTemplate=true"
        )
    elif args.resource == "template":
        path = f"/templates/{args.resource_id}?includeRunpodTemplates=true"
    else:
        path = f"/networkvolumes/{args.resource_id}"
    response = api_request("GET", path, api_key=api_key, api_base=args.api_base)
    print_redacted_json(response)
    return 0


def stop(args: argparse.Namespace) -> int:
    if not args.confirm_stop:
        raise ConfigError("stopping a pod changes cloud state; rerun with --confirm-stop")
    api_key = api_key_from_env()
    response = api_request(
        "POST",
        f"/pods/{args.pod_id}/stop",
        api_key=api_key,
        api_base=args.api_base,
    )
    print_redacted_json(response)
    return 0


def start(args: argparse.Namespace) -> int:
    if not args.confirm_spend:
        raise ConfigError("starting a pod is billable; rerun with --confirm-spend")
    api_key = api_key_from_env()
    response = api_request(
        "POST",
        f"/pods/{args.pod_id}/start",
        api_key=api_key,
        api_base=args.api_base,
    )
    print_redacted_json(response)
    return 0


def delete(args: argparse.Namespace) -> int:
    if not args.confirm_delete:
        raise ConfigError("deleting a pod is destructive; rerun with --confirm-delete")
    api_key = api_key_from_env()
    response = api_request(
        "DELETE",
        f"/pods/{args.pod_id}",
        api_key=api_key,
        api_base=args.api_base,
    )
    print_redacted_json(response)
    return 0


def lifecycle_smoke(args: argparse.Namespace) -> int:
    if not args.confirm_spend:
        raise ConfigError("lifecycle-smoke creates billable pods; rerun with --confirm-spend")
    if not args.confirm_delete:
        raise ConfigError("lifecycle-smoke deletes pods; rerun with --confirm-delete")

    spec_path = Path(args.spec)
    out_dir = Path(args.out_dir) / f"{utc_timestamp()}_{spec_path.stem}_lifecycle"
    spec = load_spec(spec_path)
    payloads, _ = build_payloads(spec, allow_unresolved=False)
    payloads = limit_payloads(payloads, args.max_jobs)
    api_key = api_key_from_env()

    launched: list[dict[str, Any]] = []
    primary_error: Exception | None = None
    try:
        for item in payloads:
            response = api_request(
                "POST",
                "/pods",
                api_key=api_key,
                api_base=args.api_base,
                payload=item["payload"],
            )
            pod_id = pod_id_from_response(response)
            record = {
                "job_name": item["job_name"],
                "pod_name": item["pod_name"],
                "pod_id": pod_id,
                "payload": redact_for_manifest(item["payload"]),
                "launch_response": redact_for_manifest(response),
            }
            launched.append(record)
            write_json(out_dir / "launch" / f"{item['job_name']}.json", record)
            print(f"launched {item['job_name']} pod_id={pod_id}")

        if args.settle_seconds:
            time.sleep(args.settle_seconds)

        for record in launched:
            pod_id = record["pod_id"]
            status = api_request(
                "GET",
                f"/pods/{pod_id}?includeMachine=true&includeNetworkVolume=true",
                api_key=api_key,
                api_base=args.api_base,
            )
            record["pre_stop_status"] = redact_for_manifest(status)
            write_json(out_dir / "status" / f"{record['job_name']}.json", record["pre_stop_status"])
            print(f"observed {record['job_name']} status={status.get('desiredStatus', 'unknown')}")
            gpu_status = status if is_gpu_status(status) else record.get("launch_response", {})
            if args.require_gpu and not is_gpu_status(gpu_status):
                raise RuntimeError(
                    f"{record['job_name']} did not report a GPU machine in pod status"
                )
    except Exception as exc:
        primary_error = exc
        print(f"lifecycle error before cleanup: {exc}", file=sys.stderr)
    finally:
        for record in launched:
            if "stop_response" in record:
                continue
            pod_id = record["pod_id"]
            try:
                response = api_request(
                    "POST",
                    f"/pods/{pod_id}/stop",
                    api_key=api_key,
                    api_base=args.api_base,
                )
                record["stop_response"] = redact_for_manifest(response)
                write_json(out_dir / "stop" / f"{record['job_name']}.json", record["stop_response"])
                print(f"stopped {record['job_name']} pod_id={pod_id}")
            except Exception as exc:
                record["stop_error"] = str(exc)
                print(
                    f"stop failed for {record['job_name']} pod_id={pod_id}: {exc}", file=sys.stderr
                )

        if launched and args.post_stop_seconds:
            time.sleep(args.post_stop_seconds)

        for record in launched:
            if "delete_response" in record:
                continue
            pod_id = record["pod_id"]
            try:
                response = api_request(
                    "DELETE",
                    f"/pods/{pod_id}",
                    api_key=api_key,
                    api_base=args.api_base,
                )
                record["delete_response"] = redact_for_manifest(response)
                write_json(
                    out_dir / "delete" / f"{record['job_name']}.json",
                    record["delete_response"],
                )
                print(f"deleted {record['job_name']} pod_id={pod_id}")
            except Exception as exc:
                record["delete_error"] = str(exc)
                print(
                    f"delete failed for {record['job_name']} pod_id={pod_id}: {exc}",
                    file=sys.stderr,
                )

    manifest_path = out_dir / "lifecycle_manifest.json"
    write_json(
        manifest_path,
        {
            "action": "lifecycle-smoke",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "spec_path": str(spec_path),
            "api_base": args.api_base,
            "settle_seconds": args.settle_seconds,
            "post_stop_seconds": args.post_stop_seconds,
            "jobs": launched,
        },
    )
    print(manifest_path)
    if primary_error is not None:
        raise primary_error
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--env-file", default=None, help="optional dotenv-style file to load before commands")
    subparsers = parser.add_subparsers(dest="command", required=True)

    render_parser = subparsers.add_parser("render", help="render payloads without API calls")
    render_parser.add_argument("--spec", required=True)
    render_parser.add_argument("--out-dir", default="build/runpod-launch-manifests")
    render_parser.set_defaults(func=render)

    launch_parser = subparsers.add_parser("launch", help="create pods from a sweep spec")
    launch_parser.add_argument("--spec", required=True)
    launch_parser.add_argument("--out-dir", default="build/runpod-launch-manifests")
    launch_parser.add_argument("--max-jobs", type=int)
    launch_parser.add_argument("--confirm-spend", action="store_true")
    launch_parser.set_defaults(func=launch)

    list_parser = subparsers.add_parser("list", help="list RunPod resources")
    list_parser.add_argument("resource", choices=["pods", "network-volumes", "templates"])
    list_parser.set_defaults(func=list_resources)

    get_parser = subparsers.add_parser("get", help="get one RunPod resource")
    get_parser.add_argument("resource", choices=["pod", "network-volume", "template"])
    get_parser.add_argument("resource_id")
    get_parser.set_defaults(func=get_resource)

    create_volume_parser = subparsers.add_parser(
        "create-network-volume",
        help="create a network volume",
    )
    create_volume_parser.add_argument("--name", required=True)
    create_volume_parser.add_argument("--size-gb", type=int, required=True)
    create_volume_parser.add_argument("--data-center-id", required=True)
    create_volume_parser.add_argument("--confirm-spend", action="store_true")
    create_volume_parser.set_defaults(func=create_network_volume)

    delete_volume_parser = subparsers.add_parser(
        "delete-network-volume",
        help="delete a network volume by id",
    )
    delete_volume_parser.add_argument("--network-volume-id", required=True)
    delete_volume_parser.add_argument("--confirm-delete", action="store_true")
    delete_volume_parser.set_defaults(func=delete_network_volume)

    stop_parser = subparsers.add_parser("stop", help="stop one pod by id")
    stop_parser.add_argument("--pod-id", required=True)
    stop_parser.add_argument("--confirm-stop", action="store_true")
    stop_parser.set_defaults(func=stop)

    start_parser = subparsers.add_parser("start", help="start one stopped pod by id")
    start_parser.add_argument("--pod-id", required=True)
    start_parser.add_argument("--confirm-spend", action="store_true")
    start_parser.set_defaults(func=start)

    delete_parser = subparsers.add_parser("delete", help="delete one pod by id")
    delete_parser.add_argument("--pod-id", required=True)
    delete_parser.add_argument("--confirm-delete", action="store_true")
    delete_parser.set_defaults(func=delete)

    smoke_parser = subparsers.add_parser(
        "lifecycle-smoke",
        help="launch, observe, stop, and delete all pods in a smoke spec",
    )
    smoke_parser.add_argument("--spec", required=True)
    smoke_parser.add_argument("--out-dir", default="build/runpod-launch-manifests")
    smoke_parser.add_argument("--settle-seconds", type=int, default=45)
    smoke_parser.add_argument("--post-stop-seconds", type=int, default=15)
    smoke_parser.add_argument("--max-jobs", type=int)
    smoke_parser.add_argument("--require-gpu", action="store_true")
    smoke_parser.add_argument("--confirm-spend", action="store_true")
    smoke_parser.add_argument("--confirm-delete", action="store_true")
    smoke_parser.set_defaults(func=lifecycle_smoke)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        load_optional_dotenv(args.env_file)
        ensure_default_public_key()
        if args.api_base is None:
            args.api_base = os.environ.get("RUNPOD_API_BASE", DEFAULT_API_BASE)
        return args.func(args)
    except ConfigError as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
