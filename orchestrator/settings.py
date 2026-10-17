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
    "JIRA_BASE_URL": "jira_base_url",
    "JIRA_EMAIL": "jira_email",
    "JIRA_API_TOKEN": "jira_api_token",
    "JIRA_PROJECT_KEY": "jira_project_key",
    "SERVICENOW_INSTANCE_URL": "servicenow_instance_url",
    "SERVICENOW_USERNAME": "servicenow_username",
    "SERVICENOW_PASSWORD": "servicenow_password",
    "SERVICENOW_CHANGE_GROUP": "servicenow_change_group",
    "HARNESS_ACCOUNT_ID": "harness_account_id",
    "HARNESS_API_KEY": "harness_api_key",
    "HARNESS_ORG": "harness_org",
    "HARNESS_PROJECT": "harness_project",
    "HARNESS_PIPELINE": "harness_pipeline",
    "DEPLOY_LOG_PATH": "deploy_log_path",
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

    # Jira: all four set = file follow-up tickets when incidents resolve.
    jira_base_url: str | None = None
    jira_email: str | None = None
    jira_api_token: SecretStr | None = None
    jira_project_key: str | None = None

    # ServiceNow: all three set = incident records, emergency changes and CAB approvals.
    servicenow_instance_url: str | None = None
    servicenow_username: str | None = None
    servicenow_password: SecretStr | None = None
    # The group a change is assigned to before it goes to the CAB for approval.
    servicenow_change_group: str = "Change Management"

    # Harness: account ID + API key = real rollbacks after a CAB approval (needs ServiceNow too).
    harness_account_id: str | None = None
    harness_api_key: SecretStr | None = None
    harness_org: str = "default"
    harness_project: str = "sentinel"
    harness_pipeline: str = "shoplite_deploy"
    deploy_log_path: str = "logs/deploys.log"  # where deploys are recorded (rollback target)

    def harness_ready(self) -> bool:
        return bool(self.harness_account_id and self.harness_api_key)

    def servicenow_ready(self) -> bool:
        return bool(
            self.servicenow_instance_url and self.servicenow_username and self.servicenow_password
        )

    def jira_ready(self) -> bool:
        return bool(
            self.jira_base_url and self.jira_email and self.jira_api_token and self.jira_project_key
        )

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
