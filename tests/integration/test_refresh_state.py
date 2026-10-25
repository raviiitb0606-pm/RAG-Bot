"""P7.2 / P7.6: state bundle round trip, refusals, and the server's `pull-index`."""

import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest
from refresh_support import INSTRUCTION, make_workspace
from typer.testing import CliRunner

from conftest import FakeEmbedder
from mf_faq.cli import app
from mf_faq.config import get_settings
from mf_faq.ingest.index import (
    build_index,
    current_version,
    index_root,
    load_index,
    previous_version,
)
from mf_faq.ingest.state import (
    STATE_FILE,
    StateError,
    checksum_path,
    export_state,
    import_state,
    install_bundle_index,
    pull_index,
    sha256_file,
)

MODEL = "fake/model"


@pytest.fixture(scope="module")
def workspace(tmp_path_factory) -> tuple[Path, Path]:
    return make_workspace(tmp_path_factory.mktemp("ws"))


@pytest.fixture(scope="module")
def bundle(workspace, tmp_path_factory) -> Path:
    corpus, data = workspace
    out = tmp_path_factory.mktemp("bundle") / "refresh-state.tar.gz"
    export_state(corpus, data, out)
    return out


def tree(root: Path) -> dict[str, bytes]:
    files = sorted(p for p in root.rglob("*") if p.is_file())
    return {str(p.relative_to(root)): p.read_bytes() for p in files}


def restore(bundle: Path, base: Path, **config):
    return import_state(
        bundle,
        base / "corpus",
        base / "data",
        **({"model_name": MODEL, "query_instruction": INSTRUCTION} | config),
    )


def test_export_import_round_trip(workspace, bundle, tmp_path):
    corpus, data = workspace
    result = restore(bundle, tmp_path)
    version = current_version(data)
    assert result.index_version == version and result.swapped
    assert current_version(tmp_path / "data") == version

    assert tree(tmp_path / "corpus" / "raw") == tree(corpus / "raw")
    for name in ("manifest.json", "chunks.jsonl"):
        assert (tmp_path / "data" / name).read_bytes() == (data / name).read_bytes()
    # Same files; same bytes except Chroma's own (it rewrites its SQLite and HNSW files whenever
    # a client opens them, as the import's verification does). Its vectors are checked instead.
    restored, live = tree(index_root(tmp_path / "data") / version), tree(index_root(data) / version)
    assert restored.keys() == live.keys()
    assert {k: v for k, v in restored.items() if not k.startswith("chroma/")} == {
        k: v for k, v in live.items() if not k.startswith("chroma/")
    }
    index = load_index(tmp_path / "data", model_name=MODEL)  # chunks + vectors hashes match
    assert index.version == version and index.meta["vectors_sha256"]
    index.close()

    state = result.state
    assert state["index_version"] == version and state["embed_model"] == MODEL
    assert state["chunk_format_version"] == "2" and state["query_instruction"] == INSTRUCTION
    # Nothing beyond the allowlist: no app.db (threads), no .env, no leftovers.
    with tarfile.open(bundle) as tar:
        names = tar.getnames()
    assert not [n for n in names if n.endswith(("app.db", ".env")) or "/." in n]
    assert checksum_path(bundle).read_text().split() == [sha256_file(bundle), bundle.name]

    again = restore(bundle, tmp_path)  # importing the live version again is a no-op
    assert not again.swapped and current_version(tmp_path / "data") == version


