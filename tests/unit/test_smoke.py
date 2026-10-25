import importlib
import pkgutil

from typer.testing import CliRunner

import mf_faq
from mf_faq.cli import app


def test_all_modules_import():
    for mod in pkgutil.walk_packages(mf_faq.__path__, prefix="mf_faq."):
        importlib.import_module(mod.name)


def test_cli_help_lists_three_commands():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("ingest", "serve", "eval"):
        assert command in result.output


def test_placeholder_commands_fail_loudly():
    result = CliRunner().invoke(app, ["eval"])  # the full eval comes in Phase 9
    assert result.exit_code == 1


def test_serve_fails_cleanly_without_an_index(monkeypatch):
    import mf_faq.api.app
    from mf_faq.ingest.index import IndexLoadError

    def no_index(*args, **kwargs):
        raise IndexLoadError("no live index")

    monkeypatch.setattr(mf_faq.api.app, "create_app", no_index)
    result = CliRunner().invoke(app, ["serve"])
    assert result.exit_code == 1
    assert "no live index" in result.output
