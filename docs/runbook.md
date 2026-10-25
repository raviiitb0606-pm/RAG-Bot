# Runbook

> Operations guide for the Mutual Fund FAQ Assistant. This file currently covers the **daily refresh** (implementation plan Phase 7). Phase 10 (P10.2) adds the rest: re-ingest after a Groww layout or URL change, and the eval gate in detail.

## Daily refresh

### How it works

| Where | What | When |
|---|---|---|
| GitHub Actions, `.github/workflows/daily-refresh.yml` | restore state from `data-latest` → unit tests → `mf-faq ingest --report` → publish on `published` | 10:00 IST daily (`30 4 * * *` UTC), best-effort |
| Server, host cron or systemd timer | `mf-faq pull-index` installs the newest published index | e.g. 10:45 IST |
| Running API | re-reads `data/index/current` and hot-swaps the pipeline | every `MF_FAQ_INDEX_RELOAD_S` (300 s) |

Releases in this (private) repository:

- `data-<index_version>`: one per published version, with `refresh-state-<version>.tar.gz` + `.sha256`. The newest 14 are kept, which is the rollback window.
- `data-latest`: the newest bundle as `refresh-state.tar.gz` + `.sha256`, plus `refresh-status.json` (the stale-streak tracker, updated after every non-dry run).

Run outcomes (in the job summary and `report.json`):

| Outcome | Meaning | Live index |
|---|---|---|
| `published` | facts (or the index inputs) changed and every gate passed | new version, released |
| `unchanged` | gates passed; the live version is still current (weekends, holidays) | unchanged, nothing released |
| `gate_failed` | a build gate (layout change, return figures, …) or the retrieval eval failed | untouched; run fails, issue opened |
| `fetch_blocked` | no page could be downloaded, or a page has no copy at all | untouched; run fails, issue opened |

A run also fails, even with a good build, when a page has been `stale` (its fetch failed, so the last good copy was used) for **3 days in a row**. That usually means Groww is blocking the runner's IPs.

### Server setup (once)

1. Create a **fine-grained token** with read-only *Contents* access to this repository only.
2. In the server's `.env`: `MF_FAQ_GITHUB_REPO=<owner>/<repo>` and `MF_FAQ_GITHUB_TOKEN=<token>`.
3. Schedule the pull, e.g. crontab (server clock in IST): `45 10 * * * cd /srv/mf-faq && uv run mf-faq pull-index >> pull.log 2>&1`.
4. Check: `uv run mf-faq pull-index` prints `index <version> … now live` (or `unchanged, already live`). Within 5 minutes `GET /api/health` shows the new `index_version` / `corpus_version`, with no restart needed.

### A failed run (`refresh-failed` issue)

1. Open the run linked from the issue. Its summary lists the failures per source.
2. **`gate_failed` with `label '…'` / `available_facts not found`**: Groww changed the page layout. Fix the parser against a fresh copy (P10.2). Until then the live index keeps serving the last good data.
3. **`gate_failed` with `retrieval eval: …`**: the new chunks hurt retrieval. Compare with `uv run mf-faq eval --retrieval-only` locally.
4. **`fetch_blocked`, or the 3-day stale alert**: try one dispatch run later in the day. If it is still blocked, use the manual-save fallback for the affected page (`mf-faq ingest --manual-copy <id> --saved-on YYYY-MM-DD`), build locally, then publish by hand (see *Publish by hand*).
5. **Job error before the report** (install, model download, timeout): re-run the job.

The issue closes itself on the next successful run.

### Re-run a missed day

GitHub can start a scheduled run late or drop it under load. Go to *Actions → daily-refresh → Run workflow* and keep the defaults (`publish: true`, `restore: true`). A second run on the same day counts as the same day for the stale streak.

**Dry run** (e.g. after changing the workflow): run it with `publish: false`. It builds and reports, but publishes nothing, saves no status and touches no issue.

### Roll back a bad index

- **On the server, immediately:** `uv run mf-faq index --rollback` repoints `current` to `previous`; the API reloads within `MF_FAQ_INDEX_RELOAD_S`. The next `pull-index` would reinstall the bad version, so also do the next step (or pause the server's pull timer).
- **For good:** make an older release the latest. Download its bundle and re-upload it under the fixed names:
  ```sh
  gh release download data-<good_version> --dir old
  cp old/refresh-state-<good_version>.tar.gz refresh-state.tar.gz
  sha256sum refresh-state.tar.gz > refresh-state.tar.gz.sha256
  gh release upload data-latest refresh-state.tar.gz refresh-state.tar.gz.sha256 --clobber
  ```
  Then delete the bad `data-<version>` release, so the rollback window stays clean.

### Publish by hand

From a machine with the current state (`mf-faq state import` of `data-latest`, then your fix and `mf-faq ingest`):

```sh
uv run mf-faq state export out/refresh-state-<index_version>.tar.gz
gh release create data-<index_version> out/refresh-state-<index_version>.tar.gz{,.sha256} --latest=false
# then re-upload it under the fixed names to data-latest, as in "Roll back"
```

### Start from empty state

Restoring is refused when the code's embedding model, query instruction or chunk format no longer match the bundle (`state import` exits 1: "bundle refused: … embed_model …"). After such a deliberate change, run the workflow once with `restore: false`. It fetches all 5 pages fresh. Footer dates restart at that day, because the old `fetched_at` history isn't carried over.

### Rotate the server token

1. Create the new fine-grained token (read-only *Contents*, this repo only).
2. Replace `MF_FAQ_GITHUB_TOKEN` in the server's `.env` and run `uv run mf-faq pull-index` once to check it.
3. Revoke the old token in GitHub settings.

The workflow itself uses only the per-run `GITHUB_TOKEN`; there is nothing to rotate there.

### Pause the schedule

*Actions → daily-refresh → ⋯ → Disable workflow* (or `gh workflow disable daily-refresh`). The live index keeps serving; footer dates stay at their last values. Re-enable with `gh workflow enable daily-refresh`. GitHub also disables scheduled workflows by itself after 60 days without repository activity, so check this if runs stop.
