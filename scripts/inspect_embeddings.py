"""Inspect the chunk embeddings stored in ChromaDB (Phase 3, P3.1).

The live index is a persistent ChromaDB at `data/index/<version>/chroma/`, collection `chunks`:
one record per chunk (id = chunk_id, its 384-d bge vector, metadata, document = the embedded
text). This script opens it through the same checked loader the server uses (hashes, model and
chunk format verified) and prints what Chroma holds. It never loads the embedding model.

Usage:
    uv run python scripts/inspect_embeddings.py                       # summary + one row per chunk
    uv run python scripts/inspect_embeddings.py --scheme hdfc-mid-cap # only one scheme
    uv run python scripts/inspect_embeddings.py --chunk groww-hdfc-mid-cap:exit_load
                                                  # full vector + its text + nearest chunks
    uv run python scripts/inspect_embeddings.py --csv embeddings.csv  # every vector, all dims
"""

import argparse
import csv
import sys
from pathlib import Path

import numpy as np

from mf_faq.config import get_settings
from mf_faq.ingest.index import IndexLoadError, LoadedIndex, load_index


def summary(index: LoadedIndex) -> None:
    meta, vectors = index.meta, index.vectors
    norms = np.linalg.norm(vectors, axis=1)
    print(f"index        {index.version}")
    print(
        f"chroma       {index.path / 'chroma'}  collection {index.collection.name!r}, "
        f"{index.collection.count()} records, {meta['hnsw_space']} space, "
        f"chromadb {meta['chroma_version']}"
    )
    print(f"model        {meta['embed_model']}@{(meta['embed_model_revision'] or '?')[:7]}")
    print(f"vectors      {vectors.shape[0]} chunks x {vectors.shape[1]} dims, {vectors.dtype}")
    print(f"norms        min {norms.min():.6f}  max {norms.max():.6f}  (normalised: dot = cosine)")
    lo, hi, mean = vectors.min(), vectors.max(), vectors.mean()
    print(f"values       min {lo:.4f}  max {hi:.4f}  mean {mean:.5f}")
    counts = {sid: int((index.scheme_ids == sid).sum()) for sid in dict.fromkeys(index.scheme_ids)}
    print("per scheme   " + ", ".join(f"{sid} {n}" for sid, n in counts.items()))
    print()


def table(index: LoadedIndex, rows: np.ndarray, dims: int) -> None:
    print(f"{'row':>3}  {'chunk_id':47} {'norm':>6}  first {dims} dims")
    for i in rows:
        chunk, vec = index.chunks[i], index.vectors[i]
        head = " ".join(f"{v:+.4f}" for v in vec[:dims])
        print(f"{i:3}  {chunk.chunk_id:47} {np.linalg.norm(vec):6.4f}  [{head} …]")
    print()


def show_chunk(index: LoadedIndex, chunk_id: str, neighbours: int) -> None:
    """One record exactly as Chroma stores it, and its neighbours by a Chroma query."""
    record = index.collection.get(ids=[chunk_id], include=["embeddings", "metadatas", "documents"])
    if not record["ids"]:
        sys.exit(f"unknown chunk id {chunk_id!r}; run without --chunk to list them")
    vec = np.asarray(record["embeddings"][0], dtype=np.float32)
    print(f"Chroma record {chunk_id}")
    print(f"metadata: {record['metadatas'][0]}")
    print("\ndocument (the embedded text):")
    print("  " + record["documents"][0].replace("\n", "\n  "))
    print(f"\nembedding ({vec.size} dims, norm {np.linalg.norm(vec):.6f}):")
    with np.printoptions(precision=5, suppress=True, linewidth=100, threshold=vec.size):
        print(vec)
    # Nearest records across all schemes (cosine space: distance = 1 - cosine).
    near = index.collection.query(
        query_embeddings=[vec], n_results=neighbours + 1, include=["distances", "metadatas"]
    )
    scheme = record["metadatas"][0]["scheme_id"]
    print(f"\nnearest {neighbours} records (Chroma query, all schemes):")
    pairs = zip(near["ids"][0], near["distances"][0], near["metadatas"][0], strict=True)
    for other, distance, meta in [p for p in pairs if p[0] != chunk_id][:neighbours]:
        same = "same scheme" if meta["scheme_id"] == scheme else ""
        print(f"  cosine {1 - distance:.4f}  {other:47} {same}")


def export_csv(index: LoadedIndex, path: Path) -> None:
    dim = index.vectors.shape[1]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["row", "chunk_id", "scheme_id", *(f"d{k}" for k in range(dim))])
        for i, (chunk, vec) in enumerate(zip(index.chunks, index.vectors, strict=True)):
            writer.writerow([i, chunk.chunk_id, chunk.scheme_id, *(f"{v:.7f}" for v in vec)])
    print(f"wrote {len(index.chunks)} vectors x {dim} dims to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scheme", help="only chunks of this scheme_id (e.g. hdfc-mid-cap)")
    parser.add_argument("--dims", type=int, default=6, help="dims shown per row (default 6)")
    parser.add_argument("--chunk", help="print one chunk's full vector, text and neighbours")
    parser.add_argument("--neighbours", type=int, default=5, help="neighbours with --chunk")
    parser.add_argument("--csv", type=Path, help="export every vector (all dims) to this CSV")
    parser.add_argument("--version", help="an index version other than the live one")
    args = parser.parse_args()

    settings = get_settings()
    try:
        index = load_index(
            settings.data_dir,
            model_name=settings.embed_model,
            query_instruction=settings.embed_query_instruction,
            version=args.version,
        )
    except IndexLoadError as e:
        sys.exit(f"Error: {e}")

    summary(index)
    if args.chunk:
        show_chunk(index, args.chunk, args.neighbours)
    elif args.csv:
        export_csv(index, args.csv)
    else:
        rows = np.arange(len(index.chunks))
        if args.scheme:
            rows = np.flatnonzero(index.scheme_ids == args.scheme)
            if not rows.size:
                sys.exit(f"no chunks for scheme {args.scheme!r}")
        table(index, rows, args.dims)


if __name__ == "__main__":
    main()
