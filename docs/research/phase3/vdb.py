import json, sys, time, shutil, statistics, os
from pathlib import Path
import numpy as np
sp = Path(sys.argv[1]); repo = Path(sys.argv[2])
rows = [json.loads(l) for l in open(repo / "data/chunks.jsonl")]
rng = np.random.default_rng(0)
V = rng.normal(size=(len(rows), 384)).astype(np.float32); V /= np.linalg.norm(V, axis=1, keepdims=True)
Q = rng.normal(size=(200, 384)).astype(np.float32); Q /= np.linalg.norm(Q, axis=1, keepdims=True)
res = {}
t = time.perf_counter(); import chromadb; res["chroma_import_s"] = round(time.perf_counter() - t, 2)
d = sp / "chroma_bench"; shutil.rmtree(d, ignore_errors=True)
t = time.perf_counter()
cl = chromadb.PersistentClient(path=str(d), settings=chromadb.Settings(anonymized_telemetry=False))
col = cl.create_collection("mf_chunks", metadata={"hnsw:space": "cosine"})
col.add(ids=[r["chunk_id"] for r in rows], embeddings=V.tolist(), metadatas=[{"scheme_id": r["scheme_id"]} for r in rows])
res["chroma_build_s"] = round(time.perf_counter() - t, 3)
lat = []
for q in Q:
    t = time.perf_counter(); col.query(query_embeddings=[q.tolist()], n_results=10, where={"scheme_id": "hdfc-mid-cap"}); lat.append((time.perf_counter() - t) * 1000)
res["chroma_query_ms_p50"] = round(statistics.median(lat), 3)
t = time.perf_counter(); cl2 = chromadb.PersistentClient(path=str(d), settings=chromadb.Settings(anonymized_telemetry=False)); cl2.get_collection("mf_chunks").count(); res["chroma_reopen_s"] = round(time.perf_counter() - t, 3)
res["chroma_disk_kb"] = round(sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 1024)
# exactness: chroma HNSW vs brute force, unfiltered top-10
agree = 0
for q in Q[:100]:
    ids = col.query(query_embeddings=[q.tolist()], n_results=10)["ids"][0]
    bf = [rows[i]["chunk_id"] for i in np.argsort(-(V @ q))[:10]]
    agree += ids == bf
res["chroma_top10_equal_bruteforce"] = f"{agree}/100"
# numpy flat
t = time.perf_counter(); np.save(sp / "vec.npy", V); res["numpy_build_s"] = round(time.perf_counter() - t, 4)
mask = np.array([r["scheme_id"] == "hdfc-mid-cap" for r in rows]); idx = np.where(mask)[0]
lat = []
for q in Q:
    t = time.perf_counter(); s = V[idx] @ q; idx[np.argsort(-s)[:10]]; lat.append((time.perf_counter() - t) * 1000)
res["numpy_query_ms_p50"] = round(statistics.median(lat), 4)
t = time.perf_counter(); np.load(sp / "vec.npy"); res["numpy_load_s"] = round(time.perf_counter() - t, 4)
res["numpy_disk_kb"] = round((sp / "vec.npy").stat().st_size / 1024)
print(json.dumps(res, indent=1))