def test_tampered_bundle_is_refused(bundle, tmp_path):
    bad = tmp_path / bundle.name
    raw = bytearray(bundle.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    bad.write_bytes(bytes(raw))
    checksum_path(bad).write_bytes(checksum_path(bundle).read_bytes())
    with pytest.raises(StateError, match="checksum mismatch"):
        restore(bad, tmp_path)
    checksum_path(bad).unlink()
    with pytest.raises(StateError, match="no usable checksum"):
        restore(bad, tmp_path)
    assert not (tmp_path / "corpus").exists() and current_version(tmp_path / "data") is None


@pytest.mark.parametrize(
    "config, field",
    [
        ({"model_name": "BAAI/bge-small-en-v1.5"}, "embed_model"),
        ({"chunk_format_version": "1"}, "chunk_format_version"),
        ({"query_instruction": "query: "}, "query_instruction"),
    ],
)
def test_bundle_for_another_config_is_refused(bundle, tmp_path, config, field):
    with pytest.raises(StateError, match=field):
        restore(bundle, tmp_path, **config)
    assert not (tmp_path / "corpus" / "raw").exists()  # refused before anything moved
    assert current_version(tmp_path / "data") is None


def _repack(bundle: Path, out: Path, extra: tarfile.TarInfo, body: bytes = b"") -> Path:
    with tarfile.open(bundle) as src, tarfile.open(out, "w:gz") as dst:
        for m in src.getmembers():
            dst.addfile(m, src.extractfile(m) if m.isfile() else None)
        dst.addfile(extra, io.BytesIO(body) if body else None)
    checksum_path(out).write_text(f"{sha256_file(out)}  {out.name}\n")
    return out


@pytest.mark.parametrize("kind", ["traversal", "symlink", "secret"])
def test_unexpected_members_are_refused(bundle, tmp_path, kind):
    if kind == "traversal":
        info = tarfile.TarInfo("corpus/raw/../../evil.html")
    elif kind == "symlink":
        info = tarfile.TarInfo("corpus/raw/link.html")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
    else:
        info = tarfile.TarInfo("data/app.db")
    info.size = 0
    bad = _repack(bundle, tmp_path / "bad.tar.gz", info)
    with pytest.raises(StateError, match="unexpected bundle member|not a plain file"):
        restore(bad, tmp_path / "out")
    assert not (tmp_path / "evil.html").exists()


def test_import_keeps_the_replaced_version_as_previous(bundle, tmp_path):
    """The server's install path: a new version goes live, the old one stays for rollback."""
    base = tmp_path / "server"
    restore(bundle, base)
    old = current_version(base / "data")

    other = make_workspace(tmp_path / "other")
    assert build_index(other[1], FakeEmbedder("fake/model"), query_instruction="x: ").swapped
    newer = tmp_path / "newer.tar.gz"
    export_state(*other, newer)
    with pytest.raises(StateError, match="query_instruction"):
        install_bundle_index(newer, base / "data", model_name=MODEL, query_instruction=INSTRUCTION)
    assert current_version(base / "data") == old

    # Same config, different chunks: a genuinely new version.
    lines = (other[1] / "chunks.jsonl").read_text().splitlines()
    row = json.loads(lines[0])
    row["text"] += " (changed)"
    (other[1] / "chunks.jsonl").write_text("\n".join([json.dumps(row), *lines[1:]]) + "\n")
    new = build_index(other[1], FakeEmbedder(), query_instruction=INSTRUCTION).version
    export_state(*other, newer)
    result = install_bundle_index(
        newer, base / "data", model_name=MODEL, query_instruction=INSTRUCTION
    )
    assert result.swapped and result.index_version == new != old
    assert current_version(base / "data") == new and previous_version(base / "data") == old
    leftovers = [p.name for p in index_root(base / "data").iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_cli_state_export_and_import(workspace, tmp_path, monkeypatch):
    corpus, data = workspace
    out = tmp_path / "s.tar.gz"
    monkeypatch.setenv("MF_FAQ_CORPUS_DIR", str(corpus))
    monkeypatch.setenv("MF_FAQ_DATA_DIR", str(data))
    monkeypatch.setenv("MF_FAQ_EMBED_MODEL", MODEL)
    get_settings.cache_clear()
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["state", "export", str(out)])
        assert result.exit_code == 0, result.output
        assert out.exists() and checksum_path(out).exists()

        monkeypatch.setenv("MF_FAQ_CORPUS_DIR", str(tmp_path / "c"))
        monkeypatch.setenv("MF_FAQ_DATA_DIR", str(tmp_path / "d"))
        get_settings.cache_clear()
        result = runner.invoke(app, ["state", "import", str(out)])
        assert result.exit_code == 0, result.output
        assert current_version(tmp_path / "d") == current_version(data)

        monkeypatch.setenv("MF_FAQ_EMBED_MODEL", "fake/other")
        get_settings.cache_clear()
        result = runner.invoke(app, ["state", "import", str(out)])
        assert result.exit_code == 1 and "embed_model" in result.output
    finally:
        get_settings.cache_clear()


class Releases:
    """Mock GitHub API: one release whose assets are files on disk."""

    def __init__(self, files: dict[str, Path], tag: str = "data-latest") -> None:
        self.files, self.tag = files, tag
        self.auth: dict[str, str | None] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.auth[f"{request.url.host}{request.url.path}"] = request.headers.get("authorization")
        if request.url.host == "api.github.com":
            if request.url.path == f"/repos/o/r/releases/tags/{self.tag}":
                assets = [
                    {"name": n, "url": f"https://api.github.com/repos/o/r/releases/assets/{i}"}
                    for i, n in enumerate(self.files)
                ]
                return httpx.Response(200, json={"tag_name": self.tag, "assets": assets})
            if "/releases/assets/" in request.url.path:
                assert request.headers["accept"] == "application/octet-stream"
                i = int(request.url.path.rsplit("/", 1)[1])
                return httpx.Response(
                    302, headers={"location": f"https://objects.example.com/blob/{i}"}
                )
            return httpx.Response(404, json={"message": "Not Found"})
        i = int(request.url.path.rsplit("/", 1)[1])  # the storage host behind the redirect
        return httpx.Response(200, content=list(self.files.values())[i].read_bytes())


def pull(data: Path, releases: Releases, **kw):
    return pull_index(
        data,
        repo="o/r",
        token="ghp_test",
        model_name=MODEL,
        query_instruction=INSTRUCTION,
        transport=httpx.MockTransport(releases),
        **kw,
    )


def test_pull_index_installs_the_release(workspace, bundle, tmp_path):
    releases = Releases({bundle.name: bundle, f"{bundle.name}.sha256": checksum_path(bundle)})
    data = tmp_path / "data"
    result = pull(data, releases)
    assert result.swapped and current_version(data) == current_version(workspace[1])
    assert load_index(data, model_name=MODEL).version == result.index_version
    assert not (data.parent / "corpus").exists()  # the server only takes the index
    # The token goes to the API, never to the storage host the asset redirects to.
    assert releases.auth["api.github.com/repos/o/r/releases/tags/data-latest"] == "Bearer ghp_test"
    assert all(v is None for k, v in releases.auth.items() if k.startswith("objects."))
    assert not pull(data, releases).swapped  # already live

    with pytest.raises(StateError, match="not readable"):
        pull(data, releases, release="data-2026-01-01.missing")


def test_pull_index_refuses_a_bad_checksum(bundle, tmp_path):
    wrong = tmp_path / "wrong.sha256"
    wrong.write_text("0" * 64 + "  x\n")
    releases = Releases({bundle.name: bundle, f"{bundle.name}.sha256": wrong})
    with pytest.raises(StateError, match="checksum mismatch"):
        pull(tmp_path / "data", releases)
    assert current_version(tmp_path / "data") is None
    assert not [p for p in index_root(tmp_path / "data").iterdir()]  # no leftovers


def test_export_needs_a_live_index(tmp_path):
    with pytest.raises(StateError, match="no live index"):
        export_state(tmp_path / "corpus", tmp_path / "data", tmp_path / "b.tar.gz")


def test_state_file_is_first(bundle):
    with tarfile.open(bundle) as tar:
        assert tar.getnames()[0] == STATE_FILE
