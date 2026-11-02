"""Process run bookkeeping: one runs row per start, one run_stops row per clean stop."""

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from nta.clock import Clock
from nta.logs import log
from nta.schema import Run, RunStop

ROOT = Path(__file__).resolve().parent.parent


def code_sha(repo: Path = ROOT) -> str:
    """HEAD commit, suffixed with -dirty if the working tree has uncommitted changes."""
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                             text=True, check=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True,
                               text=True, check=True, timeout=5).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()


def start_run(sf: sessionmaker[Session], process: str, config: dict[str, Any], clock: Clock,
              sha: str | None = None) -> str:
    run = Run(process=process, code_sha=sha or code_sha(), config_hash=config_hash(config),
              started_at=clock.now())
    with sf() as s, s.begin():
        s.add(run)
    log("run_started", run_id=run.id, process=process, code_sha=run.code_sha,
        config_hash=run.config_hash, config=config)
    return run.id


def stop_run(sf: sessionmaker[Session], run_id: str, reason: str, clock: Clock) -> None:
    with sf() as s, s.begin():
        s.add(RunStop(run_id=run_id, stopped_at=clock.now(), reason=reason))
    log("run_stopped", run_id=run_id, reason=reason)
