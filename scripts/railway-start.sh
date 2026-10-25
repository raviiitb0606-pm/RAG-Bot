#!/bin/sh
# Container entrypoint (docs/deployment-plan.md D4): make sure an index is live, keep pulling
# new ones in the background, then serve. The API hot-reloads data/index/current (P7.6), so
# this loop only repoints it; a pull killed mid-way is harmless (the swap is an atomic rename).
set -eu
: "${MF_FAQ_DATA_DIR:=/data}"
export MF_FAQ_DATA_DIR
mkdir -p "$MF_FAQ_DATA_DIR"

if ! mf-faq pull-index; then
  if [ -s "$MF_FAQ_DATA_DIR/index/current" ]; then
    echo "pull-index failed; serving the index already in $MF_FAQ_DATA_DIR" >&2
  else
    echo "pull-index failed and $MF_FAQ_DATA_DIR has no index:" \
      "check MF_FAQ_GITHUB_REPO / MF_FAQ_GITHUB_TOKEN and the data-latest release" >&2
    exit 1
  fi
fi

(
  while true; do
    sleep "${MF_FAQ_PULL_INTERVAL_S:-3600}"
    mf-faq pull-index || echo "pull-index failed; retrying in ${MF_FAQ_PULL_INTERVAL_S:-3600} s" >&2
  done
) &

# exec: uvicorn becomes PID 1 and gets Railway's SIGTERM on redeploy directly.
exec mf-faq serve --host 0.0.0.0 --port "${PORT:-8000}"
