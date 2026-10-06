"""The package as the gateway's installer and Docker see it.

aab-plugin.yaml is validated by the gateway's installer (strict, schema 1);
these tests hold it to the same rules (finance-plugin plan "Design": the
descriptor schema), and hold the Dockerfile and pyproject to the base-image
contract, so a packaging mistake fails here and not on the server.
"""

import importlib.metadata
import re
import tomllib
from pathlib import Path

import yaml

from aab_plugin_finance import main
from aab_plugin_finance.adapter import MANIFEST_PATH

REPO = Path(__file__).resolve().parents[1]
DESCRIPTOR = yaml.safe_load((REPO / "aab-plugin.yaml").read_text(encoding="utf-8"))
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")
PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_descriptor_is_exactly_schema_1():
    assert DESCRIPTOR == {
        "schema": 1, "service": "finance", "plugins": ["finance"],
        "manifests": ["aab_plugin_finance/manifest.yaml"], "runtime": "0.3",
        "build": {"dockerfile": "Dockerfile"}, "volumes": {"finance_data": "/data"},
        "environment": {"FINANCE_DB": "/data/finance.db"}, "env_passthrough": ["TZ"]}


def test_the_descriptor_follows_the_installer_rules():
    assert re.fullmatch(r"[a-z][a-z0-9]{1,31}", DESCRIPTOR["service"])
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", v) for v in DESCRIPTOR["volumes"])
    assert set(DESCRIPTOR["env_passthrough"]) <= {"TZ", "LOG_LEVEL", "LOG_FORMAT"}
    assert all(isinstance(v, str) for v in DESCRIPTOR["environment"].values())
    assert (REPO / DESCRIPTOR["build"]["dockerfile"]).is_file()
    ids = [yaml.safe_load((REPO / m).read_text(encoding="utf-8"))["id"]
           for m in DESCRIPTOR["manifests"]]
    assert ids == DESCRIPTOR["plugins"]
    assert REPO / DESCRIPTOR["manifests"][0] == MANIFEST_PATH


def test_the_descriptor_and_the_code_agree():
    assert DESCRIPTOR["environment"]["FINANCE_DB"] == main.DEFAULT_DB
    assert main.DEFAULT_DB.startswith(DESCRIPTOR["volumes"]["finance_data"] + "/")
    assert main.SERVICE == DESCRIPTOR["service"]


def test_the_dockerfile_follows_the_base_image_contract():
    assert DOCKERFILE.count("\nFROM ") == 1 and "FROM ghcr.io/pelegw/aab-plugin-base:0.3.0\n" \
        in DOCKERFILE
    cmd = re.search(r"^CMD (\[.*\])$", DOCKERFILE, re.M).group(1)
    assert yaml.safe_load(cmd) == ["uvicorn", "--factory", "aab_plugin_finance.main:create_app",
                                   "--host", "0.0.0.0", "--port", "8090", "--workers", "1",
                                   "--no-access-log"]
    # Installed as root, run as aab; the volume is aab-owned and private.
    assert DOCKERFILE.index("USER root") < DOCKERFILE.index("RUN pip install") < DOCKERFILE.rindex(
        "USER aab")
    assert "chown aab:aab /data" in DOCKERFILE and "chmod 0700 /data" in DOCKERFILE
    assert "VOLUME /data" in DOCKERFILE
    assert "COPY VERSION pyproject.toml" in DOCKERFILE        # hatch reads VERSION at build


def test_the_runtime_is_a_version_dependency_and_the_url_an_extra():
    deps = PYPROJECT["project"]["dependencies"]
    assert "aab-plugin-runtime>=0.2.0,<1" in deps
    assert not any("@" in d for d in deps)        # a URL here would break the Docker build
    [url] = PYPROJECT["project"]["optional-dependencies"]["runtime"]
    assert url == ("aab-plugin-runtime @ git+https://github.com/pelegw/agent-authority-broker"
                   "@v0.3.0#subdirectory=plugin-runtime")


def test_the_version_lives_in_VERSION():
    version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    assert importlib.metadata.version("aab-plugin-finance") == version
    assert PYPROJECT["project"]["dynamic"] == ["version"]


def test_ci_never_puts_the_secret_in_an_if():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert not re.search(r"if:.*secrets\.", ci)
    assert "steps.gate.outputs.present == 'true'" in ci
