import json
import re
import time
from pathlib import Path


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
    log_file = log_dir / f"{service_filter}.log"

    if not log_file.exists():
        return {
            "error": (
                f"No log file found for service '{service_filter}' "
                f"at {log_file}"
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