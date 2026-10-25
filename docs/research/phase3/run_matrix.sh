#!/bin/zsh
cd "$(dirname "$0")/../../.."  # repo root
run() { uv run python -I docs/research/phase3/harness.py $PWD $1 $2 2>/dev/null > docs/research/phase3/results/$(echo $1 | tr '/' '_')__$2.json || echo "FAIL $1 $2"; echo "done $1 $2"; }
for v in header+text noinstr syn bm25fix syn+bm25fix syn+bm25fix+h400 h400 text-only; do run BAAI/bge-small-en-v1.5 $v; done
for m in BAAI/bge-base-en-v1.5 BAAI/bge-large-en-v1.5 sentence-transformers/all-MiniLM-L6-v2 intfloat/e5-small-v2 thenlper/gte-small Snowflake/snowflake-arctic-embed-s; do run $m header+text; run $m syn+bm25fix+h400; done
