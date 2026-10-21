"""Structure checks for the local monitoring stack. Full validation: `make infra-check`."""

import json
from pathlib import Path

import yaml

INFRA = Path(__file__).resolve().parent.parent / "infra"


def load_yaml(relative: str) -> dict:
    return yaml.safe_load((INFRA / relative).read_text(encoding="utf-8"))


def test_stack_has_all_services():
    services = load_yaml("docker-compose.yml")["services"]
    assert {"prometheus", "grafana", "loki", "alloy"} <= services.keys()


def _pinned(image: str) -> bool:
    return ":" in image and not image.endswith(":latest")


def test_images_are_pinned():
    for name, service in load_yaml("docker-compose.yml")["services"].items():
        if "build" in service:  # built here: every FROM in its Dockerfile must be pinned
            dockerfile = INFRA / service["build"] / "Dockerfile"
            images = [
                line.split()[1]
                for line in dockerfile.read_text(encoding="utf-8").splitlines()
                if line.startswith("FROM ")
            ]
            assert images, f"{name} Dockerfile has no FROM"
            assert all(_pinned(i) for i in images), f"{name} base images must be pinned"
        else:
            assert _pinned(service["image"]), f"{name} image must be pinned"


def test_deployer_base_images_are_pinned():
    dockerfile = INFRA.parent / "deploy" / "Dockerfile"
    images = [ln.split()[1] for ln in dockerfile.read_text().splitlines() if ln.startswith("FROM ")]
    assert images and all(_pinned(i) for i in images)


def test_harness_delegate_is_opt_in_and_has_no_ports():
    delegate = load_yaml("docker-compose.yml")["services"]["harness-delegate"]
    assert delegate["profiles"] == ["harness"]  # `make infra-up` works without Harness
    assert "ports" not in delegate  # it connects out; nothing connects in
    assert delegate["environment"]["DELEGATE_TOKEN"].startswith("${HARNESS_DELEGATE_TOKEN")


def test_pipeline_never_pastes_inputs_into_the_script():
    pipeline = load_yaml("harness/shoplite-deploy.pipeline.yaml")["pipeline"]
    step = pipeline["stages"][0]["stage"]["spec"]["execution"]["steps"][0]["step"]["spec"]
    script = step["source"]["spec"]["script"]
    assert "<+" not in script  # Harness expressions would be pasted in as shell code
    assert {v["name"] for v in step["environmentVariables"]} == {
        "VERSION",
        "REASON",
        "CHANGE",
        "RUN_ID",
    }
    assert step["delegateSelectors"] == ["sentinel-laptop"]


def test_published_ports_are_local_only():
    for name, service in load_yaml("docker-compose.yml")["services"].items():
        for port in service.get("ports", []):
            assert str(port).startswith(
                "127.0.0.1:"
            ), f"{name} port {port} is exposed beyond this PC"


def test_grafana_password_comes_from_env():
    env = load_yaml("docker-compose.yml")["services"]["grafana"]["environment"]
    assert env["GF_SECURITY_ADMIN_PASSWORD"].startswith("${GRAFANA_ADMIN_PASSWORD")


def test_alloy_reads_shoplite_logs_and_writes_to_loki():
    volumes = load_yaml("docker-compose.yml")["services"]["alloy"]["volumes"]
    assert "../logs:/var/log/shoplite:ro" in volumes
    config = (INFRA / "alloy" / "config.alloy").read_text(encoding="utf-8")
    assert '"/var/log/shoplite/*.log"' in config
    assert "http://loki:3100/loki/api/v1/push" in config
    for label in ("level", "service", "env", "version", "commit_sha"):
        assert f"{label} " in config


def test_grafana_has_both_datasources():
    datasources = load_yaml("grafana/provisioning/datasources/datasources.yml")["datasources"]
    assert {d["uid"] for d in datasources} == {"prometheus", "loki"}


def test_dashboard_panels_use_known_datasources():
    dashboard = json.loads((INFRA / "grafana/dashboards/shoplite.json").read_text(encoding="utf-8"))
    titles = {p["title"]: p for p in dashboard["panels"]}
    assert "Error logs" in titles
    assert titles["Error logs"]["datasource"]["uid"] == "loki"
    assert 'level="ERROR"' in titles["Error logs"]["targets"][0]["expr"]
    for panel in dashboard["panels"]:
        assert panel["datasource"]["uid"] in {"prometheus", "loki"}


def test_alerting_services_are_wired_together():
    services = load_yaml("docker-compose.yml")["services"]
    assert {"alertmanager", "alert-inbox"} <= services.keys()
    assert "ports" not in services["alert-inbox"], "the inbox should not be reachable from the PC"

    prometheus = load_yaml("prometheus/prometheus.yml")
    targets = prometheus["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"]
    assert targets == ["alertmanager:9093"]

    receiver = load_yaml("alertmanager/alertmanager.yml")["receivers"][0]
    urls = {w["url"]: w for w in receiver["webhook_configs"]}
    assert set(urls) == {
        "http://host.docker.internal:8001/alerts",  # orchestrator
        "http://alert-inbox:8080/alerts",
    }
    assert all(w["send_resolved"] is True for w in urls.values())


def test_warning_rule_is_lower_and_silenced_by_the_critical_one():
    rules = {
        r["alert"]: r for r in load_yaml("prometheus/rules/shoplite.yml")["groups"][0]["rules"]
    }
    warning = rules["ShopLiteElevatedErrorRate"]
    assert warning["labels"]["severity"] == "warning"
    assert "> 0.05" in warning["expr"] and warning["for"] == "2m"
    inhibit = load_yaml("alertmanager/alertmanager.yml")["inhibit_rules"][0]
    assert inhibit["source_matchers"] == ['severity="critical"']
    assert inhibit["target_matchers"] == ['severity="warning"']
    assert set(inhibit["equal"]) == {"service", "env", "version", "commit_sha"}


def test_high_error_rate_rule_matches_the_agreed_threshold():
    rule = load_yaml("prometheus/rules/shoplite.yml")["groups"][0]["rules"][0]
    assert rule["alert"] == "ShopLiteHighErrorRate"
    assert rule["for"] == "1m"
    assert "> 0.20" in rule["expr"]
    for tag in ("service", "env", "version", "commit_sha"):
        assert tag in rule["expr"], "alert must say which service/version/commit is failing"
    assert rule["labels"]["severity"] == "critical"


def test_dashboard_marks_firing_alerts():
    dashboard = json.loads((INFRA / "grafana/dashboards/shoplite.json").read_text(encoding="utf-8"))
    annotation = dashboard["annotations"]["list"][0]
    assert "ShopLiteHighErrorRate" in annotation["expr"]
    assert annotation["iconColor"] == "red"
