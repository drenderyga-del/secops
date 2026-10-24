import os
import subprocess
from pathlib import Path

import pytest

from scripts.validate_local_url import validate_loopback_http_url


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "value, expected",
    [
        ("http://localhost:18081", "http://localhost:18081"),
        ("http://127.0.0.1:18081/", "http://127.0.0.1:18081"),
        ("http://127.42.0.7:18081", "http://127.42.0.7:18081"),
        ("http://[::1]:18081", "http://[::1]:18081"),
    ],
)
def test_loopback_url_guard_accepts_only_explicit_local_targets(value, expected):
    assert validate_loopback_http_url(value, name="QA_BASE_URL") == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:18081",
        "file:///tmp/socket",
        "http://db.corp.example:18081",
        "http://localhost.evil.example:18081",
        "http://127.0.0.1.evil.example:18081",
        "http://2130706433:18081",
        "http://0x7f000001:18081",
        "http://user:secret@localhost:18081",
        "http://localhost:18081/path",
        "http://localhost:18081?next=http://corp.example",
        "http://localhost:18081/#fragment",
    ],
)
def test_loopback_url_guard_rejects_external_deceptive_or_credentialed_targets(value):
    with pytest.raises(ValueError, match="loopback") as error:
        validate_loopback_http_url(value, name="QA_BASE_URL")
    assert "secret" not in str(error.value)
    assert "corp.example" not in str(error.value)


@pytest.mark.parametrize(
    ("script", "environment_name"),
    [
        ("scripts/run_seeded_browser_gate.sh", "QA_BASE_URL"),
        ("scripts/run_seeded_browser_gate.sh", "QA_MOCK_URL"),
        ("scripts/run_browser_qa.sh", "QA_BASE_URL"),
    ],
)
def test_shell_runners_reject_remote_targets_before_curl_docker_or_browser(
    tmp_path, script, environment_name
):
    marker = tmp_path / "external-command-ran"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for command in ("curl", "docker", "npm", "npx"):
        executable = fake_bin / command
        executable.write_text(
            f"#!/bin/sh\nprintf '%s' {command} >> '{marker}'\nexit 97\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        environment_name: "http://user:secret@db.corp.example:18081",
    }
    result = subprocess.run(
        [str(ROOT / script)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not marker.exists()
    assert "secret" not in result.stderr
    assert "db.corp.example" not in result.stderr
