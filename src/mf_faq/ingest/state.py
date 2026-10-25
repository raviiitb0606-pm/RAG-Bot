"""Refresh state bundles: carry the corpus and live index between runs (P7.2, P7.6).

GitHub Actions runners start empty, but the footer-date rule (arch §5.6) and the `stale`
fallback need the previous run's pages and meta. A bundle is a `.tar.gz` holding exactly:

    state.json               corpus/index version, embed model, chunk format, created at
    corpus/raw/<id>.html     the 5 pages, and their <id>.meta.json sidecars
    data/manifest.json       the corpus build's outputs
    data/chunks.jsonl
    data/refresh_status.json the stale-streak tracker (P7.5), when there is one
    data/index/<version>/    the live index version, and
    data/index/current       its pointer

plus a `<bundle>.sha256` file (`sha256sum` format). No `app.db` (threads) and no secrets.

`import_state` (the workflow) restores everything; `install_bundle_index` (the server's
`mf-faq pull-index`) installs only the index version. Both refuse a bundle whose checksum
doesn't match, whose members fall outside the list above (or are links), or whose
`index_meta.json` was built for another model, query instruction or chunk format: the same
load check as the server's (P3.3), run on the unpacked copy before anything goes live.

Round trips restore every file byte for byte except Chroma's own SQLite/HNSW files, which Chroma
rewrites whenever a client opens them (the verification does); their content is checked
through the `vectors_sha256` and `chunks_sha256` recorded at build time instead.
"""

import hashlib
import io
import json
import logging
import os
import shutil
import tarfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from mf_faq.ingest.build import CHUNKS_FILE, MANIFEST_FILE
from mf_faq.ingest.chunk import CHUNK_FORMAT_VERSION
from mf_faq.ingest.index import (
    CURRENT,
    META_FILE,
    IndexLoadError,
    current_version,
    index_root,
    install_version,
)
from mf_faq.logs import log_event

logger = logging.getLogger(__name__)

STATE_FILE = "state.json"
STATUS_FILE = "refresh_status.json"
STATE_FORMAT = 1
RAW_SUFFIXES = (".html", ".meta.json")
MAX_BUNDLE_BYTES = 1 << 30  # unpacked; today's bundle is a few MB
_RAW = "corpus/raw/"
_INDEX = "data/index/"
_DATA_FILES = frozenset({f"data/{MANIFEST_FILE}", f"data/{CHUNKS_FILE}", f"data/{STATUS_FILE}"})

GITHUB_API = "https://api.github.com"
DEFAULT_RELEASE = "data-latest"


class StateError(RuntimeError):
    """The bundle is unusable or doesn't match this code; nothing was changed."""


@dataclass(frozen=True)
class Imported:
    state: dict
    index_version: str
    swapped: bool


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def checksum_path(bundle: Path) -> Path:
    return bundle.with_name(bundle.name + ".sha256")


# -- export ------------------------------------------------------------------------------------


