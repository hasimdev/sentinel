"""ShopLite configuration, read from environment variables (never hard-coded)."""

import os

from pydantic import BaseModel, Field

# Environment variable name -> Settings field name.
ENV_VARS = {
    "SERVICE": "service",
    "ENV": "env",
    "VERSION": "version",
    "COMMIT_SHA": "commit_sha",
    "SHOPLITE_FAIL_RATE": "fail_rate",
}


class Settings(BaseModel):
    service: str = "shoplite"
    env: str = "local"
    version: str = "0.1.0"
    commit_sha: str = "unknown"
    # Share of checkouts that fail on purpose (0 = never, 1 = always).
    fail_rate: float = Field(default=0.0, ge=0.0, le=1.0)

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
    values = {field: os.environ[var] for var, field in ENV_VARS.items() if var in os.environ}
    return Settings(**values)
