import json
import re
import time
from pathlib import Path


def list_log_services(log_dir: Path) -> list[str]:
    """
    Services that have a log file on disk. A second, independent source
    for the service catalog: a service may have logs before Prometheus
    has scraped it, or vice versa, so the two are unioned rather than
    one being preferred.

    `deploys.log` is the deploy feed (see settings.deploys_log_path), not
    a service, so it is excluded.
    """
    log_dir = Path(log_dir)

    if not log_dir.is_dir():
        return []

    return sorted({
        p.stem.strip().lower()
        for p in log_dir.glob("*.log")
        if p.stem and p.stem.lower() != "deploys"
    })


def run_log_query_live(
    query: str,
    log_dir: Path,
    lookback_seconds: int = 300,
) -> dict:
    match = re.match(
        r'^\s*\{service="([^"]+)"\}\s*\|=\s*"([^"]*)"\s*$',
        query,
    )

    if not match:
        return {
            "error": (
                f'Could not parse query "{query}". '
                'Expected format: {service="x"} |= "pattern"'
            )
        }

    service_filter, pattern = match.groups()
    log_dir = Path(log_dir)
    log_file = log_dir / f"{service_filter}.log"

    # Distinguish the three failure modes that used to collapse into one
    # "no log file found" message. That message read as "this service
    # doesn't exist", when the real cause was usually a log directory
    # that hadn't been created or wasn't the one the containers write to.
    if not log_dir.is_dir():
        return {
            "error": (
                f"Log directory {log_dir} does not exist. Live log "
                f"collection is not configured — services bind-mount "
                f"./logs to /var/log/app (see infra/docker-compose.yml), "
                f"so this directory is created when the stack first runs."
            )
        }

    if not log_file.exists():
        available = sorted(p.stem for p in log_dir.glob("*.log"))
        return {
            "error": (
                f"No log file found for service '{service_filter}' at "
                f"{log_file}. "
                + (
                    f"Log files present: {', '.join(available)}."
                    if available
                    else f"No .log files present in {log_dir} at all — "
                         f"the services may not be running, or may not "
                         f"have served any traffic yet."
                )
            )
        }

    cutoff = time.time() - lookback_seconds
    matches = []

    for line in log_file.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        timestamp = entry.get("timestamp")

        if timestamp is None:
            continue

        if timestamp < cutoff:
            continue

        message = entry.get("message", "")

        if pattern == "" or pattern.lower() in message.lower():
            matches.append({
                "timestamp": timestamp,
                "level": entry.get("level"),
                "message": message,
            })

    return {
        "service": service_filter,
        "pattern": pattern,
        "matched_lines": matches[-50:],
        "match_count": len(matches),
        "lookback_seconds": lookback_seconds,
    }