"""Phase 3 retrieval experiments over data/chunks.jsonl. Usage: python harness.py <repo> <model> [variant]"""

import json
import re
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import yaml
from rank_bm25 import BM25Okapi

sys.path.insert(0, str(Path(__file__).parent))
from queries import (  # noqa: E402
    FACT_GROUP, HELDOUT, HOLDINGS_DEEP, OOC_GENERAL, OOC_NEAR_MISS, OOC_SCHEME, PARAPHRASES,
)

REPO = Path(sys.argv[1])
MODEL = sys.argv[2]
VARIANT = sys.argv[3] if len(sys.argv) > 3 else "header+text"

PREFIX = {  # (query prefix, passage prefix) from each model card
    "BAAI/bge-small-en-v1.5": ("Represent this sentence for searching relevant passages: ", ""),
    "BAAI/bge-base-en-v1.5": ("Represent this sentence for searching relevant passages: ", ""),
    "BAAI/bge-large-en-v1.5": ("Represent this sentence for searching relevant passages: ", ""),
    "intfloat/e5-small-v2": ("query: ", "passage: "),
    "Snowflake/snowflake-arctic-embed-s": ("Represent this sentence for searching relevant passages: ", ""),
}
NO_INSTR = "noinstr" in VARIANT

chunks = [json.loads(line) for line in open(REPO / "data/chunks.jsonl")]
schemes = {s["scheme_id"]: s for s in yaml.safe_load(open(REPO / "corpus/schemes.yaml"))["schemes"]}
group = lambda c: c["chunk_id"].split(":")[1]  # noqa: E731


SYN = {  # section descriptions (no fact values): what a reader would call this section
    "expense_ratio": "expense ratio, TER, total expense ratio, annual fee, yearly cost of holding the fund",
    "exit_load": "exit load, redemption charge, penalty or fee for withdrawing or selling units early",
    "minimum_investments": "minimum investment, minimum SIP, lump sum, first investment, additional top-up, amount needed to start",
    "category_risk": "category, type of fund, risk level, riskometer, how risky the fund is",
    "nav": "NAV, net asset value, price of one unit",
    "fund_size": "fund size, AUM, assets under management, how big the fund is",
    "stamp_duty": "stamp duty charged on purchase of units",
    "tax_implication": "tax, taxation of gains, capital gains tax, STCG, LTCG, short term and long term",
    "fund_management": "fund manager, who manages or runs the fund, portfolio manager, managing since, education and experience",
    "investment_objective": "investment objective, aim, goal, what the fund invests in",
    "benchmark": "benchmark index the fund is measured against",
    "holdings": "portfolio holdings, stocks, shares and companies the fund owns, weight of each holding",
    "fund_house": "fund house, AMC, asset management company, contact phone number and address",
    "custodian": "custodian bank that holds the fund's securities in safe custody",
    "registrar": "registrar and transfer agent, RTA",
}


def content(c):
    if VARIANT.startswith("text-only"):
        return c["text"]
    if "syn" in VARIANT:
        return c["header"] + "\nCovers: " + SYN[c["chunk_id"].split(":")[1]] + ".\n" + c["text"]
    return c["header"] + "\n" + c["text"]


# ---- query set -------------------------------------------------------------------------------
golden = yaml.safe_load(open(REPO / "tests/eval/golden.yaml"))
src2scheme = {s["source_id"]: sid for sid, s in schemes.items()}
G = []
for case in golden:
    for t in case.get("turns", [case]):
        if t.get("expect_kind") == "answer":
            G.append((t["q"], src2scheme[t["expect_source"]], FACT_GROUP[t["fact"]]))
SETS = {"golden": G, "paraphrase": PARAPHRASES, "heldout": HELDOUT}

# ---- indexes ---------------------------------------------------------------------------------
from sentence_transformers import SentenceTransformer  # noqa: E402

t0 = time.time()
model = SentenceTransformer(MODEL)
load_s = time.time() - t0
qp, pp = PREFIX.get(MODEL, ("", ""))
if NO_INSTR:
    qp = "" if "e5" not in MODEL else qp
