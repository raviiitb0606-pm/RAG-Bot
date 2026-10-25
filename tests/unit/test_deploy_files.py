"""Deployment files stay consistent with the code (docs/deployment-plan.md D1-D4)."""

import re
from pathlib import Path

import yaml

from mf_faq.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def dockerfile_arg(name: str) -> str:
    match = re.search(rf"^ARG {name}=(\S+)$", (ROOT / "Dockerfile").read_text(), re.M)
    assert match, f"Dockerfile has no ARG {name}"
    return match.group(1)


def test_model_and_revision_match_everywhere():
    """An index only loads with the model revision it was built with (deployment D2)."""
    settings = Settings(_env_file=None)
    workflow = yaml.safe_load((ROOT / ".github/workflows/daily-refresh.yml").read_text())
    env_example = (ROOT / ".env.example").read_text()
    for key, value in (
        ("MF_FAQ_EMBED_MODEL", settings.embed_model),
        ("MF_FAQ_EMBED_MODEL_REVISION", settings.embed_model_revision),
    ):
        assert dockerfile_arg(key) == value
        assert workflow["env"][key] == value
        assert f"{key}={value}\n" in env_example


def test_docker_context_keeps_secrets_and_data_out():
    ignored = set((ROOT / ".dockerignore").read_text().split())
    assert {".env", ".env.*", "data/", "corpus/raw/", ".git"} <= ignored


def test_start_script_serves_on_railways_port():
    script = (ROOT / "scripts/railway-start.sh").read_text()
    assert 'exec mf-faq serve --host 0.0.0.0 --port "${PORT:-8000}"' in script
    assert script.index("mf-faq pull-index") < script.index("exec mf-faq serve")


def test_linux_torch_is_cpu_only():
    """D1: no CUDA wheels in the lock (Railway and the GitHub runners have no GPU)."""
    lock = (ROOT / "uv.lock").read_text()
    assert not re.search(r'^name = "nvidia-', lock, re.M)
    assert 'version = "' in lock and "+cpu" in lock
