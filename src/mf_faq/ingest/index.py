"""Embed the chunks into a ChromaDB collection; versioned directories, atomic swap (P3.1).

Input is what the Phase 2 build wrote: `data/chunks.jsonl` and `data/manifest.json`.
Output (arch §5.5, §5.7):

    data/index/<version>/   chunks.jsonl, chroma/ (persistent ChromaDB), index_meta.json,
                            manifest.json
    data/index/current      pointer file: the name of the live version
    data/index/previous     pointer file: the version it replaced, kept for rollback

The vector store is a local, embedded **ChromaDB** (owner decision, 25 Oct 2026; it replaced
the NumPy flat file of the Phase 3 research). Each version has its own `PersistentClient` folder
holding one collection, `chunks`: one record per chunk (id = `chunk_id`, our own bge vectors,
cosine space, metadata `row` / `scheme_id` / `source_id`, document = `Chunk.content`). Chroma
never embeds anything itself (no embedding function), and its telemetry is off.

`<version>` is `<corpus_version>-<8 hex>`, the hex being a hash of everything that shapes the
vectors (chunks file, model and revision, chunk format, query instruction). The corpus version
alone only hashes the page *facts*, so a new model or chunk wording would otherwise reuse the
live directory name. The same inputs give the same version, and rebuilding it is a no-op.

A build writes into a temporary directory, runs the gates on it (no chunk over the model's
token limit, the round-trip load below, plus any extra gates: the CLI adds the P3.5 retrieval
eval), closes its Chroma client, and only then renames it into place and repoints `current`.
A failed or interrupted build leaves `current` untouched (IDX-01, IDX-02).

`LoadedIndex.vectors` is the collection's embeddings read back at load (float32, rows in
`chunks.jsonl` order) and checked against the hash recorded at build time. Chroma normalises
vectors on insert, which moves some values by one float32 step, so the hash is taken over what
Chroma returns, not over the model output. BM25 is **not** stored: the retriever rebuilds it
from `chunks.jsonl` at load (P3.3), so nothing here is unpickled.
"""

import hashlib
import json
import logging
import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from chromadb.config import Settings as ChromaSettings

from mf_faq.ingest.build import CHUNKS_FILE, MANIFEST_FILE, load_chunks
from mf_faq.ingest.chunk import CHUNK_FORMAT_VERSION, Chunk
from mf_faq.logs import log_event

logger = logging.getLogger(__name__)

INDEX_DIR = "index"
CHROMA_DIR = "chroma"
COLLECTION = "chunks"
HNSW_SPACE = "cosine"
META_FILE = "index_meta.json"
CURRENT = "current"
PREVIOUS = "previous"
_TMP_PREFIX = ".build-"
_NORM_TOLERANCE = 1e-3

# A gate gets the freshly built (not yet live) index and returns its failures.
IndexGate = Callable[["LoadedIndex"], list[str]]


class IndexBuildError(RuntimeError):
    """The index can't be built from the current inputs; `current` is unchanged."""


class IndexLoadError(RuntimeError):
    """No usable index: missing, corrupt, or built for another model / chunk format."""


class Embedder(Protocol):
    model_name: str
    revision: str | None
    dimension: int
    max_seq_length: int

    def encode(self, texts: list[str]) -> np.ndarray:
        """Normalised float32 vectors, one row per text, the text embedded as given."""
        ...

    def count_tokens(self, text: str) -> int:
        """Tokens the model sees for `text`, special tokens included."""
        ...


class SentenceTransformerEmbedder:
    """The real embedder: a `sentence-transformers` model, loaded from the local HF cache."""

    def __init__(
        self, model_name: str, *, revision: str | None = None, batch_size: int = 16
    ) -> None:
        from sentence_transformers import SentenceTransformer

        try:
            self._model = SentenceTransformer(model_name, revision=revision)
        except Exception as e:  # any load failure gets the same advice (IDX-05)
            raise IndexBuildError(
                f"could not load embedding model {model_name!r}: {e}. The model is downloaded "
                "once on first use, so run this with network access, or set MF_FAQ_EMBED_MODEL "
                "to a local copy (and MF_FAQ_EMBED_MODEL_REVISION to a revision it has)."
            ) from e
        self.model_name = model_name
        self.revision = self._model.model_card_data.base_model_revision
        self.dimension = int(self._model.get_embedding_dimension())
        self.max_seq_length = int(self._model.max_seq_length)
        self._batch_size = batch_size

    def encode(self, texts: list[str]) -> np.ndarray:
        return self._model.encode(
            texts,
            batch_size=self._batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        ).astype(np.float32)

    def count_tokens(self, text: str) -> int:
        return len(self._model.tokenizer(text, verbose=False)["input_ids"])


