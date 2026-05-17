from __future__ import annotations

import json
from pathlib import Path

import pytest

from runpod_research.dashboard import build_status, render_html


def test_dashboard_redacts_and_escapes_status_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "secret-token")
    status_cache = tmp_path / "status-cache"
    status_cache.mkdir()
    (status_cache / "lane.json").write_text(
        json.dumps(
            {
                "status": "DONE",
                "RUNPOD_API_KEY": "secret-token",
                "message": "<script>alert(1)</script>",
            }
        )
    )

    status = build_status(
        manifest_root=tmp_path / "manifests",
        status_cache=status_cache,
        api_base="https://api.example",
        allow_api=False,
    )
    html = render_html(status)

    assert status["status_cache_records"][0]["RUNPOD_API_KEY"] == "<redacted>"
    assert "secret-token" not in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
