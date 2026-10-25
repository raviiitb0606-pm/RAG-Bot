# Phase 3 Data Research: Chunking, Embeddings, Vector Store

> **Note (25 Oct 2026):** the owner chose **ChromaDB** as the vector store instead of the NumPy flat index recommended in §1 and §3.6 (implementation plan, P3.1 amendment). The measurements below are unchanged; with Chroma, the retrieval eval gives the same results.

> **What this file is for:** the evidence behind the Phase 3 design in [`implementation-plan.md`](../implementation-plan.md) (P3.0–P3.8). Every number here comes from the cleaned corpus `data/chunks.jsonl` (corpus `2026-10-25.99bd795b`, 80 chunks) and was measured on 25 Oct 2026. Scripts and raw results are in [`phase3/`](./phase3/). To re-run: `zsh docs/research/phase3/run_matrix.sh`, then `python3 docs/research/phase3/summary.py docs/research/phase3`.

---

## 1. Summary of decisions

| Topic | Plan said | Research says | Why (section) |
|---|---|---|---|
| Chunk unit | One chunk per fact group | **Keep it** | Corpus is regular, and fact groups map 1:1 to `facts` metadata (§2) |
| Chunk text | `header + text` | **`header + "Covers: …" line + text`**: a fixed, value-free description of each section | Held-out hit@4 (dense) goes from 0.87 to 1.00; AUROC goes from 0.87 to 0.92 (§3.2) |
| Holdings split | ≤ 600 *estimated* tokens | **≤ 400 tokens counted with the embedding model's own tokenizer** | 8 of 13 holdings chunks are 654–694 real tokens and get truncated at 512 (§3.1) |
| Embedding model | `bge-small-en-v1.5` | **Keep `bge-small-en-v1.5`, with its query instruction** | After the chunk fix, base/large models add nothing to hit@4, cost 1.6–4× the latency, and score ranges are no better (§3.4) |
| Vector store | Chroma | **NumPy exact flat index** (`vectors.npy`); drop `chromadb` | 86 vectors; exact search in 0.004 ms vs 0.4 ms; no telemetry, no per-path singleton, no extra dependency tree (§3.6) |
| BM25 | Pickled `rank_bm25` over `header+text` | **Rebuild in memory at startup (1.4 ms)** over `heading + Covers + text`, no scheme words or stopwords, light stemming | Scheme-name tokens inside a filtered scheme made BM25 favour the shortest chunks; pickle is a code-execution risk (§3.3) |
| Fusion | RRF k=60, top-4 | **Keep it** | With the fixes, RRF = 1.00 hit@4 on all three query sets; BM25 is what finds holdings entities (§3.3) |
| Scheme filter | Optional ("search all 5 if no scheme") | **Required.** No resolved scheme means no retrieval | Unfiltered search loses 13–26 points of RRF hit@1, and other-AMC questions score like real ones (§3.5) |
| Evidence threshold τ | 0.55, the main not-found gate | **τ = 0.70, a safety floor only.** Not-found comes from deterministic layers (§4) | At 0.55, 3% of out-of-corpus questions are rejected. Scheme-scoped out-of-corpus cosines (0.72–0.86) overlap in-corpus (0.745–0.90) (§3.5) |
| Alias matching | rapidfuzz token-set ≥ 85 | **Check other-AMC names first**; generic aliases don't count when another fund house is named | "SBI Small Cap Fund", "Mirae Asset Large Cap Fund" etc. score **100** against HDFC aliases (§3.7) |

---

## 2. Corpus profile

- **80 chunks from 5 pages** (Large 16, Mid 16, Small 17, Multi 17, Silver FoF 14); 34 KB of text, about 8.4k estimated tokens.
- **15 fact groups, the same on every page.** Chunks of one group differ across schemes only in their values. So the **scheme is the main discriminator**, and inside one scheme retrieval is "pick 1 of ~15 sections".
- **Holdings dominate the volume:** 13 of 80 chunks (16%) hold ~63% of the tokens. All other groups are 23–153 estimated tokens.