tok = model.tokenizer
m = re.search(r"h(\d+)", VARIANT)
if m:  # re-split holdings with the model's own tokenizer, budget = N tokens of content
    budget = int(m.group(1))
    new, done = [], set()
    for c in chunks:
        if group(c) != "holdings":
            new.append(c); continue
        base = ":".join(c["chunk_id"].split(":")[:2])
        if base in done:
            continue
        done.add(base)
        parts = [x for x in chunks if x["chunk_id"].startswith(base)]
        lead = parts[0]["text"].split("\n")[0]
        body = [ln for x in parts for ln in x["text"].split("\n")[1:]]
        cur, out_parts = [], []
        for ln in body:
            trial = "\n".join([lead, *cur, ln])
            if cur and len(tok(pp + content({**c, "text": trial}))["input_ids"]) > budget:
                out_parts.append(cur); cur = []
            cur.append(ln)
        out_parts.append(cur)
        for n, p in enumerate(out_parts, 1):
            cid = base + (f":{n}" if len(out_parts) > 1 else "")
            new.append({**c, "chunk_id": cid, "text": "\n".join([lead, *p])})
    chunks = new
    ENT = {"Goodyear": "Goodyear India", "Sula": "Sula Vineyards", "Timken": "Timken India"}
    HOLDINGS_DEEP = [(q, sid, next(c["chunk_id"] for c in chunks if c["scheme_id"] == sid
                                   and group(c) == "holdings" and next(v for k, v in ENT.items() if k in q) in c["text"]))
                     for q, sid, _ in HOLDINGS_DEEP]
lens = [len(tok(pp + content(c))["input_ids"]) for c in chunks]
maxlen = model.max_seq_length
truncated = [(c["chunk_id"], n) for c, n in zip(chunks, lens) if n > maxlen]
t0 = time.time()
D = model.encode([pp + content(c) for c in chunks], normalize_embeddings=True, batch_size=16)
embed_s = time.time() - t0

TOK_RE = re.compile(r"[a-z0-9]+(?:[.,][0-9]+)*")
STOP = set("a an the of in on for to is are was do does did i my me what which who how much many "
           "and or it its this that be with by from at as can will any there".split())
SCHEME_WORDS = set("hdfc large mid midcap small multi cap silver etf fof fund direct growth scheme".split())


