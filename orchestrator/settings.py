"""Orchestrator configuration, read from environment variables."""

import os

from pydantic import BaseModel

# Environment variable name -> Settings field name.
ENV_VARS = {
    "ENV": "env",
    "ORCHESTRATOR_VERSION": "version",
    "COMMIT_SHA": "commit_sha",
    "PROMETHEUS_URL": "prometheus_url",
    "LOKI_URL": "loki_url",
    "ORCHESTRATOR_DB_PATH": "db_path",
    "ORCHESTRATOR_LOG_FILE": "log_file",
}


class Settings(BaseModel):
    service: str = "orchestrator"
    env: str = "local"
    version: str = "0.1.0"
    commit_sha: str = "unknown"
    # 127.0.0.1 rather than localhost: on Windows, localhost tries IPv6 first and stalls.
    prometheus_url: str = "http://127.0.0.1:9090"
    loki_url: str = "http://127.0.0.1:3100"
    db_path: str = "data/orchestrator.db"
    log_file: str | None = None

    def tags(self) -> dict[str, str]:
        """The four tags every log, metric and deploy must carry."""
        return {
            "service": self.service,
            "env": self.env,
            "version": self.version,
            "commit_sha": self.commit_sha,
        }


def load_settings() -> Settings:
    """Build Settings from whichever environment variables are set; the rest use defaults."""
    values = {field: os.environ[var] for var, field in ENV_VARS.items() if os.environ.get(var)}
    return Settings(**values)
