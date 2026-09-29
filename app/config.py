"""config.py — same pydantic-settings pattern as your real Codebase Q&A project."""
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings

# app/config.py -> app/ -> repo root
REPO_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    groq_api_key: str
    groq_model: str = "qwen/qwen3.8-27b"
    openrouter_api_key: str | None = None
    openrouter_model: str = "qwen/qwen3.6-27b"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # "fixture" = Phase 1 (fixtures.py, no infra needed)
    # "live"    = Phase 2 (real Prometheus + real log files + real
    #             infra/logs/deploys.log — see infra/docker-compose.yml)
    data_source: str = "fixture"
    prometheus_url: str = "http://127.0.0.1:9090"
    # Anchored to the repo root (this file is app/config.py, so parents[1]),
    # NOT to the process CWD. These used to be bare relative paths, which
    # silently resolved to a nonexistent directory whenever uvicorn was
    # started from anywhere other than the repo root — and logs_live then
    # reported it as "no log file found for service ...", which reads like
    # a missing service rather than a missing directory.
    log_dir: Path = REPO_ROOT / "infra" / "logs"
    deploys_log_path: Path = REPO_ROOT / "infra" / "logs" / "deploys.log"
    admin_token: str | None = None   # gates only /admin/* fault-injection demo endpoints

    @field_validator("log_dir", "deploys_log_path")
    @classmethod
    def _anchor_to_repo_root(cls, v: Path) -> Path:
        """A relative LOG_DIR/DEPLOYS_LOG_PATH supplied via .env is still
        resolved against the repo root, so the configured path means the
        same thing no matter where the server was launched from."""
        return v if v.is_absolute() else (REPO_ROOT / v).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()