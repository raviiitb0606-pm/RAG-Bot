# Deployment Plan: Railway

> **What this file is for:** how to run the Mutual Fund FAQ Assistant (API + chat UI) on [Railway](https://railway.com), and keep it fed by the daily refresh of Phase 7. It builds on [`architecture.md`](./architecture.md) §5.7 and §15, and on the [`runbook.md`](./runbook.md). Task IDs `D1…` belong to this plan. They also cover P10.5 (`Dockerfile`) of the implementation plan.
>
> **Status (26 Oct 2026):** code changes **D1–D4 done**; Railway setup (§4–§6) not started yet.
> - D1: `torch` is now a direct dependency (`tool.uv.sources` only applies to direct ones), so Linux locks `2.14.1+cpu`; the 15 `nvidia-*` packages and `triton` are gone from `uv.lock`.
> - D2: `MF_FAQ_EMBED_MODEL_REVISION` (default `5c38ec7…`, the revision of today's live index) is used by every embedder (`index.embedder_from`), baked into the image, and set in `daily-refresh.yml` (also its cache key). `tests/unit/test_deploy_files.py` fails if the config, Dockerfile, workflow and `.env.example` disagree.
> - D3/D4: `Dockerfile`, `.dockerignore`, `scripts/railway-start.sh`. The script was checked locally (no Docker on the dev machine): with an empty data dir it exits 1 with the message below; with an existing index it serves `/api/health` + UI and retries the pull in the background. The image itself is built and smoke-tested by the new `docker` job in `ci.yml` (CPU torch, baked model loads with `--network none`, an empty volume fails fast).

---

## 1. Target setup

```
GitHub (private repo)                                Railway project "mf-faq"
┌──────────────────────────────┐                    ┌──────────────────────────────────────────┐
│ push to main ── ci.yml ──────┼── deploy ────────► │ service "mf-faq" (Dockerfile, 1 replica) │
│                              │                    │  start: scripts/railway-start.sh         │
│ daily-refresh.yml 10:00 IST  │                    │   1. mf-faq pull-index (boot)            │
│   → release data-latest ◄────┼── pull (hourly) ── │   2. pull-index loop, every hour         │
│     (bundle + .sha256)       │    read-only token │   3. mf-faq serve --host 0.0.0.0 $PORT   │
└──────────────────────────────┘                    │      re-reads index/current every 300 s  │
                                                    │ volume "mf-faq-data" → /data             │
                                                    │   index/<version>/, current, previous    │
       users ── HTTPS ── <name>.up.railway.app ───► │   app.db (threads, 30-day TTL)           │
                                                    └──────────────────────────────────────────┘
```

- **One service, one volume, one replica.** The volume holds the live index (two versions: `current` and `previous`) and the SQLite thread store.
- **The server pulls; nothing pushes to Railway.** Phase 7's design is unchanged. GitHub Actions publishes `data-latest`; the container runs `mf-faq pull-index` itself, and the API hot-reloads the new `current` (P7.6).
- **Code deploys** come from Railway's GitHub integration on `main`. **Data deploys** (new index versions) need no redeploy at all.
- **No ingest on Railway.** The container never fetches Groww pages. Fetching stays on GitHub's runners, so the P0.6 / risk 2 fetch-volume assumptions still hold.

### Why the pull runs inside the web service

Railway allows **one volume per service**, attaches each volume to a single service, and **does not allow replicas on a service with a volume** ([volumes reference](https://docs.railway.com/reference/volumes)). A separate Railway cron service therefore can't write into the web service's `data/index/`. The pull loop runs as a background process in the same container instead. It is cheap: `pull-index` imports ChromaDB but not torch, downloads about 0.5 MB, and is a no-op when the version is already live.

---

## 2. Go-live gates (before a public URL)

| # | Gate | Why |
|---|---|---|
| G1 | Phase 7 exit criteria met: `daily-refresh` has run, and `data-latest` exists in the private repo | The container's first boot has no index; it can only get one by pulling |
| G2 | **Phase 9** (evaluation and hardening) passed: golden set, security tests, all gates green | Until then, deploy only as a private preview (§7.1) |
| G3 | Groq capacity decided | Free tier ≈ **5 LLM answers per minute, ~120 a day, shared by all users** (Phase 5 measurement). A public URL needs a paid tier, or must accept "busy" replies |
| G4 | D2 done (embedding-model revision pinned) | Otherwise one upstream model update can stop the server from loading the index (risk R1) |
| G5 | Owner confirmed P7.1 (daily fetch volume, private repo) | Deployment depends on the private releases |

---

## 3. Code changes

### D1: CPU-only torch *(recommended; also speeds up the GitHub workflow)*

`uv.lock` currently resolves the CUDA build of torch on Linux (15 `nvidia-*` packages, several GB). That bloats the image, slows every build and costs storage, and Railway has no GPU anyway. In `pyproject.toml`:

```toml
[tool.uv.sources]
torch = [{ index = "pytorch-cpu", marker = "sys_platform == 'linux'" }]
# …and "torch>=2.14.1" in [project] dependencies: sources only apply to direct dependencies

[[tool.uv.index]]
name = "pytorch-cpu"
url = "https://download.pytorch.org/whl/cpu"
explicit = true
```

Then `uv lock` and check:
- `grep -c '^name = "nvidia' uv.lock` → `0`
- the locked torch version is still available on the CPU index (if it lags, accept the version uv picks and re-run the tests)
- `uv run pytest` passes on macOS (macOS keeps the PyPI wheel, because of the marker)

### D2: Pin the embedding-model revision *(go-live gate G4)*

The retriever refuses an index whose `embed_model_revision` differs from the running encoder's (`Retriever.__init__`). Three different processes download `BAAI/bge-small-en-v1.5` from Hugging Face's `main`: the Docker build, the workflow's model cache and a developer machine. If the model repo is updated, the image built after that can't load the index the workflow built before it. Startup then fails, and every reload logs `index.reload_failed`.

Fix *(done)*: add a `MF_FAQ_EMBED_MODEL_REVISION` setting (a commit hash). Pass it as `SentenceTransformer(model, revision=…)` in `ingest/index.py`, as a build arg in the `Dockerfile`, and in `daily-refresh.yml` (both in `env` and in the cache key). Use the revision recorded in today's `index_meta.json`, so the live index stays valid.

### D3: `Dockerfile` and `.dockerignore` (repo root)

Railway uses a `Dockerfile` at the repo root automatically.

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/opt/hf \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first: this layer is rebuilt only when the lockfile changes.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY corpus/sources.yaml corpus/schemes.yaml ./corpus/
COPY scripts/railway-start.sh ./scripts/
RUN uv sync --frozen --no-dev

# Bake the embedding model into the image: no download at runtime, and every container of
# one image uses the same revision (D2 adds MF_FAQ_EMBED_MODEL_REVISION here).
ARG MF_FAQ_EMBED_MODEL=BAAI/bge-small-en-v1.5
RUN python -c "from sentence_transformers import SentenceTransformer as S; S('${MF_FAQ_EMBED_MODEL}')"
ENV HF_HUB_OFFLINE=1

CMD ["sh", "scripts/railway-start.sh"]
```

```gitignore
# .dockerignore
.git
.github
.venv
.env
.env.*
!.env.example
data/
corpus/raw/
docs/
tests/
scripts/inspect_embeddings.py
stitch_fund_facts_assistant_ui/
.coverage
.DS_Store
**/__pycache__
```

Notes:
- The image runs as root, so the volume mount is writable (a non-root UID needs `RAILWAY_RUN_UID=0`, per Railway's volume docs).
- `tests/eval/` isn't needed: the retrieval-eval gate runs only during ingest, which runs on GitHub.
- Before merging, check the image size (`docker build`), and that `docker run` with an empty `/data` fails clearly (see D4).

### D4: `scripts/railway-start.sh`

```sh
#!/bin/sh
# Railway entrypoint: make sure an index is live, keep pulling new ones, then serve.
set -eu
: "${MF_FAQ_DATA_DIR:=/data}"
export MF_FAQ_DATA_DIR
mkdir -p "$MF_FAQ_DATA_DIR"

if ! mf-faq pull-index; then
  if [ -s "$MF_FAQ_DATA_DIR/index/current" ]; then
    echo "pull-index failed; serving the index already on the volume" >&2
  else
    echo "pull-index failed and /data has no index: check MF_FAQ_GITHUB_REPO/TOKEN and data-latest" >&2
    exit 1
  fi
fi

# The API hot-reloads data/index/current (P7.6); this loop only repoints it.
(
  while true; do
    sleep "${MF_FAQ_PULL_INTERVAL_S:-3600}"
    mf-faq pull-index || echo "pull-index failed; retrying next interval" >&2
  done
) &

exec mf-faq serve --host 0.0.0.0 --port "${PORT:-8000}"
```

- `exec` makes uvicorn PID 1, so Railway's SIGTERM on redeploy reaches it directly. The pull loop dies with the container. A pull killed mid-way is harmless: `current` is only repointed by an atomic rename.
- With an hourly pull, a new index goes live 0–60 minutes after the workflow publishes it, plus up to `MF_FAQ_INDEX_RELOAD_S` (300 s). Footer dates change at the same moment.

### D5: Proxy-aware client IPs (configuration only)

Railway's edge proxy sits in front of the container. Without forwarded headers, every request seems to come from the proxy. The per-IP rate limit (`MF_FAQ_API_RATE_LIMIT`, P6.6) would then be **one shared bucket for all users**. Uvicorn already honours `X-Forwarded-For`, but only from the IPs in `FORWARDED_ALLOW_IPS` (default `127.0.0.1,::1`). Setting **`FORWARDED_ALLOW_IPS=*`** is enough, with no code change. Only Railway's proxy can reach the public port, so trusting it is safe here.

Test after deploy: from two networks, 21 quick `POST /api/threads` calls from one of them get `429`, while the other still gets `201`.

---

## 4. Railway setup (dashboard)

Railway's file-based **Config as Code** (`railway.json` / `railway.toml`) is deprecated: new services can't use it, and existing ones work only until 1 Dec 2026 ([config as code](https://docs.railway.com/reference/config-as-code)). This plan therefore sets everything up in the dashboard. An optional sketch in Railway's newer **Infrastructure as Code** format is in §8.

1. **Project:** *New Project → Deploy from GitHub repo* → grant the Railway GitHub app access to this **private** repo → choose `main`.
2. **Region:** service *Settings → Deploy → Regions*: **Southeast Asia (Singapore)**, the closest to Indian users. Create the volume in the same region.
3. **Volume:** right-click the service → *Attach volume* → mount path **`/data`**, 1 GB. That is plenty: about 2 index versions of a few MB each, plus `app.db`. The Hobby plan allows up to 5 GB.
4. **Build:** builder is the Dockerfile (detected). *Watch paths* (so docs-only commits don't redeploy): `src/**`, `corpus/*.yaml`, `scripts/railway-start.sh`, `Dockerfile`, `pyproject.toml`, `uv.lock`.
5. **Deploy:** leave the start command empty (the image's `CMD` runs). *Healthcheck path* **`/api/health`**, timeout 300 s, which covers loading the model and the boot pull. *Restart policy*: On failure, 5 retries. Replicas: **1**, the only option with a volume.
6. **Source:** turn on *Wait for CI*, so a push deploys only after `ci.yml` passes.
7. **Variables** (§5). Seal the two secrets after entering them.
8. **Networking:** *Generate domain* (`<name>.up.railway.app`, target port = `$PORT`). Later, add a custom domain with the CNAME record Railway shows.

## 5. Variables

| Variable | Value | Notes |
|---|---|---|
| `GROQ_API_KEY` | `gsk_…` | **Sealed.** Required: the app refuses to start without it |
| `MF_FAQ_GITHUB_REPO` | `<owner>/<repo>` | The private repo with the `data-*` releases |
| `MF_FAQ_GITHUB_TOKEN` | fine-grained PAT | **Sealed.** Repository access: this repo only; permission *Contents: read-only*. Set an expiry and add it to the rotation in the runbook |
| `MF_FAQ_DATA_DIR` | `/data` | The volume |
| `FORWARDED_ALLOW_IPS` | `*` | D5 |
| `MF_FAQ_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Must equal the workflow's value and the Docker build arg |
| `MF_FAQ_EMBED_MODEL_REVISION` | `<commit hash>` | After D2; must equal the workflow's value |
| `MF_FAQ_INDEX_RELOAD_S` | `300` | Default; how often the API re-reads `current` |
| `MF_FAQ_PULL_INTERVAL_S` | `3600` | Read by the start script only |
| `MF_FAQ_LOG_LEVEL` | `INFO` | |
| `MF_FAQ_API_RATE_LIMIT` | `20/minute` | Default; per client IP once D5 is set |

`PORT` is set by Railway. Never put `.env` in the image (it's in `.dockerignore`). The other settings keep their defaults from `config.py`.

---

## 6. Rollout

### 6.1 First deploy

1. Merge D1–D4 on a branch. CI goes green, and a local `docker build` + `docker run -e … -v $PWD/tmpdata:/data -p 8000:8000` serves the app.
2. Make sure `data-latest` exists (G1). If not: *Actions → daily-refresh → Run workflow*.
3. Create the Railway service (§4) and variables (§5); the first deploy starts.
4. Watch the deploy logs. In order, they should show: `index.pulled` / `index … now live`, then `api.started` with `index_version`, then the healthcheck passing.

### 6.2 Smoke test (record the results in the Status line at the top)

| Check | Expected |
|---|---|
| `GET /api/health` | `200`, `index_version` and `corpus_version` = the `data-latest` release |
| `GET /api/meta` | 3 examples, 5 schemes, disclaimer |
| UI at `/` | loads with no CSP errors in the browser console |
| Ask each of the 3 example questions | `answer` with a Groww link and "Last updated from sources: DD MMM YYYY" |
| A message with a PAN | PII reply; then `railway ssh` → `sqlite3 /data/app.db` shows `[REDACTED_PAN]` |
| Redeploy the service | threads survive (volume); a short outage while the volume moves over (expected, §9 R3) |
| Rate limit (D5 test) | per-client `429`, not global |
| Next daily refresh that publishes | within ~65 min, `/api/health` shows the new version **without a redeploy** (Phase 7 exit criterion) |

### 6.3 Private preview, then public

Until G2/G3 are met, don't share the generated domain. If it must be online, keep it unlisted and accept the Groq limits. Afterwards, add the custom domain and announce it.

---

## 7. Operations on Railway

These add to the [`runbook.md`](./runbook.md); put them there once deployed.

- **New code:** merge to `main`; Railway deploys after CI passes. To roll back code: *Deployments → previous deployment → Rollback*. The volume, and so the index and threads, isn't touched.
- **New data:** automatic (daily workflow → hourly pull → hot reload). To force it now: `railway ssh` → `mf-faq pull-index`.
- **Bad index:** `railway ssh` → `mf-faq index --rollback`; the API reloads within 300 s. Then fix `data-latest` as in the runbook, or the next hourly pull reinstalls the bad version.
- **Logs:** the app writes one JSON object per line to stderr. In Railway's log view, search for event names: `index.reload_failed`, `index.pulled`, `turn.failed`, `api.error`, `api.rate_limited`. Logs carry no raw user text (P-1).
- **Alert hooks:** enable Railway's deploy-failure notifications (email or webhook). Refresh failures are already reported as GitHub issues (P7.5).
- **Token rotation:** as in the runbook. Update the sealed `MF_FAQ_GITHUB_TOKEN` (this triggers a redeploy), then check that `index.pulled` appears on boot.
- **Backups:** the volume holds only threads (30-day TTL) and a copy of data that is also on GitHub. Turn on volume backups if the plan includes them. If the volume is lost, the next boot pulls the index again; threads are gone.
- **Cost:** usage-based; memory is the main cost (Python + torch + bge-small + ChromaDB, roughly 1 GB). Check the service's metrics after a week and set a usage limit in the workspace settings.

---

## 8. Optional: Infrastructure as Code sketch

Railway's replacement for `railway.json` is a TypeScript file, `.railway/railway.ts`, applied with `railway config plan` / `railway config apply` (or the `railwayapp/config` GitHub Action). Railway doesn't read it during deploys ([IaC overview](https://docs.railway.com/infrastructure-as-code), [reference](https://docs.railway.com/infrastructure-as-code/reference)). The reference doesn't yet document fields for the Dockerfile builder, restart policy or region on a service, so those stay in the dashboard. **Not validated:** run `railway config pull` against the project created in §4 first, and diff against this sketch.

```ts
import { defineRailway, github, preserve, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  // Region id: take it from `railway config pull`; Singapore is assumed here.
  const data = volume("mf-faq-data", { region: "asia-southeast1", sizeMB: 1024 });

  const web = service("mf-faq", {
    source: github("<owner>/<repo>", { branch: "main" }),
    healthcheck: "/api/health",
    healthcheckTimeout: 300,
    replicas: 1, // a service with a volume can't have more
    volumeMounts: { "/data": data },
    env: {
      MF_FAQ_DATA_DIR: "/data",
      FORWARDED_ALLOW_IPS: "*",
      MF_FAQ_GITHUB_REPO: "<owner>/<repo>",
      MF_FAQ_EMBED_MODEL: "BAAI/bge-small-en-v1.5",
      GROQ_API_KEY: preserve(), // secrets stay in Railway, never in the repo
      MF_FAQ_GITHUB_TOKEN: preserve(),
    },
  });

  return project("mf-faq", { resources: [data, web] });
});
```

---

## 9. Risks

| # | Risk | Effect | Mitigation |
|---|---|---|---|
| R1 | Embedding-model revision differs between image and index | Server won't start, or keeps the old index (`index.reload_failed`) | D2 (pin the revision everywhere); the reload already keeps the old index |
| R2 | Groq free-tier limits on a public URL | "Busy, try again in a minute" replies for everyone | G3: paid tier, or stay private |
| R3 | Volume ⇒ single replica, brief downtime per redeploy | Seconds to a minute of outage on each code deploy | Deploy rarely, behind *Wait for CI*; data updates need no deploy |
| R4 | No `data-latest` yet, or the token is wrong or expired, on a fresh volume | Boot fails (start script exits 1), so the deploy fails | G1; clear error message; token expiry on the calendar |
| R5 | CUDA torch in the image (D1 not done) | Multi-GB image, slow builds, higher cost | D1 |
| R6 | Railway's IaC format is new and changing | §8 file drifts from the real API | Dashboard is the source of truth (§4); IaC is optional |
| R7 | Rate limit keyed on the proxy IP | One user can exhaust everyone's quota | D5 (`FORWARDED_ALLOW_IPS=*`) + the D5 test |
| R8 | Volume lost | Threads lost; index re-pulled automatically | Backups if available; threads are short-lived by design |

---

## 10. Done when

- [x] D1–D4 implemented; tests, ruff, actionlint, shellcheck green locally.
- [ ] CI green on GitHub (including the `docker` job); image builds on Railway.
- [ ] Railway service + volume + variables set as in §4–§5; healthcheck green.
- [ ] Every row of the §6.2 smoke test passes, including a daily refresh picked up without a redeploy.
- [ ] Runbook updated with §7.
- [ ] G2 and G3 met before the URL is shared publicly.
