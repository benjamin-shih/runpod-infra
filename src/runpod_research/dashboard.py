#!/usr/bin/env python3
"""Generic local dashboard/status view for RunPod research controller state."""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from runpod_research import launcher as runpod


DEFAULT_MANIFEST_ROOT = Path("build/runpod-launch-manifests")
DEFAULT_STATUS_CACHE = Path("build/runpod-monitor/status-cache")


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def iter_manifest_jobs(manifest_root: Path) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    if not manifest_root.exists():
        return jobs
    for path in sorted(manifest_root.rglob("*.json")):
        if path.name not in {"launch_manifest.json", "manifest.json", "lifecycle_manifest.json"}:
            continue
        payload = safe_read_json(path)
        if not isinstance(payload, dict):
            continue
        for job in payload.get("jobs", []):
            if not isinstance(job, dict):
                continue
            item = dict(job)
            item["manifest_path"] = str(path)
            item["action"] = payload.get("action")
            item["created_at_utc"] = payload.get("created_at_utc")
            jobs.append(item)
    return jobs


def iter_status_cache(status_cache: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not status_cache.exists():
        return records
    for path in sorted(status_cache.rglob("*.json")):
        payload = safe_read_json(path)
        if isinstance(payload, dict):
            item = dict(payload)
            item["status_cache_path"] = str(path)
            records.append(item)
    return records


def summarize_pods(api_base: str, *, allow_api: bool) -> tuple[list[dict[str, Any]], str | None]:
    if not allow_api:
        return [], None
    api_key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not api_key:
        return [], "RUNPOD_API_KEY not set; API pod list skipped"
    try:
        payload = runpod.api_request("GET", "/pods", api_key=api_key, api_base=api_base)
    except Exception as exc:  # noqa: BLE001
        return [], f"RunPod API pod list failed: {exc}"
    if isinstance(payload, list):
        pods = payload
    elif isinstance(payload, dict):
        raw = payload.get("pods") or payload.get("data") or payload.get("items") or []
        pods = raw if isinstance(raw, list) else []
    else:
        pods = []
    summaries = []
    for pod in pods:
        if not isinstance(pod, dict):
            continue
        summaries.append(
            {
                "id": pod.get("id"),
                "name": pod.get("name"),
                "desiredStatus": pod.get("desiredStatus"),
                "gpuTypeId": pod.get("gpuTypeId") or _nested_get(pod, "machine", "gpuTypeId"),
                "costPerHr": pod.get("costPerHr"),
                "networkVolumeId": pod.get("networkVolumeId"),
                "createdAt": pod.get("createdAt"),
                "lastStartedAt": pod.get("lastStartedAt"),
            }
        )
    return summaries, None


def _nested_get(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def build_status(
    *,
    manifest_root: Path,
    status_cache: Path,
    api_base: str,
    allow_api: bool,
) -> dict[str, Any]:
    pods, api_warning = summarize_pods(api_base, allow_api=allow_api)
    jobs = iter_manifest_jobs(manifest_root)
    status_records = iter_status_cache(status_cache)
    terminal_statuses = {"DONE", "FAILED", "ERROR", "CANCELED", "CANCELLED"}
    cached_terminal = 0
    for record in status_records:
        lane_status = record.get("lane_status") or _nested_get(record, "status", "status") or record.get("status")
        if str(lane_status).upper() in terminal_statuses:
            cached_terminal += 1
    status = {
        "generated_at_utc": utc_now(),
        "manifest_root": str(manifest_root),
        "status_cache": str(status_cache),
        "counts": {
            "manifest_jobs": len(jobs),
            "status_cache_records": len(status_records),
            "terminal_status_cache_records": cached_terminal,
            "api_pods": len(pods),
        },
        "api_warning": api_warning,
        "manifest_jobs": jobs[-100:],
        "status_cache_records": status_records[-100:],
        "api_pods": pods,
    }
    return runpod.redact_for_manifest(status)


def render_html(status: dict[str, Any]) -> str:
    redacted_status = runpod.redact_for_manifest(status)
    body = html.escape(json.dumps(redacted_status, indent=2, sort_keys=True))
    generated_at = html.escape(str(redacted_status["generated_at_utc"]))
    cards = "".join(
        '<div class="card"><strong>{}</strong><br>{}</div>'.format(
            html.escape(str(key)),
            html.escape(str(value)),
        )
        for key, value in redacted_status["counts"].items()
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>runpod-research dashboard</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 2rem; background: #f8fafc; color: #0f172a; }}
    pre {{ background: #0f172a; color: #e2e8f0; padding: 1rem; border-radius: 0.75rem; overflow-x: auto; }}
    .counts {{ display: flex; gap: 1rem; flex-wrap: wrap; }}
    .card {{ background: white; border: 1px solid #e2e8f0; border-radius: 0.75rem; padding: 1rem; min-width: 12rem; }}
  </style>
</head>
<body>
  <h1>runpod-research dashboard</h1>
  <p>Generated at {generated_at}.</p>
  <div class="counts">
    {cards}
  </div>
  <h2>Raw status JSON</h2>
  <pre>{body}</pre>
</body>
</html>
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-root", type=Path, default=DEFAULT_MANIFEST_ROOT)
    parser.add_argument("--status-cache", type=Path, default=DEFAULT_STATUS_CACHE)
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--offline", action="store_true", help="Do not call the RunPod API.")
    parser.add_argument("--once", action="store_true", help="Print one JSON snapshot and exit.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--refresh-seconds", type=float, default=15.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runpod.load_optional_dotenv(args.env_file)
    api_base = args.api_base or os.environ.get("RUNPOD_API_BASE", runpod.DEFAULT_API_BASE)
    allow_api = not args.offline
    if args.once:
        print(
            json.dumps(
                build_status(
                    manifest_root=args.manifest_root,
                    status_cache=args.status_cache,
                    api_base=api_base,
                    allow_api=allow_api,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            status = build_status(
                manifest_root=args.manifest_root,
                status_cache=args.status_cache,
                api_base=api_base,
                allow_api=allow_api,
            )
            if self.path == "/json":
                data = json.dumps(status, indent=2, sort_keys=True).encode()
                content_type = "application/json; charset=utf-8"
            else:
                data = render_html(status).encode()
                content_type = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", f"max-age={max(1, int(args.refresh_seconds))}")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            print(f"dashboard: {format % args}", file=sys.stderr)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"serving http://{args.host}:{args.port} (Ctrl-C to stop)")
    try:
        server.serve_forever(poll_interval=min(max(args.refresh_seconds, 1.0), 60.0))
    except KeyboardInterrupt:
        print("stopping dashboard")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
