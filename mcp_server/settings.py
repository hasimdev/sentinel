"""MCP server configuration, read from environment variables."""

import os

from pydantic import BaseModel

# Environment variable name -> Settings field name.
ENV_VARS = {
    "PROMETHEUS_URL": "prometheus_url",
    "LOKI_URL": "loki_url",
    "ORCHESTRATOR_URL": "orchestrator_url",
}


class Settings(BaseModel):
    # 127.0.0.1 rather than localhost: on Windows, localhost tries IPv6 first and stalls.
    prometheus_url: str = "http://127.0.0.1:9090"
    loki_url: str = "http://127.0.0.1:3100"
    orchestrator_url: str = "http://127.0.0.1:8001"


def load_settings() -> Settings:
    """Build Settings from whichever environment variables are set; the rest use defaults."""
    values = {field: os.environ[var] for var, field in ENV_VARS.items() if os.environ.get(var)}
    return Settings(**values)
