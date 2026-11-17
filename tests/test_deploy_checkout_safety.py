import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def bash_path():
    git = shutil.which("git")
    if os.name == "nt" and git:
        candidate = Path(git).resolve().parents[1] / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    result = shutil.which("bash")
    if result is None:
        pytest.skip("Bash unavailable")
    return result


def git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize("script", ["deploy.sh", "update.sh"])
@pytest.mark.parametrize("state", ["clean", "dirty", "staged", "diverged"])
def test_update_keeps_writing_branch_and_never_discards_changes(tmp_path, script, state):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", str(remote), str(checkout))
    git(checkout, "config", "user.name", "test")
    git(checkout, "config", "user.email", "test@example.invalid")
    git(checkout, "switch", "-c", "ai-writing")
    source = checkout / "code.txt"
    source.write_text("base\n", encoding="utf-8")
    git(checkout, "add", "code.txt")
    git(checkout, "commit", "-m", "base")
    git(checkout, "push", "-u", "origin", "ai-writing")
    base = git(checkout, "rev-parse", "HEAD")
    source.write_text("remote\n", encoding="utf-8")
    git(checkout, "commit", "-am", "remote update")
    git(checkout, "push", "origin", "ai-writing")
    upstream = git(checkout, "rev-parse", "HEAD")
    # Reset only this test-created disposable repository to model an old installation.
    git(checkout, "reset", "--hard", base)
    if state != "clean":
        source.write_text("local work\n", encoding="utf-8")
        if state == "staged":
            git(checkout, "add", "code.txt")
        elif state == "diverged":
            git(checkout, "commit", "-am", "local work")
    previous = git(checkout, "rev-parse", "HEAD")
    text = (ROOT / script).read_text(encoding="utf-8")
    start = text.index('UPDATE_BRANCH="$(git symbolic-ref')
    end_token = 'git merge --ff-only "origin/$UPDATE_BRANCH"'
    block = text[start:text.index(end_token, start) + len(end_token)]
    result = subprocess.run(
        [bash_path(), "--noprofile", "--norc", "-e", "-c", block], cwd=checkout,
        env={**os.environ, "BACKUP_DIR": tmp_path.as_posix()}, capture_output=True, text=True,
        encoding="utf-8", timeout=20,
    )
    assert git(checkout, "branch", "--show-current") == "ai-writing"
    if state == "clean":
        assert result.returncode == 0, result.stderr
        assert git(checkout, "rev-parse", "HEAD") == upstream
        assert source.read_text() == "remote\n"
    else:
        assert result.returncode != 0
        assert git(checkout, "rev-parse", "HEAD") == previous
        assert source.read_text() == "local work\n"
        if state == "staged":
            assert git(checkout, "diff", "--cached", "--name-only") == "code.txt"


def test_failed_health_check_reports_failure_and_keeps_backup(tmp_path):
    text = (ROOT / "update.sh").read_text(encoding="utf-8")
    block = text[text.index("HEALTHY=false"):]
    sentinel = tmp_path / "previous-commit.txt"
    sentinel.write_text("old-commit", encoding="utf-8")
    result = subprocess.run(
        [bash_path(), "--noprofile", "--norc", "-e", "-c",
         'sleep() { :; }; sudo() { :; }; curl() { return 22; };\n' + block],
        env={**os.environ, "BACKUP_DIR": tmp_path.as_posix(), "SERVICE_NAME": "test",
             "FLASK_PORT": "1", "RED": "", "GREEN": "", "YELLOW": "", "NC": ""},
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    assert result.returncode == 1
    assert sentinel.read_text() == "old-commit"