def make_embedder(model_name: str, revision: str | None = None) -> Embedder:
    """Factory used by the CLI; tests replace it with a fake."""
    return SentenceTransformerEmbedder(model_name, revision=revision)


def embedder_from(settings) -> Embedder:
    """The configured model at its pinned revision (`MF_FAQ_EMBED_MODEL_REVISION`)."""
    if settings.embed_model_revision:
        return make_embedder(settings.embed_model, revision=settings.embed_model_revision)
    return make_embedder(settings.embed_model)


@dataclass(frozen=True)
class LoadedIndex:
    version: str
    path: Path
    meta: dict
    chunks: list[Chunk]
    vectors: np.ndarray  # (n, dim) float32, read-only: the collection's embeddings, row order
    scheme_ids: np.ndarray  # (n,) str, the scheme of each row (P3.3)
    collection: Any = field(repr=False)  # chromadb Collection, queried by the retriever
    client: Any = field(default=None, repr=False)  # its PersistentClient; None when not owned

    def close(self) -> None:
        """Release the Chroma client (its SQLite files) so the folder can be moved or deleted."""
        if self.client is not None:
            self.client.close()


def chroma_settings() -> ChromaSettings:
    """A fresh settings object: Chroma refuses a second client on a path with other settings."""
    return ChromaSettings(anonymized_telemetry=False, allow_reset=False)


def open_client(path: Path):
    import chromadb

    return chromadb.PersistentClient(path=str(path), settings=chroma_settings())


def write_collection(client, chunks: list[Chunk], vectors: np.ndarray, name: str = COLLECTION):
    """One record per chunk, our own vectors, cosine space; Chroma embeds nothing itself."""
    collection = client.create_collection(
        name, configuration={"hnsw": {"space": HNSW_SPACE}}, embedding_function=None
    )
    collection.add(
        ids=[c.chunk_id for c in chunks],
        embeddings=vectors,
        metadatas=[
            {"row": i, "scheme_id": c.scheme_id, "source_id": c.source_id}
            for i, c in enumerate(chunks)
        ],
        documents=[c.content for c in chunks],
    )
    return collection


def read_vectors(collection, chunks: list[Chunk]) -> np.ndarray:
    """The stored embeddings as float32, in `chunks` order; KeyError if a chunk is missing."""
    ids = [c.chunk_id for c in chunks]
    got = collection.get(ids=ids, include=["embeddings"])
    position = {chunk_id: i for i, chunk_id in enumerate(got["ids"])}
    embeddings = np.asarray(got["embeddings"], dtype=np.float32)
    return np.ascontiguousarray(embeddings[[position[i] for i in ids]])


@dataclass
class IndexBuildResult:
    version: str
    failures: list[str] = field(default_factory=list)
    swapped: bool = False  # False when the build failed or was already live
    meta: dict | None = None

    @property
    def ok(self) -> bool:
        return not self.failures


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _vectors_sha256(vectors: np.ndarray) -> str:
    return _sha256(np.ascontiguousarray(vectors, dtype=np.float32).tobytes())


def index_root(data_dir: Path) -> Path:
    return data_dir / INDEX_DIR


def _read_pointer(root: Path, name: str) -> str | None:
    try:
        value = (root / name).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return value or None


def _write_pointer(root: Path, name: str, version: str) -> None:
    tmp = root / f".{name}.tmp"
    tmp.write_text(version + "\n", encoding="utf-8")
    os.replace(tmp, root / name)  # atomic: readers see the old or the new name, never neither


def current_version(data_dir: Path) -> str | None:
    return _read_pointer(index_root(data_dir), CURRENT)


def previous_version(data_dir: Path) -> str | None:
    return _read_pointer(index_root(data_dir), PREVIOUS)


