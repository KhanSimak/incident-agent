"""
tools/deploys_live.py — reads real deploy events from logs/deploys.log,
written by infra/fault_injection/inject_bad_deploy.sh when it runs. This
is what makes "a deploy happened" a real, timestamped fact on disk
instead of something hardcoded into fixtures.py's DeployEvent list.
"""
import time
from pathlib import Path


def get_recent_deploys_live(service: str, log_path: Path, lookback_seconds: int = 900) -> dict:
    if not log_path.exists():
        return {"service": service, "lookback_seconds": lookback_seconds, "deploys": []}

    now = time.time()
    deploys = []
    for line in log_path.read_text().splitlines():
        parts = line.strip().split("|")
        if len(parts) != 4:
            continue   # skip malformed lines rather than crash the whole query
        ts_str, svc, commit, description = parts
        try:
            ts = float(ts_str)
        except ValueError:
            continue
        if svc == service and (now - ts) <= lookback_seconds:
            deploys.append({"timestamp": ts, "commit": commit, "description": description})

    return {"service": service, "lookback_seconds": lookback_seconds, "deploys": deploys}
