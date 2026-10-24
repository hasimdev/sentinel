"""Raise the normal change a release is deployed under (make release-change VERSION=0.2.0).

Every release goes out under a change: Change Management, then the CAB, approve it in
ServiceNow; then `make deploy VERSION=... CHANGE=CHG...` records the change on the deploy.
If the release goes bad, the rollback backs out this change, with no new ticket.
"""

import re
import sys

from dotenv import dotenv_values

from orchestrator.adapters.change_mgmt import NewChange, ServiceNowChangeMgmt

VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def release_change(version: str) -> NewChange:
    return NewChange(
        short_description=f"Release: deploy shoplite {version}",
        description=(
            f"Planned release of shoplite {version} (Git tag shoplite-v{version}). A normal "
            "change: Change Management, then the CAB, approve it before it is deployed."
        ),
        justification=f"Planned release {version}; release notes on its Git tag.",
        implementation_plan=(
            f"make deploy VERSION={version} CHANGE=<this change>, or the Harness pipeline "
            "with this change number."
        ),
        backout_plan=(
            "Roll back to the previous version. The rollback backs out this change, so it "
            "needs no new ticket."
        ),
        test_plan="Error rate under 5% after the deploy; no ShopLite alerts fire.",
    )


def main() -> None:
    if len(sys.argv) != 2 or not VERSION.fullmatch(sys.argv[1]):
        sys.exit("Usage: python -m orchestrator.release_change <version>, e.g. 0.2.0")
    version = sys.argv[1]
    env = dotenv_values(".env")
    snow = ServiceNowChangeMgmt(
        env["SERVICENOW_INSTANCE_URL"],
        env["SERVICENOW_USERNAME"],
        env["SERVICENOW_PASSWORD"],
        env.get("SERVICENOW_CHANGE_GROUP") or "Change Management",
    )
    change = snow.create_normal_change(release_change(version))
    snow.request_assessment(change)
    print(f"Raised {change.number} for release {version}: {change.url}")
    print("Approve it in ServiceNow (Change Management, then CAB), then deploy with:")
    print(f"  make deploy VERSION={version} CHANGE={change.number}")


if __name__ == "__main__":
    main()