| Group | Chunks | Est. tokens (min–max) | Notes |
|---|---|---|---|
| holdings | 13 | 49–599 (real: 58–694) | Split into parts; long lists of company names |
| fund_management | 5 | 132–153 | Names, "since" dates, education/experience bios |
| tax_implication | 5 | 63–70 | Contains "returns are taxed at 20%" (passes the P2.7 gate) |
| fund_house | 5 | 62 | Name, phone, address |
| others (11 groups) | 52 | 23–50 | One to three short lines each |

**What this means for retrieval:** the main failure mode is the **vocabulary gap** between how users ask and the section labels: "TER", "redemption charges", "who runs", "RTA", "which stocks". Telling schemes apart is not the problem.

---

## 3. Experiments

**Query sets** (`phase3/queries.py`):
- **golden:** the 31 factual `answer` turns in `golden.yaml`.
- **paraphrase:** 48 hand-written questions that avoid the section labels.
- **held-out:** 30 more questions, written *before* any variant was tried and never used to choose the "Covers" wording.
- **3 deep-holdings lookups.**
- **32 out-of-corpus questions:** 16 scheme-scoped (lock-in, launch date, Sharpe ratio, SWP…), 3 near-misses (regular-plan expense ratio, last year's expense ratio, the 2019 exit load) and 13 general or other-AMC.

The query always gets the resolved scheme's name appended (arch §6.5 step 1). A hit means a chunk of the expected scheme and group is in the top k.

### 3.1 Truncation (real tokenizer)

The regex estimate in `chunk.py` undercounts WordPiece by ~15%. A "599-token" chunk is 694 real tokens. **8 of 13 holdings chunks exceed bge's 512-token limit**, so their tails are never embedded. Re-splitting at ≤ 400 model tokens gives 19 holdings parts (86 chunks in all) and 0 truncated chunks.

### 3.2 Chunk content variants (`bge-small-en-v1.5`, dense, scheme-filtered, hit@1 / hit@4)

| Variant | golden | paraphrase | held-out | Out-of-corpus AUROC |
|---|---|---|---|---|
| `text` only (no header) | 0.45 / 0.81 | 0.38 / 0.65 | 0.43 / 0.70 | 0.78 |
| `header + text` (current) | 1.00 / 1.00 | 0.79 / 0.92 | 0.77 / 0.87 | 0.87 |
| `header + Covers + text` | 1.00 / 1.00 | 0.92 / 0.98 | 0.77 / **1.00** | **0.92** |
| `… + holdings ≤ 400 tokens` | 1.00 / 1.00 | 0.92 / 0.96 | 0.80 / 0.97 | 0.92 |

- The context header is essential. Without it, hit@4 drops to 0.65–0.81.
- The **"Covers:" line** is the largest single gain. It is a fixed description per *section* and contains no values, e.g. for exit load: "exit load, redemption charge, penalty or fee for withdrawing or selling units early". It closes the vocabulary gap, and on held-out questions too.
- Re-splitting holdings costs about one query on dense alone, but fixes truncation. Hybrid recovers that query (§3.3).

### 3.3 BM25 and fusion (best chunk variant, hit@4)

| Method | golden | paraphrase | held-out | deep holdings (rank of the right part) |
|---|---|---|---|---|
| BM25, naive tokenisation, `header+text` | 0.94 | 0.71 | 0.70 | — |
| BM25, fixed (see below) | 1.00 | 0.90 | 0.93 | 1st, 1st, 1st |
| Dense only | 1.00 | 0.96 | 0.97 | 2nd, 3rd, 1st |
| **RRF (k=60)** | **1.00** | **1.00** | **1.00** | **1st, 1st, 1st** |

- **Why naive BM25 hurt.** Every chunk of a scheme repeats the scheme name in its header. Those tokens are rare across the corpus (high IDF) but present in every chunk *after* the filter, so all chunks scored, and BM25's length normalisation pushed the shortest ones to the top (custodian, category). With the current text, RRF with naive BM25 was *worse* than dense alone (paraphrase 0.81 vs 0.92).
- **The fix.** Index `heading_path + Covers + text` without the header. Drop scheme words and stopwords from documents and queries, and apply light suffix stemming ("manages" → "manag", matching "managers").
- **BM25 is worth keeping.** It is what finds named entities in holdings lists ("Goodyear India", "Sula Vineyards") and the remaining dense misses. Weighted RRF (BM25 × 0.5) gave the same hit@4, so plain RRF stays.

### 3.4 Embedding models (best chunk variant, hybrid unless noted)

| Model | Dim | Max seq | Dense held-out hit@1 / hit@4 | RRF paraphrase / held-out hit@4 | AUROC | In-corpus min cos | Out-of-corpus max cos | Query latency p50 |
|---|---|---|---|---|---|---|---|---|
| **bge-small-en-v1.5** | 384 | 512 | 0.80 / 0.97 | **1.00 / 1.00** | 0.92 | 0.745 | 0.857 | **8 ms** |
| bge-base-en-v1.5 | 768 | 512 | 0.90 / 0.97 | 0.98 / 0.93 | 0.91 | 0.674 | 0.803 | 13 ms |
| bge-large-en-v1.5 | 1024 | 512 | 0.87 / 0.97 | 0.96 / 1.00 | 0.95 | 0.687 | 0.800 | 33 ms |
| gte-small | 384 | 512 | 0.93 / 1.00 | 0.96 / 0.97 | 0.92 | 0.894 | 0.943 | 7 ms |
| snowflake-arctic-embed-s | 384 | 512 | 0.80 / 0.97 | 0.98 / 1.00 | 0.91 | 0.700 | 0.849 | 8 ms |
| e5-small-v2 | 384 | 512 | 0.73 / 0.97 | 0.98 / 0.97 | 0.87 | 0.869 | 0.915 | 8 ms |
| all-MiniLM-L6-v2 | 384 | **256** | 0.70 / 0.93 | 0.96 / 0.97 | 0.92 | 0.609 | 0.771 | 5 ms |

Latency is measured on a CPU laptop and includes query embedding.

- **Fixing the chunks matters more than the model.** Before the fix, every model scored 0.80–0.97 dense held-out hit@4; after it, all 512-token models score 0.97–1.00.
- **Bigger bge models don't pay for themselves here.** They are no better on hybrid hit@4 and 1.6–4× slower. bge-large has the best AUROC (0.95), but §3.5 shows that no model makes a cosine threshold safe for scheme-scoped questions.
- **gte-small and e5-small compress their scores** (everything sits in 0.87–0.95), which makes any threshold fragile. MiniLM truncates at 256 tokens.
- **Without the bge query instruction** (`noinstr`), held-out hit@4 is slightly lower (0.93 vs 0.97 dense) and the score ranges shift upward. Keep the instruction (model-card default).
- **A hosted embedding API is not needed.** It would add an API key, network latency, and send (redacted) user questions to another third party.

### 3.5 Can a cosine threshold detect out-of-corpus questions?

Best configuration, top-1 dense cosine:

| Question set | Range | Example |
|---|---|---|
| In-corpus, scheme-filtered (109 questions) | 0.745 – 0.899 (p5 0.769) | — |
| Out-of-corpus **with** a scheme named (16) | 0.717 – 0.857 | "Groww rating of HDFC Multi Cap" 0.857, "P/E ratio of HDFC Large Cap" 0.819 |
| Near-miss (3) | 0.807 – 0.860 | "expense ratio of HDFC Large Cap last year" 0.860 |
| General, no supported scheme (13) | 0.513 – 0.751 | "expense ratio of Axis Midcap Fund" 0.751, "exit load of SBI Bluechip" 0.725 |

- **τ = 0.55 rejects 1 of 29** out-of-corpus questions, so it never fires.
- **The best possible τ (0.74) rejects only 4 of 16 scheme-scoped out-of-corpus questions**, three of them by margins of 0.005 or less. Both golden cases N-07 (launch date) and N-08 (lock-in) sit at 0.735–0.737, right at the edge.
- **Scheme-scoped out-of-corpus questions can't be separated by score.** The filter always returns the scheme's best section, and that section looks relevant ("Sharpe *ratio*" → expense ratio).
- **Other-AMC questions without a filter score like in-corpus ones** (0.71–0.75). They would be answered from an HDFC chunk.
- **Unfiltered retrieval is also less accurate:** RRF hit@1 drops from 0.90 to 0.71–0.77.

A precise **unsupported-topic lexicon** (`phase3/lexicon.py`) caught 16/16 scheme-scoped out-of-corpus questions with **0 false positives on 112 in-corpus questions**. *Caveat:* the same person wrote the lexicon and these questions, so P8.1 must add fresh cases.

### 3.6 Vector store (80–86 vectors × 384 dims)

| | Chroma 1.5.9 (persistent) | NumPy flat (`.npy`) |
|---|---|---|
| Filtered top-10 query | 0.43 ms | 0.004 ms |
| Exact? | Yes at this size (100/100 identical to brute force) | Yes, by construction |
| On disk | 524 KB (SQLite + segments) | 120 KB |
| Dependencies | `chromadb` + 139-line dependency tree, incl. Rust bindings (50 MB) and `onnxruntime` (76 MB) | NumPy (already present) |
| Telemetry | `anonymized_telemetry=True` by default (PostHog); must be disabled | None |
| Blue/green swap | One client per path per process; "instance already exists … with different settings" if reopened differently | Load a new file, swap one reference |

The only feature Phase 3 needs from a vector store is a `scheme_id` filter, and a boolean mask does that. Brute-force search stays under a millisecond up to roughly 10⁵ chunks. That is about 1,000× this corpus, far beyond the 5-page scope. **Revisit** (LanceDB / Chroma / sqlite-vec) only if the corpus grows past ~100k chunks or needs concurrent writers.

### 3.7 Scheme resolution: generic aliases

`schemes.yaml` has generic aliases (`small cap fund`, `large cap fund`, `multicap fund`, `silver fof`, `mid cap`). With `token_set_ratio`, an alias that is a subset of the question scores 100:

| Question | Best match | Score |
|---|---|---|
| "exit load of SBI Small Cap Fund" | hdfc-small-cap via `small cap fund` | 100 |
| "Nippon India Silver ETF FoF exit load" | hdfc-silver-etf-fof via `silver fof` | 100 |
| "ICICI Prudential Large Cap Fund expense ratio" | hdfc-large-cap via `large cap fund` | 100 |
| "What is the expense ratio of Axis Mid Cap Fund?" | hdfc-mid-cap via `mid cap` | 100 |
| "Kotak Multicap Fund benchmark" | hdfc-multi-cap via `multicap fund` | 100 |

The `not_in_scope` list covers other *HDFC* schemes only. Without an other-AMC check, the bot would answer these questions with HDFC's figures **and cite them**: confidently wrong and hard to spot.

---

## 4. Resulting not-found design (layered)

| Layer | Where | Catches | Tested here |
|---|---|---|---|
| A. Other AMC or deny-listed HDFC scheme named → `not_found` | P3.2 | SBI/Axis/Nippon…, HDFC Flexi Cap, HDFC Silver ETF | Golden N-04, N-05, N-06; §3.7 |
| B. No scheme resolved and no follow-up carry-over → `not_found`, **no retrieval** | P3.2 / P3.4 | ELSS, statements, KYC, ITR, "what is a mutual fund", silver price | 13/13 general |
| C. Unsupported-topic lexicon → `not_found` | P3.4 | Lock-in, launch date, rating, Sharpe/SD/beta/turnover/P/E, SWP/STP, IDCW, nominee, statement, regular plan, CEO, SIP dates | 16/16 scheme-scoped, 0/112 false positives |
| D. Dense floor τ = 0.70 | P3.4 | Garbage and very-low-similarity questions that slip through A–C | In-corpus min 0.745 (0.045 margin) |
| E. LLM `insufficient` status + V4 number grounding | P5 / P4.6 | Near-misses ("last year's expense ratio", "regular plan") | Not testable at retrieval level |

Historical questions ("in 2019", "last year", "on 1 Jan 2024") also overlap with the router's performance rule (golden P-04). Layer E stays the backstop for them.

---

## 5. Caveats

- **The query sets are small:** 112 in-corpus and 32 out-of-corpus questions. One query is worth about 3 points on the held-out set.
- **One author wrote the paraphrases, the "Covers" wording and the lexicon.** The held-out set guards the "Covers" result (written before any variant was tried); nothing guards the lexicon yet. P3.5 merges these sets into the eval; P8.1 must add cases written by someone else.
- **τ and all cosine figures are specific to the model and chunk format.** Re-derive them if the model, the query instruction, the header or the "Covers" wording changes (P3.6).
- **Holdings questions are in scope (`available_facts`) but absent from `golden.yaml`.** P3.5 adds them.
- **The NumPy recommendation assumes a read-only index loaded once per process** (arch §8.2), which is already the design.
