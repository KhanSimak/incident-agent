"""config.py — same pydantic-settings pattern as your real Codebase Q&A project."""
from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    groq_api_key: str
    groq_model: str = "qwen/qwen3.6-27b"
    openrouter_api_key: str | None = None
    openrouter_model: str = "qwen/qwen3.6-27b"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # "fixture" = Phase 1 (fixtures.py, no infra needed)
    # "live"    = Phase 2 (real Prometheus + real log files + real
    #             infra/logs/deploys.log — see infra/docker-compose.yml)
    data_source: str = "fixture"
    prometheus_url: str = "http://127.0.0.1:9090"
    log_dir: Path = Path("infra/logs")
    deploys_log_path: Path = Path("infra/logs/deploys.log")


@lru_cache
def get_settings() -> Settings:
    return Settings()
