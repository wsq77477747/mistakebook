#!/usr/bin/env python3
"""Poll GitHub for a tested commit and deploy it with update_native.sh."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


REPOSITORY = os.environ.get("SQL_WRONGBOOK_GITHUB_REPOSITORY", "wsq77477747/mistakebook")
BRANCH = os.environ.get("SQL_WRONGBOOK_GITHUB_BRANCH", "main")
WORKFLOW_PATH = os.environ.get(
    "SQL_WRONGBOOK_CI_WORKFLOW_PATH", ".github/workflows/deploy.yml"
)
STATE_FILE = Path(
    os.environ.get(
        "SQL_WRONGBOOK_DEPLOY_STATE",
        "/opt/sql-wrongbook/deploy-state/last_success_sha",
    )
)
API_ROOT = "https://api.github.com"
ARCHIVE_ROOT = "https://codeload.github.com"
USER_AGENT = "sql-wrongbook-server-pull/1.0"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    print(f"[{stamp}] {message}", flush=True)


def open_url(url: str):
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return urlopen(request, timeout=30)
        except HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504}:
                break
        except URLError as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    assert last_error is not None
    raise last_error


def get_json(url: str) -> dict:
    with open_url(url) as response:
        return json.load(response)


def get_latest_sha() -> str:
    branch = quote(BRANCH, safe="")
    payload = get_json(f"{API_ROOT}/repos/{REPOSITORY}/commits/{branch}")
    sha = str(payload.get("sha", "")).lower()
    if not SHA_PATTERN.fullmatch(sha):
        raise RuntimeError("GitHub returned an invalid commit SHA")
    return sha


def get_ci_state(sha: str) -> tuple[str, str]:
    query = urlencode({"head_sha": sha, "per_page": "20"})
    payload = get_json(f"{API_ROOT}/repos/{REPOSITORY}/actions/runs?{query}")
    runs = [
        run
        for run in payload.get("workflow_runs", [])
        if run.get("head_sha") == sha and run.get("path") == WORKFLOW_PATH
    ]
    if any(
        run.get("status") == "completed" and run.get("conclusion") == "success"
        for run in runs
    ):
        return "success", "CI passed"
    if any(run.get("status") != "completed" for run in runs):
        return "pending", "CI is still running"
    if runs:
        conclusions = ", ".join(
            sorted({str(run.get("conclusion") or "unknown") for run in runs})
        )
        return "failed", f"CI completed without success: {conclusions}"
    return "missing", "No CI run exists for this commit"


def read_deployed_sha() -> str:
    try:
        return STATE_FILE.read_text(encoding="utf-8").strip().lower()
    except FileNotFoundError:
        return ""


def download_archive(sha: str, destination: Path) -> None:
    url = f"{ARCHIVE_ROOT}/{REPOSITORY}/tar.gz/{sha}"
    with open_url(url) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def extract_archive(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError(f"Unsafe archive path: {member.name}")
            if member.issym() or member.islnk():
                raise RuntimeError(f"Archive links are not allowed: {member.name}")
        bundle.extractall(destination, filter="data")

    candidates = [
        path
        for path in destination.iterdir()
        if path.is_dir() and (path / "deploy" / "update_native.sh").is_file()
    ]
    if len(candidates) != 1:
        raise RuntimeError("Could not identify the project root in the archive")
    return candidates[0]


def run_checked(command: list[str], cwd: Path) -> None:
    log(f"Running: {' '.join(command)}")
    subprocess.run(command, cwd=cwd, check=True)


def record_deployed_sha(sha: str) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_name(f".{STATE_FILE.name}.tmp")
    temporary.write_text(f"{sha}\n", encoding="utf-8")
    os.replace(temporary, STATE_FILE)


def main() -> int:
    latest_sha = get_latest_sha()
    deployed_sha = read_deployed_sha()
    log(f"Latest main commit: {latest_sha}")
    if latest_sha == deployed_sha:
        log("NO_UPDATE: the latest tested commit is already deployed")
        return 0

    ci_state, ci_message = get_ci_state(latest_sha)
    if ci_state != "success":
        log(f"WAITING_FOR_CI: {ci_message}")
        return 0

    with tempfile.TemporaryDirectory(prefix="sql-wrongbook-pull-") as temp_name:
        temp_dir = Path(temp_name)
        archive = temp_dir / f"{latest_sha}.tar.gz"
        log("Downloading the tested release archive")
        download_archive(latest_sha, archive)
        project_dir = extract_archive(archive, temp_dir / "release")

        run_checked(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            project_dir,
        )
        run_checked(["bash", "-n", "deploy/update_native.sh"], project_dir)
        run_checked(["bash", "deploy/update_native.sh"], project_dir)

    record_deployed_sha(latest_sha)
    log(f"PULL_UPDATE_OK: deployed {latest_sha}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"PULL_UPDATE_FAILED: {exc}")
        raise
