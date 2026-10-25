import json, sys, glob, os
rows = []
for f in sorted(glob.glob(sys.argv[1] + "/results/*__*.json")):
    try: d = json.load(open(f))
    except Exception: continue
    m = d["model"].split("/")[-1]; s = d["sets"]; th = d["threshold"]
    def g(k, meth): return s[k][meth]
    rows.append([m, d["variant"], d["dim"], d["n_chunks"], len(d["truncated"]),
        *[f'{g(k,"dense")["hit@1"]:.2f}/{g(k,"dense")["hit@4"]:.2f}' for k in ("golden","paraphrase","heldout")],
        *[f'{g(k,"rrf")["hit@4"]:.2f}' for k in ("paraphrase","heldout")],
        *[f'{g(k,"rrf_w")["hit@4"]:.2f}' for k in ("paraphrase","heldout")],
        f'{g("heldout","bm25")["hit@4"]:.2f}',
        f'{g("heldout_nofilter","dense")["hit@4"]:.2f}',
        th["auroc"], th["in_cos"]["min"], th["ooc_cos"]["max"], th["best_tau"], d["latency_ms_p50"], d["load_s"]])
hdr = ["model","variant","dim","N","trunc","D gold h1/h4","D para h1/h4","D held h1/h4","RRF para","RRF held","RRFw para","RRFw held","BM25 held","D held nofilt","AUROC","in_min","ooc_max","best_tau","ms","load_s"]
print("| " + " | ".join(hdr) + " |"); print("|" + "---|"*len(hdr))
for r in rows: print("| " + " | ".join(map(str, r)) + " |")
