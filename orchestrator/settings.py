"""Orchestrator configuration, read from environment variables."""

import os

from pydantic import BaseModel, SecretStr

# Environment variable name -> Settings field name.
ENV_VARS = {
    "ENV": "env",
    "ORCHESTRATOR_VERSION": "version",
    "COMMIT_SHA": "commit_sha",
    "PROMETHEUS_URL": "prometheus_url",
    "LOKI_URL": "loki_url",
    "ORCHESTRATOR_DB_PATH": "db_path",
    "ORCHESTRATOR_LOG_FILE": "log_file",
    "TRIAGE_ENABLED": "triage_enabled",
    "SLACK_BOT_TOKEN": "slack_bot_token",
    "SLACK_APP_TOKEN": "slack_app_token",
    "SLACK_CHANNEL": "slack_channel",
    "GRAFANA_URL": "grafana_url",
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
    # AI triage runs when this is on AND an Anthropic API key is available.
    triage_enabled: bool = True
    # Slack: all three set = post incidents and receive Approve / Reject clicks.
    # SecretStr keeps the tokens out of logs and error messages.
    slack_bot_token: SecretStr | None = None
    slack_app_token: SecretStr | None = None
    slack_channel: str | None = None
    grafana_url: str = "http://127.0.0.1:3000"

    def slack_ready(self) -> bool:
        return bool(self.slack_bot_token and self.slack_app_token and self.slack_channel)

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
