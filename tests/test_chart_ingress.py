"""Chart render: the ingress to the api Service and the helm test hook."""

from test_chart import render

HOST = "agents.example.test"
INGRESS = {
    "ingress": {
        "enabled": True,
        "className": "nginx",
        "denySourceRanges": ["10.0.0.0/8", "192.168.0.0/16"],
        "hosts": [{"host": HOST, "paths": [{"path": "/history", "pathType": "Prefix"}]}],
        "tls": [{"secretName": "agents-tls", "hosts": [HOST]}],
    }
}


def _one(docs: list[dict], kind: str) -> dict:
    found = [d for d in docs if d["kind"] == kind]
    assert len(found) == 1, f"expected one {kind}, got {len(found)}"
    return found[0]


def test_ingress_routes_http_only():
    docs = render(INGRESS)
    ing = _one(docs, "Ingress")
    spec = ing["spec"]
    assert spec["ingressClassName"] == "nginx"
    assert spec["tls"] == [{"secretName": "agents-tls", "hosts": [HOST]}]
    (rule,) = spec["rules"]
    assert rule["host"] == HOST
    (path,) = rule["http"]["paths"]
    assert path["path"] == "/history"
    assert path["pathType"] == "Prefix"
    svc = _one(docs, "Service")
    backend = path["backend"]["service"]
    assert backend["name"] == svc["metadata"]["name"]
    assert backend["port"] == {"name": "http"}
    assert ing["metadata"]["annotations"]["nginx.ingress.kubernetes.io/denylist-source-range"] == (
        "10.0.0.0/8,192.168.0.0/16"
    )


def test_ingress_never_exposes_the_token_listeners():
    docs = render(
        INGRESS
        | {
            "serviceListener": {"enabled": True, "tokensSecret": {"name": "s"}},
            "ingestListener": {"enabled": True, "tokensSecret": {"name": "i"}},
        }
    )
    ing = _one(docs, "Ingress")
    ports = [
        p["backend"]["service"]["port"] for r in ing["spec"]["rules"] for p in r["http"]["paths"]
    ]
    assert ports == [{"name": "http"}]


def test_ingress_disabled_by_default():
    assert not [d for d in render() if d["kind"] == "Ingress"]


def test_default_ingress_path_is_history():
    ing = _one(render({"ingress": {"enabled": True}}), "Ingress")
    (rule,) = ing["spec"]["rules"]
    assert [p["path"] for p in rule["http"]["paths"]] == ["/history"]


def test_no_cert_manager_annotation():
    ing = _one(render(INGRESS), "Ingress")
    annotations = ing["metadata"].get("annotations", {})
    assert not [k for k in annotations if "cert-manager" in k]


def test_connection_hook():
    docs = render()
    pod = _one(docs, "Pod")
    assert pod["metadata"]["annotations"]["helm.sh/hook"] == "test"
    svc = _one(docs, "Service")
    container = pod["spec"]["containers"][0]
    target = " ".join(container.get("command", []) + container.get("args", []))
    assert f"{svc['metadata']['name']}:80/healthz" in target
    assert pod["spec"]["restartPolicy"] == "Never"


def test_connection_hook_is_pinned_and_restricted():
    pod = _one(render(), "Pod")
    annotations = pod["metadata"]["annotations"]
    assert annotations["helm.sh/hook-delete-policy"] == "before-hook-creation,hook-succeeded"
    container = pod["spec"]["containers"][0]
    name, _, tag = container["image"].rpartition(":")
    assert name and tag and tag != "latest"
    psc = pod["spec"]["securityContext"]
    assert psc["runAsNonRoot"] is True
    assert psc["runAsUser"] != 0
    assert psc["seccompProfile"] == {"type": "RuntimeDefault"}
    sc = container["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["readOnlyRootFilesystem"] is True
    assert sc["capabilities"] == {"drop": ["ALL"]}
