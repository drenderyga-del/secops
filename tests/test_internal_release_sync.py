import shutil
import subprocess
from pathlib import Path

import pytest


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=True)


def git(*args: str, cwd: Path) -> None:
    run("git", *args, cwd=cwd)


@pytest.fixture
def release_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    if not shutil.which("rsync"):
        pytest.skip("rsync is required by the release-export script")

    remote = tmp_path / "source.git"
    source = tmp_path / "source"
    internal = tmp_path / "internal"
    run("git", "init", "--bare", str(remote))
    run("git", "clone", str(remote), str(source))
    git("config", "user.email", "test@example.com", cwd=source)
    git("config", "user.name", "Test", cwd=source)
    (source / "scripts").mkdir()
    for filename in ("export_internal.sh", "internal-export.exclude", "sync_internal_release.sh"):
        shutil.copy2(Path("scripts") / filename, source / "scripts" / filename)
    (source / "app.py").write_text("RELEASE = 'v1.2.3'\n", encoding="utf-8")
    (source / ".gitlab-ci.yml").write_text("source-ci\n", encoding="utf-8")
    (source / ".env").write_text("SOURCE_SECRET=never-export\n", encoding="utf-8")
    git("add", ".", cwd=source)
    git("commit", "-m", "release source", cwd=source)
    git("branch", "-M", "main", cwd=source)
    git("tag", "v1.2.3", cwd=source)
    git("push", "origin", "main", "--tags", cwd=source)

    run("git", "init", str(internal))
    git("config", "user.email", "test@example.com", cwd=internal)
    git("config", "user.name", "Test", cwd=internal)
    (internal / ".gitlab-ci.yml").write_text("internal-ci\n", encoding="utf-8")
    (internal / ".env").write_text("INTERNAL_SECRET=keep\n", encoding="utf-8")
    (internal / "internal-only.txt").write_text("keep\n", encoding="utf-8")
    git("add", ".", cwd=internal)
    git("commit", "-m", "internal baseline", cwd=internal)
    return remote, source, internal


def test_sync_exports_exact_tag_and_preserves_internal_files(release_fixture: tuple[Path, Path, Path]):
    _remote, source, internal = release_fixture

    result = run(
        "bash",
        str(source / "scripts" / "sync_internal_release.sh"),
        "--remote",
        "origin",
        str(internal),
        "v1.2.3",
        cwd=source,
    )

    assert (internal / "app.py").read_text(encoding="utf-8") == "RELEASE = 'v1.2.3'\n"
    assert (internal / ".gitlab-ci.yml").read_text(encoding="utf-8") == "internal-ci\n"
    assert (internal / ".env").read_text(encoding="utf-8") == "INTERNAL_SECRET=keep\n"
    assert (internal / "internal-only.txt").read_text(encoding="utf-8") == "keep\n"
    assert "Exported release v1.2.3" in result.stdout
    assert "git -C" in result.stdout
    assert "commit -m" in result.stdout


def test_sync_refuses_dirty_internal_repository(release_fixture: tuple[Path, Path, Path]):
    _remote, source, internal = release_fixture
    (internal / "internal-only.txt").write_text("modified\n", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            str(source / "scripts" / "sync_internal_release.sh"),
            "--remote",
            "origin",
            str(internal),
            "v1.2.3",
        ],
        cwd=source,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "Destination has uncommitted changes" in result.stderr