def load_index(
    data_dir: Path,
    *,
    model_name: str,
    chunk_format_version: str = CHUNK_FORMAT_VERSION,
    query_instruction: str | None = None,
    version: str | None = None,
) -> LoadedIndex:
    """Load the live index (or `version`) and check it is whole and built for this config.

    Refuses (IndexLoadError) when there is no index yet (IDX-04), when the model, chunk format or
    query instruction differs from the caller's config (IDX-06; τ is specific to all three), or
    when a file doesn't match the hashes recorded at build time (IDX-03).
    """
    root = index_root(data_dir)
    version = version or _read_pointer(root, CURRENT)
    if version is None:
        raise IndexLoadError(f"no index at {root}; run `mf-faq ingest` first")
    return _load_dir(
        root / version,
        model_name=model_name,
        chunk_format_version=chunk_format_version,
        query_instruction=query_instruction,
    )


def _load_dir(
    path: Path, *, model_name: str, chunk_format_version: str, query_instruction: str | None
) -> LoadedIndex:
    try:
        meta = json.loads((path / META_FILE).read_text(encoding="utf-8"))
        chunks_bytes = (path / CHUNKS_FILE).read_bytes()
        chunks = load_chunks(path)
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise IndexLoadError(f"index {path.name} is unreadable: {e}") from e

    expected = {
        "embed_model": model_name,
        "chunk_format_version": chunk_format_version,
    }
    if query_instruction is not None:
        expected["query_instruction"] = query_instruction
    for key, want in expected.items():
        if meta.get(key) != want:
            raise IndexLoadError(
                f"index {path.name} was built with {key}={meta.get(key)!r}, but the config says "
                f"{want!r}; rebuild it with `mf-faq index`"
            )

    if not (path / CHROMA_DIR).is_dir():
        raise IndexLoadError(f"index {path.name} is unreadable: no {CHROMA_DIR}/ folder")
    client = open_client(path / CHROMA_DIR)
    try:
        try:
            collection = client.get_collection(COLLECTION, embedding_function=None)
            count = collection.count()
            vectors = read_vectors(collection, chunks)
        except Exception as e:  # missing collection, missing ids, corrupt store
            raise IndexLoadError(f"index {path.name}: Chroma collection unreadable: {e}") from e

        problems = []
        if _sha256(chunks_bytes) != meta.get("chunks_sha256"):
            problems.append("chunks.jsonl does not match its recorded hash")
        if count != meta.get("chunk_count") or len(chunks) != meta.get("chunk_count"):
            problems.append(
                f"{count} vectors and {len(chunks)} chunks, meta says {meta.get('chunk_count')}"
            )
        if vectors.shape != (meta.get("chunk_count"), meta.get("dim")):
            problems.append(f"vectors are {vectors.shape}, meta says otherwise")
        elif _vectors_sha256(vectors) != meta.get("vectors_sha256"):
            problems.append("Chroma vectors do not match their recorded hash")
        if problems:
            raise IndexLoadError(f"index {path.name} is inconsistent: " + "; ".join(problems))
    except IndexLoadError:
        client.close()
        raise

    vectors.setflags(write=False)  # shared read-only across requests (arch §8.2)
    return LoadedIndex(
        version=path.name,
        path=path,
        meta=meta,
        chunks=chunks,
        vectors=vectors,
        scheme_ids=np.array([c.scheme_id for c in chunks]),
        collection=collection,
        client=client,
    )


def _chroma_version() -> str:
    import chromadb

    return chromadb.__version__


def _check_vectors(vectors: np.ndarray, n: int, dim: int) -> list[str]:
    if vectors.ndim != 2 or vectors.shape != (n, dim):
        return [f"embedder returned shape {vectors.shape}, expected ({n}, {dim})"]
    if not np.isfinite(vectors).all():
        return ["embedder returned NaN or infinite values"]
    norms = np.linalg.norm(vectors, axis=1)
    bad = np.flatnonzero(np.abs(norms - 1.0) > _NORM_TOLERANCE)
    if bad.size:
        return [f"{bad.size} vectors are not unit length (cosine needs normalised vectors)"]
    return []


def _prune(root: Path, keep: set[str]) -> None:
    """Remove leftover build dirs and every version other than `keep` (current + previous)."""
    for child in root.iterdir():
        if not child.is_dir() or child.name in keep:
            continue
        if child.name.startswith(_TMP_PREFIX) or (child / META_FILE).exists():
            shutil.rmtree(child, ignore_errors=True)


