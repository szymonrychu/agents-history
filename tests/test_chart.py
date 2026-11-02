"""Chart render: one api Deployment with a migrate init container, ports, token files, values."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent.parent / "chart" / "agents-history"
FULLNAME = "hs-agents-history"

DB_PASS = "".join(["db", "-", "fixture", "-", "value"])

BASE = {
    "image": {"tag": "1.2.3"},
    "postgres": {"url": "postgresql://" + "u:" + DB_PASS + "@db:5432/h"},
}
SERVICE = {"serviceListener": {"enabled": True, "tokensSecret": {"name": "history-service-tokens"}}}
INGEST = {"ingestListener": {"enabled": True, "tokensSecret": {"name": "history-ingest-token"}}}
BOTH = {**SERVICE, **INGEST}


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _run(values: dict) -> subprocess.CompletedProcess:
    helm = shutil.which("helm")
    assert helm, "helm binary not found on PATH; the chart tests require it"
    with tempfile.TemporaryDirectory() as tmp:
        vf = Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values))
        return subprocess.run(
            [helm, "template", "hs", str(CHART), "-n", "agents", "-f", str(vf)],
            capture_output=True,
            text=True,
        )


def render(overrides: dict | None = None) -> list[dict]:
    proc = _run(_merge(BASE, overrides or {}))
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def api_deployment(docs: list[dict]) -> dict:
    (dep,) = [d for d in docs if d["kind"] == "Deployment"]
    return dep


def pod(docs: list[dict]) -> dict:
    return api_deployment(docs)["spec"]["template"]["spec"]


def main(docs: list[dict]) -> dict:
    (container,) = pod(docs)["containers"]
    return container


def _env(container: dict) -> dict[str, dict]:
    return {e["name"]: e for e in container.get("env", [])}


def _secret_refs(spec: dict) -> set[str]:
    """Every Secret a pod mounts or reads through env, init containers included."""
    names = {v["secret"]["secretName"] for v in spec["volumes"] if "secret" in v}
    for container in [*spec["initContainers"], *spec["containers"]]:
        for e in container.get("env", []):
            ref = e.get("valueFrom", {}).get("secretKeyRef")
            if ref:
                names.add(ref["name"])
    return names


def _mount(container: dict, name: str) -> dict:
    return next(m for m in container["volumeMounts"] if m["name"] == name)


def test_one_deployment_with_migrate_init():
    docs = render()
    kinds = sorted(d["kind"] for d in docs)
    assert kinds == ["ConfigMap", "Deployment", "Pod", "Secret", "Service", "ServiceAccount"]
    dep = api_deployment(docs)
    assert dep["metadata"]["name"] == f"{FULLNAME}-api"
    assert dep["spec"]["replicas"] == 1
    spec = pod(docs)
    (init,) = spec["initContainers"]
    assert init["name"] == "migrate"
    assert init["command"] == ["history"]
    assert init["args"] == ["migrate"]
    container = main(docs)
    assert container["name"] == "api"
    assert container["command"] == ["history"]
    assert container["args"] == ["api"]
    assert container["image"] == "harbor.szymonrichert.pl/containers/agents-history:1.2.3"
    assert spec["automountServiceAccountToken"] is False
    assert container["readinessProbe"]["httpGet"] == {"path": "/readyz", "port": 8000}
    assert container["livenessProbe"]["httpGet"] == {"path": "/healthz", "port": 8000}
    assert container["resources"] == {
        "requests": {"cpu": "50m", "memory": "128Mi"},
        "limits": {"memory": "512Mi"},
    }


def test_service_ports():
    docs = render()
    (svc,) = [d for d in docs if d["kind"] == "Service"]
    assert svc["metadata"]["name"] == FULLNAME
    assert [(p["name"], p["port"], p["targetPort"]) for p in svc["spec"]["ports"]] == [
        ("http", 80, "http")
    ]
    assert [p["name"] for p in main(docs)["ports"]] == ["http"]
    assert main(docs)["ports"][0]["containerPort"] == 8000
    docs = render(BOTH)
    (svc,) = [d for d in docs if d["kind"] == "Service"]
    by_name = {p["name"]: p for p in svc["spec"]["ports"]}
    assert (by_name["service"]["port"], by_name["service"]["targetPort"]) == (8001, "service")
    assert (by_name["ingest"]["port"], by_name["ingest"]["targetPort"]) == (8002, "ingest")
    ports = {p["name"]: p["containerPort"] for p in main(docs)["ports"]}
    assert ports == {"http": 8000, "service": 8001, "ingest": 8002}


def test_token_files_mounted_in_api_only():
    docs = render(BOTH)
    api = main(docs)
    env = _env(api)
    assert env["HISTORY_SERVICE_PORT"]["value"] == "8001"
    assert env["HISTORY_INGEST_PORT"]["value"] == "8002"
    assert env["HISTORY_SERVICE_TOKENS_FILE"]["value"] == "/var/run/secrets/service/tokens"
    assert env["HISTORY_INGEST_TOKENS_FILE"]["value"] == "/var/run/secrets/ingest/tokens"
    for name, path in (
        ("service-tokens", "/var/run/secrets/service"),
        ("ingest-tokens", "/var/run/secrets/ingest"),
    ):
        mount = _mount(api, name)
        assert mount["readOnly"] is True and mount["mountPath"] == path
    volumes = {v["name"]: v for v in pod(docs)["volumes"]}
    assert volumes["service-tokens"]["secret"] == {
        "secretName": "history-service-tokens",
        "items": [{"key": "tokens", "path": "tokens"}],
    }
    assert volumes["ingest-tokens"]["secret"] == {
        "secretName": "history-ingest-token",
        "items": [{"key": "token", "path": "tokens"}],
    }
    (init,) = pod(docs)["initContainers"]
    assert {m["name"] for m in init["volumeMounts"]} == {"tmp"}
    assert not [e for e in _env(init) if "TOKENS_FILE" in e]
    assert _secret_refs(pod(docs)) == {
        f"{FULLNAME}-database",
        "history-service-tokens",
        "history-ingest-token",
    }
    # The chart never renders either token: the Secrets are created by agents-helmfile.
    secrets = {d["metadata"]["name"] for d in docs if d["kind"] == "Secret"}
    assert secrets == {f"{FULLNAME}-database"}


def test_listeners_off_by_default():
    docs = render()
    env = _env(main(docs))
    assert not [e for e in env if e.startswith(("HISTORY_SERVICE", "HISTORY_INGEST"))]
    assert {v["name"] for v in pod(docs)["volumes"]} == {"tmp"}


def test_listener_without_secret_fails():
    for key, needle in (
        ("serviceListener", "serviceListener.tokensSecret.name"),
        ("ingestListener", "ingestListener.tokensSecret.name"),
    ):
        proc = _run(_merge(BASE, {key: {"enabled": True}}))
        assert proc.returncode != 0
        assert needle in proc.stderr


def test_env_prefix_enforced():
    proc = _run(_merge(BASE, {"env": {"FOO": "x"}}))
    assert proc.returncode != 0
    assert "HISTORY_" in proc.stderr


@pytest.mark.parametrize(
    "overrides",
    [
        {"postgres": {"url": ""}},
        {"postgres": {"url": "x", "existingSecret": "y"}},
    ],
    ids=["neither", "both"],
)
def test_postgres_exactly_one_source(overrides):
    proc = _run(_merge(BASE, overrides))
    assert proc.returncode != 0
    assert "postgres" in proc.stderr


def test_existing_secret_is_referenced_not_rendered():
    docs = render(
        {"postgres": {"url": "", "existingSecret": "cnpg-app", "existingSecretKey": "dsn"}}
    )
    assert not [d for d in docs if d["kind"] == "Secret"]
    (init,) = pod(docs)["initContainers"]
    assert _env(init)["HISTORY_DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "cnpg-app",
        "key": "dsn",
    }


def test_database_secret_wired():
    docs = render()
    config = next(d for d in docs if d["kind"] == "ConfigMap")
    assert DB_PASS not in yaml.safe_dump(config)
    assert "HISTORY_DATABASE_URL" not in config["data"]
    assert DB_PASS not in yaml.safe_dump(api_deployment(docs))
    secret = next(d for d in docs if d["kind"] == "Secret")
    assert secret["metadata"]["name"] == f"{FULLNAME}-database"
    assert secret["stringData"] == {"uri": BASE["postgres"]["url"]}
    for container in [*pod(docs)["initContainers"], main(docs)]:
        ref = _env(container)["HISTORY_DATABASE_URL"]
        assert "value" not in ref
        assert ref["valueFrom"]["secretKeyRef"] == {"name": f"{FULLNAME}-database", "key": "uri"}


def test_read_only_root_and_tmp():
    docs = render()
    spec = pod(docs)
    container = main(docs)
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert _mount(container, "tmp")["mountPath"] == "/tmp"
    assert next(v for v in spec["volumes"] if v["name"] == "tmp") == {"name": "tmp", "emptyDir": {}}
    assert spec["securityContext"]["runAsNonRoot"] is True


def test_image_tag_defaults_to_app_version():
    docs = render({"image": {"tag": ""}})
    assert main(docs)["image"].endswith("/agents-history:0.1.0")


def test_env_and_port_reach_the_configmap():
    docs = render(
        {"env": {"HISTORY_SWEEP_BATCH": 25}, "components": {"api": {"port": 9000}}},
    )
    config = next(d for d in docs if d["kind"] == "ConfigMap")
    assert config["data"]["HISTORY_SWEEP_BATCH"] == "25"
    assert config["data"]["HISTORY_HTTP_PORT"] == "9000"
    api = main(docs)
    assert api["readinessProbe"]["httpGet"]["port"] == 9000
    assert api["ports"][0]["containerPort"] == 9000


def test_listener_ports_reach_the_api():
    docs = render(
        {
            "serviceListener": {
                "enabled": True,
                "port": 9001,
                "servicePort": 81,
                "tokensSecret": {"name": "s"},
            },
            "ingestListener": {
                "enabled": True,
                "port": 9002,
                "servicePort": 82,
                "tokensSecret": {"name": "i"},
            },
        }
    )
    env = _env(main(docs))
    assert env["HISTORY_SERVICE_PORT"]["value"] == "9001"
    assert env["HISTORY_INGEST_PORT"]["value"] == "9002"
    (svc,) = [d for d in docs if d["kind"] == "Service"]
    by_name = {p["name"]: p for p in svc["spec"]["ports"]}
    assert by_name["service"]["port"] == 81 and by_name["ingest"]["port"] == 82
    ports = {p["name"]: p["containerPort"] for p in main(docs)["ports"]}
    assert ports == {"http": 8000, "service": 9001, "ingest": 9002}


def test_api_pod_annotations():
    docs = render({"components": {"api": {"podAnnotations": {"x/y": "z"}}}})
    ann = api_deployment(docs)["spec"]["template"]["metadata"]["annotations"]
    assert ann["x/y"] == "z"
    assert "checksum/settings" in ann