def stem(w):
    for suf in ("ing", "ers", "er", "es", "ed", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


if "bm25fix" in VARIANT:
    # filter does the scheme, so drop scheme words; drop stopwords; light stemming
    def tokenize(s):
        return [stem(w) for w in TOK_RE.findall(s.lower()) if w not in STOP and w not in SCHEME_WORDS]

    def bm25_doc(c):
        return c["heading_path"] + "\n" + content(c).split("\n", 1)[1]
else:
    def tokenize(s):
        return TOK_RE.findall(s.lower())

    bm25_doc = content
bm25 = BM25Okapi([tokenize(bm25_doc(c)) for c in chunks])

scheme_of = np.array([c["scheme_id"] for c in chunks])


def query_text(q, sid):
    # arch §6.5 step 1: user text + the resolved scheme's name (helps follow-ups like "And its exit load?")
    if sid and "noname" not in VARIANT and schemes[sid]["short_name"].lower() not in q.lower():
        return f"{q} ({schemes[sid]['short_name']})"
    return q


def retrieve(q, sid, use_filter=True):
    qt = query_text(q, sid)
    qv = model.encode([qp + qt], normalize_embeddings=True)[0]
    dense = D @ qv
    sparse = bm25.get_scores(tokenize(qt))
    mask = (scheme_of == sid) if (sid and use_filter) else np.ones(len(chunks), bool)
    idx = np.where(mask)[0]
    d_rank = idx[np.argsort(-dense[idx])][:10]
    s_rank = idx[np.argsort(-sparse[idx])][:10]
    rrf = {}
    for r, i in enumerate(d_rank):
        rrf[i] = rrf.get(i, 0) + 1 / (60 + r + 1)
    for r, i in enumerate(s_rank):
        if sparse[i] > 0:
            rrf[i] = rrf.get(i, 0) + 1 / (60 + r + 1)
    fused = sorted(rrf, key=lambda i: -rrf[i])
    w = {}
    for r, i in enumerate(d_rank):
        w[i] = w.get(i, 0) + 1 / (60 + r + 1)
    for r, i in enumerate(s_rank):
        if sparse[i] > 0:
            w[i] = w.get(i, 0) + 0.5 / (60 + r + 1)
    return {
        "dense": list(d_rank), "bm25": list(s_rank), "rrf": fused,
        "rrf_w": sorted(w, key=lambda i: -w[i]),
        "top_cos": float(dense[idx].max()), "top_bm25": float(sparse[idx].max()),
        "margin": float(np.sort(dense[idx])[-1] - np.sort(dense[idx])[-2]),
    }


def ok(i, sid, grp):
    return chunks[i]["scheme_id"] == sid and group(chunks[i]) == grp


def score(results, sid, grp, method):
    ranks = results[method]
    pos = next((r for r, i in enumerate(ranks) if ok(i, sid, grp)), None)
    return (pos == 0, pos is not None and pos < 4, 0 if pos is None else 1 / (pos + 1))


out = {"model": MODEL, "variant": VARIANT, "dim": int(D.shape[1]), "max_seq": maxlen,
       "load_s": round(load_s, 2), "embed_corpus_s": round(embed_s, 2),
       "tok_max": max(lens), "tok_holdings": sorted(n for c, n in zip(chunks, lens) if group(c) == "holdings"),
       "truncated": truncated, "n_chunks": len(chunks), "sets": {}}

misses = []
in_cos = []
for name, qs in SETS.items():
    for filt in (True, False):
        agg = {m: [0, 0, 0.0] for m in ("dense", "bm25", "rrf", "rrf_w")}
        for q, sid, grp in qs:
            r = retrieve(q, sid, filt)
            if filt:
                in_cos.append(r["top_cos"])
            for m in agg:
                h1, h4, rr = score(r, sid, grp, m)
                agg[m][0] += h1; agg[m][1] += h4; agg[m][2] += rr
                if filt and m == "dense" and not h4:
                    misses.append((name, q, grp, [chunks[i]["chunk_id"] for i in r["rrf"][:4]]))
        n = len(qs)
        out["sets"][f"{name}{'' if filt else '_nofilter'}"] = {
            m: {"hit@1": round(v[0] / n, 3), "hit@4": round(v[1] / n, 3), "mrr": round(v[2] / n, 3)}
            for m, v in agg.items()} | {"n": n}

deep = []
for q, sid, cid in HOLDINGS_DEEP:
    r = retrieve(q, sid)
    deep.append({m: [chunks[i]["chunk_id"] for i in r[m][:4]].index(cid) if cid in
                 [chunks[i]["chunk_id"] for i in r[m][:4]] else None for m in ("dense", "bm25", "rrf", "rrf_w")})
out["holdings_deep_rank"] = deep


def ooc(qs):
    rows = []
    for q, sid in qs:
        r = retrieve(q, sid)
        rows.append({"q": q, "top_cos": round(r["top_cos"], 3), "top_bm25": round(r["top_bm25"], 2),
                     "top1": chunks[r["rrf"][0]]["chunk_id"] if r["rrf"] else None})
    return rows


oo_s, oo_n, oo_g = ooc(OOC_SCHEME), ooc(OOC_NEAR_MISS), ooc(OOC_GENERAL)
out_cos = [r["top_cos"] for r in oo_s + oo_g]


def auroc(pos, neg):
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


best = max(((t, (sum(c >= t for c in in_cos) + sum(c < t for c in out_cos)) / (len(in_cos) + len(out_cos)))
            for t in np.arange(0.3, 0.95, 0.005)), key=lambda x: x[1])
out["threshold"] = {
    "in_cos": {"min": round(min(in_cos), 3), "p5": round(float(np.percentile(in_cos, 5)), 3),
               "median": round(statistics.median(in_cos), 3), "max": round(max(in_cos), 3)},
    "ooc_cos": {"max": round(max(out_cos), 3), "p95": round(float(np.percentile(out_cos, 95)), 3),
                "median": round(statistics.median(out_cos), 3)},
    "auroc": round(auroc(in_cos, out_cos), 3),
    "best_tau": round(float(best[0]), 3), "best_acc": round(best[1], 3),
    "tau_0.55": {"in_kept": round(sum(c >= 0.55 for c in in_cos) / len(in_cos), 3),
                 "ooc_rejected": round(sum(c < 0.55 for c in out_cos) / len(out_cos), 3)},
}
out["ooc_scheme"], out["ooc_near_miss"], out["ooc_general"] = oo_s, oo_n, oo_g
out["rrf_misses"] = misses

lat = []
for q, sid, _ in G[:20]:
    t0 = time.perf_counter(); retrieve(q, sid); lat.append((time.perf_counter() - t0) * 1000)
out["latency_ms_p50"] = round(statistics.median(lat), 1)
out["latency_ms_max"] = round(max(lat), 1)
print(json.dumps(out, indent=1, ensure_ascii=False))