def build_index(
    data_dir: Path,
    embedder: Embedder,
    *,
    query_instruction: str,
    gates: Sequence[IndexGate] = (),
    now: datetime | None = None,
) -> IndexBuildResult:
    """Embed `data/chunks.jsonl` into a new version and make it live if every gate passes.

    Raises IndexBuildError when the inputs are unusable (missing, or written by older code);
    returns a result with `failures` when the new index fails a gate.
    """
    try:
        manifest = json.loads((data_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
        chunks_bytes = (data_dir / CHUNKS_FILE).read_bytes()
        chunks = load_chunks(data_dir)
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise IndexBuildError(f"no usable chunks in {data_dir}: {e}; run `mf-faq ingest`") from e
    if manifest.get("chunk_format_version") != CHUNK_FORMAT_VERSION:
        raise IndexBuildError(
            f"{data_dir / CHUNKS_FILE} has chunk format "
            f"{manifest.get('chunk_format_version')!r}, this code writes {CHUNK_FORMAT_VERSION!r}; "
            "re-run `mf-faq ingest --skip-fetch`"
        )
    if manifest.get("chunk_count") != len(chunks) or not chunks:
        raise IndexBuildError(
            f"manifest says {manifest.get('chunk_count')} chunks, {CHUNKS_FILE} has {len(chunks)}"
        )
    ids = [c.chunk_id for c in chunks]
    if len(set(ids)) != len(ids):
        raise IndexBuildError("duplicate chunk ids in chunks.jsonl")

    chunks_sha256 = _sha256(chunks_bytes)
    inputs = {
        "chunks_sha256": chunks_sha256,
        "embed_model": embedder.model_name,
        "embed_model_revision": embedder.revision,
        "chunk_format_version": CHUNK_FORMAT_VERSION,
        "query_instruction": query_instruction,
        "vector_store": "chromadb",
    }
    index_key = _sha256(json.dumps(inputs, sort_keys=True).encode())[:8]
    version = f"{manifest['corpus_version']}-{index_key}"
    root = index_root(data_dir)
    root.mkdir(parents=True, exist_ok=True)

    live = _read_pointer(root, CURRENT)
    if live == version:
        try:
            existing = _load_dir(
                root / version,
                model_name=embedder.model_name,
                chunk_format_version=CHUNK_FORMAT_VERSION,
                query_instruction=query_instruction,
            )
            existing.close()
            meta = existing.meta
        except IndexLoadError:
            pass  # the live copy is damaged: rebuild it below
        else:
            log_event(logger, "index.unchanged", version=version)
            return IndexBuildResult(version, meta=meta)

    vectors = embedder.encode([c.content for c in chunks])
    failures = _check_vectors(vectors, len(chunks), embedder.dimension)
    if failures:
        return _fail(version, failures)
    vectors = np.ascontiguousarray(vectors, dtype=np.float32)

    # Real token counts: the model silently truncates past max_seq_length, so a chunk over it
    # would be searched by its head only. Chunking splits by this tokenizer (P3.0); this gate
    # catches chunks written with another tokenizer or budget.
    tokens = [embedder.count_tokens(c.content) for c in chunks]
    truncated = [
        c.chunk_id for c, t in zip(chunks, tokens, strict=True) if t > embedder.max_seq_length
    ]
    if truncated:
        return _fail(
            version,
            [
                f"{len(truncated)} chunks exceed {embedder.model_name}'s max_seq_length "
                f"{embedder.max_seq_length} and would be truncated: {', '.join(truncated)}; "
                "re-run `mf-faq ingest --skip-fetch` to re-split them"
            ],
        )

    meta = {
        "version": version,
        "corpus_version": manifest["corpus_version"],
        "built_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        **inputs,
        "dim": embedder.dimension,
        "max_seq_length": embedder.max_seq_length,
        "normalized": True,
        "chunk_count": len(chunks),
        "max_chunk_tokens": max(tokens),
        "chroma_version": _chroma_version(),
        "collection": COLLECTION,
        "hnsw_space": HNSW_SPACE,
    }

    tmp = root / f"{_TMP_PREFIX}{version}-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)  # a leftover from a crashed build
    tmp.mkdir()
    try:
        (tmp / CHUNKS_FILE).write_bytes(chunks_bytes)
        shutil.copyfile(data_dir / MANIFEST_FILE, tmp / MANIFEST_FILE)
        client = open_client(tmp / CHROMA_DIR)
        try:
            collection = write_collection(client, chunks, vectors)
            # Hash what Chroma hands back (it normalises on insert), so loads can verify it.
            meta["vectors_sha256"] = _vectors_sha256(read_vectors(collection, chunks))
        finally:
            client.close()
        (tmp / META_FILE).write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")

        # Gates run on the files as written, through the same loader the server uses.
        try:
            candidate = _load_dir(
                tmp,
                model_name=embedder.model_name,
                chunk_format_version=CHUNK_FORMAT_VERSION,
                query_instruction=query_instruction,
            )
        except IndexLoadError as e:
            return _fail(version, [str(e)])
        try:
            failures = [f for gate in gates for f in gate(candidate)]
        finally:
            candidate.close()  # Chroma must let go of the folder before it is renamed
        if failures:
            return _fail(version, failures)

        final = root / version
        if final.exists():  # an older, non-live copy of the same version (e.g. `previous`)
            shutil.rmtree(final)
        os.replace(tmp, final)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)

    if live and live != version:
        _write_pointer(root, PREVIOUS, live)
    _write_pointer(root, CURRENT, version)
    _prune(root, {version, _read_pointer(root, PREVIOUS) or ""})
    log_event(
        logger,
        "index.built",
        version=version,
        chunks=len(chunks),
        dim=embedder.dimension,
        model=embedder.model_name,
        previous=live,
    )
    return IndexBuildResult(version, swapped=True, meta=meta)


def _fail(version: str, failures: list[str]) -> IndexBuildResult:
    for failure in failures:
        log_event(logger, "index.gate_failed", level=logging.ERROR, failure=failure)
    return IndexBuildResult(version, failures=failures)


def verify_dir(
    path: Path,
    *,
    model_name: str,
    query_instruction: str | None,
    chunk_format_version: str = CHUNK_FORMAT_VERSION,
) -> dict:
    """Run the full load check on a version directory (IDX-03, IDX-06); returns its meta."""
    index = _load_dir(
        path,
        model_name=model_name,
        chunk_format_version=chunk_format_version,
        query_instruction=query_instruction,
    )
    index.close()
    return index.meta


def install_version(
    data_dir: Path,
    src: Path,
    *,
    model_name: str,
    query_instruction: str | None,
    chunk_format_version: str = CHUNK_FORMAT_VERSION,
) -> tuple[str, bool]:
    """Make an unpacked version directory live (P7.2 import, P7.6 pull): verify, move, swap.

    `src` must be on the same filesystem as `data/index/` (it is moved, not copied). The live
    version moves to `previous`, so `mf-faq index --rollback` still works; older versions are
    pruned. Returns `(version, swapped)`; `swapped` is False when it was already live.
    """
    meta = verify_dir(
        src,
        model_name=model_name,
        query_instruction=query_instruction,
        chunk_format_version=chunk_format_version,
    )
    version = meta.get("version")
    if not version or version.startswith(".") or "/" in version:
        raise IndexLoadError(f"index_meta.json in {src} has no usable version name")
    root = index_root(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    live = _read_pointer(root, CURRENT)
    if live == version and (root / version / META_FILE).exists():
        try:
            verify_dir(
                root / version,
                model_name=model_name,
                query_instruction=query_instruction,
                chunk_format_version=chunk_format_version,
            )
        except IndexLoadError:
            pass  # the live copy is damaged: replace it below
        else:
            return version, False

    final = root / version
    staged = root / f"{_TMP_PREFIX}install-{version}-{os.getpid()}"
    shutil.rmtree(staged, ignore_errors=True)
    os.replace(src, staged)
    if final.exists():  # a non-live (or damaged live) copy of the same version
        shutil.rmtree(final)
    os.replace(staged, final)
    if live and live != version:
        _write_pointer(root, PREVIOUS, live)
    _write_pointer(root, CURRENT, version)
    _prune(root, {version, _read_pointer(root, PREVIOUS) or ""})
    log_event(logger, "index.installed", version=version, previous=live)
    return version, True


def rollback_index(data_dir: Path) -> str:
    """Make `previous` live again (and the current version the new `previous`)."""
    root = index_root(data_dir)
    current, previous = _read_pointer(root, CURRENT), _read_pointer(root, PREVIOUS)
    if not previous or not (root / previous / META_FILE).exists():
        raise IndexLoadError("no previous index version to roll back to")
    if not (root / previous / CHROMA_DIR).is_dir():
        raise IndexLoadError(f"previous version {previous} has no ChromaDB store; rebuild instead")
    _write_pointer(root, CURRENT, previous)
    if current:
        _write_pointer(root, PREVIOUS, current)
    log_event(logger, "index.rolled_back", version=previous, replaced=current)
    return previous