def _reset_owner(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def export_state(
    corpus_dir: Path, data_dir: Path, bundle: Path, *, now: datetime | None = None
) -> dict:
    """Write `bundle` and `<bundle>.sha256`; returns the bundle's `state.json`."""
    root = index_root(data_dir)
    version = current_version(data_dir)
    if version is None or not (root / version / META_FILE).exists():
        raise StateError(f"no live index under {root}; run `mf-faq ingest` first")
    meta = json.loads((root / version / META_FILE).read_text(encoding="utf-8"))
    manifest = json.loads((data_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    state = {
        "format": STATE_FORMAT,
        "corpus_version": meta["corpus_version"],
        "index_version": version,
        "data_corpus_version": manifest.get("corpus_version"),
        "embed_model": meta["embed_model"],
        "embed_model_revision": meta.get("embed_model_revision"),
        "query_instruction": meta["query_instruction"],
        "chunk_format_version": meta["chunk_format_version"],
        "created_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
    }

    raw = sorted(
        p for p in (corpus_dir / "raw").iterdir() if p.is_file() and p.name.endswith(RAW_SUFFIXES)
    )
    data_files = [data_dir / MANIFEST_FILE, data_dir / CHUNKS_FILE]
    if (data_dir / STATUS_FILE).exists():
        data_files.append(data_dir / STATUS_FILE)

    bundle.parent.mkdir(parents=True, exist_ok=True)
    tmp = bundle.with_name(bundle.name + ".tmp")
    with tarfile.open(tmp, "w:gz") as tar:
        body = (json.dumps(state, indent=2) + "\n").encode()
        info = _reset_owner(tarfile.TarInfo(STATE_FILE))
        info.size, info.mtime = len(body), int(datetime.now(UTC).timestamp())
        tar.addfile(info, io.BytesIO(body))
        for p in raw:
            tar.add(p, arcname=f"{_RAW}{p.name}", filter=_reset_owner)
        for p in data_files:
            tar.add(p, arcname=f"data/{p.name}", filter=_reset_owner)
        tar.add(root / version, arcname=f"{_INDEX}{version}", filter=_reset_owner)
        tar.add(root / CURRENT, arcname=f"{_INDEX}{CURRENT}", filter=_reset_owner)
    os.replace(tmp, bundle)
    checksum_path(bundle).write_text(f"{sha256_file(bundle)}  {bundle.name}\n", encoding="utf-8")
    log_event(logger, "state.exported", index_version=version, bytes=bundle.stat().st_size)
    return state


# -- import ------------------------------------------------------------------------------------


def verify_checksum(bundle: Path, checksum: Path | None = None) -> None:
    checksum = checksum or checksum_path(bundle)
    try:
        expected = checksum.read_text(encoding="utf-8").split()[0].lower()
    except (OSError, IndexError) as e:
        raise StateError(f"no usable checksum at {checksum}") from e
    if sha256_file(bundle) != expected:
        raise StateError(f"{bundle.name}: checksum mismatch (corrupt or tampered bundle)")


def _allowed(name: str, version: str | None) -> bool:
    if name == STATE_FILE or name in _DATA_FILES:
        return True
    if name.startswith(_RAW):
        rest = name[len(_RAW) :]
        return "/" not in rest and rest.endswith(RAW_SUFFIXES)
    if name == f"{_INDEX}{CURRENT}":
        return True
    return version is not None and (
        name == f"{_INDEX}{version}" or name.startswith(f"{_INDEX}{version}/")
    )


def _check_members(tar: tarfile.TarFile) -> str:
    """Every member is a plain file or dir on the allowlist; returns the bundle's version."""
    members = tar.getmembers()
    versions = {
        m.name[len(_INDEX) :].split("/", 1)[0]
        for m in members
        if m.name.startswith(_INDEX) and m.name != f"{_INDEX}{CURRENT}"
    }
    if len(versions) != 1:
        raise StateError(f"bundle must hold exactly one index version, found {sorted(versions)}")
    (version,) = versions
    total = 0
    for m in members:
        if not (m.isfile() or m.isdir()):
            raise StateError(f"bundle member {m.name!r} is not a plain file or directory")
        if m.name.startswith("/") or ".." in m.name.split("/") or not _allowed(m.name, version):
            raise StateError(f"unexpected bundle member {m.name!r}")
        total += m.size
    if total > MAX_BUNDLE_BYTES:
        raise StateError(f"bundle unpacks to {total} bytes, over the {MAX_BUNDLE_BYTES} limit")
    for required in (STATE_FILE, f"{_INDEX}{CURRENT}", f"{_INDEX}{version}/{META_FILE}"):
        if required not in {m.name for m in members}:
            raise StateError(f"bundle has no {required}")
    return version


@contextmanager
def _unpacked(bundle: Path, workdir: Path, checksum: Path | None = None):
    """Verify and unpack into a scratch dir inside `workdir` (same filesystem: moves are cheap)."""
    verify_checksum(bundle, checksum)
    workdir.mkdir(parents=True, exist_ok=True)
    # Not `.build-*`: installing prunes those, and the raw pages are copied out after it.
    scratch = workdir / f".unpack-{os.getpid()}"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir()
    try:
        try:
            with tarfile.open(bundle, "r:gz") as tar:
                version = _check_members(tar)
                tar.extractall(scratch, filter="data")
            state = json.loads((scratch / STATE_FILE).read_text(encoding="utf-8"))
            pointer = (scratch / _INDEX / CURRENT).read_text(encoding="utf-8").strip()
        except (OSError, ValueError, tarfile.TarError) as e:
            raise StateError(f"{bundle.name}: unreadable bundle: {e}") from e
        if state.get("format") != STATE_FORMAT:
            raise StateError(f"bundle format {state.get('format')!r}, expected {STATE_FORMAT}")
        if pointer != version or state.get("index_version") != version:
            raise StateError("bundle's current pointer, state.json and index folder disagree")
        yield scratch, state, version
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _install(data_dir: Path, scratch: Path, version: str, config: dict) -> bool:
    try:
        _, swapped = install_version(data_dir, scratch / _INDEX / version, **config)
    except IndexLoadError as e:
        raise StateError(f"bundle refused: {e}") from e
    return swapped


def _config(model_name: str, query_instruction: str, chunk_format_version: str) -> dict:
    return {
        "model_name": model_name,
        "query_instruction": query_instruction,
        "chunk_format_version": chunk_format_version,
    }


def import_state(
    bundle: Path,
    corpus_dir: Path,
    data_dir: Path,
    *,
    model_name: str,
    query_instruction: str,
    chunk_format_version: str = CHUNK_FORMAT_VERSION,
) -> Imported:
    """Restore a bundle (the workflow's first step). The index is checked before anything moves."""
    config = _config(model_name, query_instruction, chunk_format_version)
    with _unpacked(bundle, index_root(data_dir)) as (scratch, state, version):
        swapped = _install(data_dir, scratch, version, config)
        raw_dir = corpus_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        for p in sorted((scratch / _RAW).iterdir()) if (scratch / _RAW).is_dir() else []:
            shutil.copyfile(p, raw_dir / p.name)
        for name in sorted(_DATA_FILES):
            if (scratch / name).exists():
                shutil.copyfile(scratch / name, data_dir / Path(name).name)
    log_event(logger, "state.imported", index_version=version, swapped=swapped)
    return Imported(state, version, swapped)


def install_bundle_index(
    bundle: Path,
    data_dir: Path,
    *,
    model_name: str,
    query_instruction: str,
    chunk_format_version: str = CHUNK_FORMAT_VERSION,
    checksum: Path | None = None,
) -> Imported:
    """Install only the bundle's index version (the server side, P7.6)."""
    config = _config(model_name, query_instruction, chunk_format_version)
    with _unpacked(bundle, index_root(data_dir), checksum) as (scratch, state, version):
        swapped = _install(data_dir, scratch, version, config)
    return Imported(state, version, swapped)


# -- pull (server) -----------------------------------------------------------------------------


def _download(client: httpx.Client, url: str, dest: Path) -> None:
    with client.stream("GET", url, headers={"Accept": "application/octet-stream"}) as resp:
        resp.raise_for_status()
        with dest.open("wb") as f:
            for block in resp.iter_bytes():
                f.write(block)


def pull_index(
    data_dir: Path,
    *,
    repo: str,
    token: str,
    model_name: str,
    query_instruction: str,
    release: str = DEFAULT_RELEASE,
    transport: httpx.BaseTransport | None = None,
    on_download: Callable[[str], None] | None = None,
) -> Imported:
    """Download a release's bundle, verify it and make its index live (`mf-faq pull-index`)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "mf-faq-pull-index",
    }
    # Asset downloads redirect to another host; httpx drops the token on cross-host redirects.
    with httpx.Client(
        base_url=GITHUB_API,
        headers=headers,
        timeout=httpx.Timeout(60.0, connect=10.0),
        follow_redirects=True,
        transport=transport,
    ) as client:
        try:
            resp = client.get(f"/repos/{repo}/releases/tags/{release}")
            resp.raise_for_status()
            assets = {a["name"]: a for a in resp.json()["assets"]}
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            raise StateError(f"release {release!r} of {repo} not readable: {e}") from e
        bundles = sorted(n for n in assets if n.endswith(".tar.gz"))
        if len(bundles) != 1 or f"{bundles[0]}.sha256" not in assets:
            raise StateError(f"release {release!r} needs one .tar.gz bundle and its .sha256")
        name = bundles[0]
        root = index_root(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        workdir = root / f".pull-{os.getpid()}"
        shutil.rmtree(workdir, ignore_errors=True)
        workdir.mkdir()
        try:
            bundle, checksum = workdir / name, workdir / f"{name}.sha256"
            try:
                _download(client, assets[name]["url"], bundle)
                _download(client, assets[f"{name}.sha256"]["url"], checksum)
            except httpx.HTTPError as e:
                raise StateError(f"download of {name} failed: {e}") from e
            if on_download:
                on_download(name)
            result = install_bundle_index(
                bundle,
                data_dir,
                model_name=model_name,
                query_instruction=query_instruction,
                checksum=checksum,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    log_event(
        logger,
        "index.pulled",
        release=release,
        version=result.index_version,
        swapped=result.swapped,
    )
    return result
