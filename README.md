# Agentic RAG Platform

[![Eval Loop](https://github.com/mldll/Prod_RAG/actions/workflows/evaluate.yml/badge.svg)](https://github.com/mldll/Prod_RAG/actions/workflows/evaluate.yml)
[![Deploy](https://github.com/mldll/Prod_RAG/actions/workflows/deploy.yml/badge.svg)](https://github.com/mldll/Prod_RAG/actions/workflows/deploy.yml)

A production-oriented continuation of [Simple_RAG](https://github.com/mldll/Simple_RAG). The core pipeline (hybrid search, cross-encoder re-ranking, multi-hop agent, classical ML routing, semantic cache, LLM-as-judge evaluation) is documented in the [Simple_RAG README](https://github.com/mldll/Simple_RAG#readme). This repository adds an MCP server, pluggable secrets, monitoring, persistent state, a web frontend, and automated deployment of one Docker image to **Azure Container Apps** and **AWS Lambda**.


## Architecture

```
Browser ── Vercel: web/ (Next.js chat, demo sandbox, dashboard) ──► either cloud's API (CORS)

GitHub Actions (OIDC, no stored cloud credentials)
  ├── evaluate.yml      build image → offline evaluation gate
  ├── collect-data.yml  daily: release notes → ingest-batch per cloud → restart → live evaluation gate
  └── deploy.yml        build image → push → deploy → /health warmup → live evaluation gate
                          │                                  │
                          ▼                                  ▼
            Azure Container Apps                   AWS Lambda (container image)
            ├── image:   ACR                       ├── image:   ECR
            ├── secrets: Key Vault                 ├── secrets: Secrets Manager
            │            (Managed Identity)        │            (IAM execution role)
            ├── state:   Blob snapshot (versioned) ├── state:   S3 snapshot (versioned)
            │            + events/ (routing, evals)│            + events/ (routing, evals)
            ├── logs:    Application Insights      ├── logs:    CloudWatch
            └── scales to zero, 1 replica          └── scales to zero, Function URL

Same image in both clouds: FastAPI (/health, /query, /demo/documents, /stats/*, /docs) + MCP server (/mcp)
```

## Features by phase

| Phase | Status | What it adds |
|---|---|---|
| 1. MCP server | ✅ Done | RAG tools (`search_documents`, `route_query`, `get_corpus_stats`, `multi_hop_search`) over MCP: streamable HTTP at `/mcp` on the same API, or stdio for local clients such as Claude Desktop |
| 2. Secret management | ✅ Done | `SECRET_BACKEND` selects the OS keyring, env vars, Azure Key Vault or AWS Secrets Manager; code only calls `get_secret()` |
| 3. Monitoring & evaluation | ✅ Done | Groundedness score, router confidence, PSI drift, `live-eval` against a deployed URL, Azure Monitor telemetry |
| 4. Multi-cloud deployment | ✅ Done | One image to Azure and AWS through GitHub Actions with OIDC; setup and teardown scripts; a live evaluation gate after every deploy |
| 5. Persistent storage | ✅ Done | Corpus, index, document registry and router models live in a versioned snapshot (Azure Blob / S3), restored at startup and published after every write |
| 6. Ingestion | ✅ Done | Public demo sandbox: upload a .txt, .md or .pdf and ask questions about that one document. Admin `ingest-batch`: index an inbox folder into the shared corpus as one snapshot. A corpus probe makes new documents reachable through the classical router |
| 7. Scheduled data pipeline | ✅ Done | Daily `collect-data` workflow: the latest Pydantic AI release notes go through `ingest-batch` in both clouds, followed by a restart and the evaluation gates. Every batch has the LLM write questions per document; the retrieval classifier trains on them, and a routing guard refuses batches that would misroute benchmark questions |
| 8. Web frontend | ✅ Done | Next.js app on Vercel with an Azure/AWS switcher: a chat that shows each answer's route, scores and source passages; the demo sandbox, which uploads the document again by itself when an instance has dropped it; and a dashboard of eval runs, routing drift (PSI) and the corpus. The API now reports how each answer was produced, and stores routing decisions and eval runs as events next to the snapshot |
| 9. A reliable public site | ✅ Done | The LLM's rate limit answers 503 with `Retry-After` (the pages show the wait, and `live-eval` waits once); an empty LLM answer is asked again, then answers 502. Evaluation traffic skips the semantic cache, and a cache hit shows the original route, scores and passages. On Lambda, routing events are written before the response. A routing self-test at startup reports `routing_ok` in `/health`; the deploy and data workflows check it, and the frontend shows "Router failing" |

**Design notes**

- **One image, two runtimes.** Lambda runs the container as a non-root user on a read-only filesystem. The models are baked into the image at build time and all writable state lives under `/tmp/state`, so the same image runs unchanged in both clouds.
- **Persistent state.** Each instance restores the latest snapshot to local disk at startup; on the very first boot it publishes the image's seed corpus and prebuilt index as snapshot #1. A write publishes a new snapshot only if nobody else published first (ETag conditional writes), so a stale instance can never overwrite newer state. If the store is unreachable at startup, the app serves the seed read-only instead of failing. Snapshots rather than a mounted volume keep queries on local disk (SQLite is unreliable on network filesystems) and need no VPC or NAT gateway on Lambda. The semantic cache is deliberately not persisted, and `live-eval`'s requests skip it, so the evaluation gates test the real pipeline. Old versions are kept 30 days for rollback.
- **Evaluation gates.** `evaluate.yml` runs the offline benchmark inside the image on every push and pull request. `deploy.yml` deploys to both clouds, waits until `/health` reports the pushed revision and a passing routing self-test, then runs `live-eval` against each deployment. Both fail below 80% Precision@k.
- **LLM failures.** Groq's SDK retries a rate-limited call twice, waiting as long as Groq asks. If the limit still holds, `/query` and the demo answer 503 with `Retry-After` instead of 500, and `live-eval` waits once before counting the question as failed. A blank answer is asked once more, then answered with 502. At startup the app routes one test question: if the router models can't load (a library version mismatch broke them once), `/health` stays 200 but reports `status: "degraded"` and `routing_ok: false`.
- **Two ingestion lanes.** Public requests never write shared state. The demo sandbox keeps each upload in its own in-memory collection (30 idle minutes, 20 documents and 5 uploads per minute per instance) and answers only from that document. Shared documents go through `ingest-batch`, which only identities with write access to the state store can run (you, and the daily `collect-data` workflow): it starts from the latest snapshot, checks every file before writing anything, embeds only new or changed files and publishes one snapshot per batch.
- **Routing.** One function, `decide_route()` in `src/routing/router.py`, decides for `/query`, the MCP `route_query` tool and the checks below. The retrieval classifier is trained on the benchmark questions plus questions the LLM wrote for every indexed document, against chit-chat and general-knowledge questions, so it changes with the corpus. An exact corpus probe finds the closest chunk: at a cosine similarity of `CORPUS_PROBE_THRESHOLD` (0.55, measured) or more it overrules the classifier's "no retrieval", and that chunk's source is the one searched. An unsure "retrieve" vote (confidence below `CORPUS_PROBE_FLOOR_MAX_CONFIDENCE`, 0.65) only stands if that chunk reaches `CORPUS_PROBE_FLOOR` (0.45); otherwise no document covers the question, and the LLM answers it directly. A question that names an indexed release (`v2.51.0`) goes to that release's notes and retrieves only its chunks, because embeddings don't tell version numbers apart.
- **Publish guard.** After retraining, `ingest-batch` checks that every benchmark question still routes to its own source (and the general one to no retrieval). If not, nothing is published, and the CLI names the questions the new document would capture.
- **Events next to the snapshot.** Per-request data can't go into the snapshot, which only a batch publishes. So for each `/query` the API writes one small JSON object under `events/` in the state container or bucket (after the response; on Lambda before it, because Lambda freezes the instance once the response is sent): the route, the scores, a cache hit, whether `live-eval` sent the request, and the deployed revision, but never the question text. `live-eval` writes one object per run, answers included, to the state store it's configured for (in CI, the target cloud's). `/stats/*` reads them for the dashboard, cached for 60 s. The drift score (PSI) compares users' routing with the classifier's training mix.
- **A separate frontend.** `web/` is a Next.js app whose pages run in the browser and call either cloud's API directly. `CORS_ALLOW_ORIGINS` lists the allowed origins. The backend URLs are compiled in at build time (`NEXT_PUBLIC_*`). It deploys on its own (Vercel), so a frontend change never rebuilds the backend image.

## Local setup

`requirements.txt` is a lock file for Python 3.13 that the image, CI and your local venv all install, so everyone runs the same library versions. That matters beyond tidiness: state snapshots carry pickled router models and a Chroma index, which only load with the versions that wrote them. Install [uv](https://docs.astral.sh/uv/), then:

```powershell
uv venv --seed --python 3.13 .venv; .venv\Scripts\Activate.ps1
uv pip sync requirements.txt requirements-dev.txt --torch-backend cpu
python -m src.app set-secret GROQ_API_KEY       # stored in the OS keyring; or put the keys in .env
python -m src.app set-secret GOOGLE_API_KEY
python -m src.app index                          # builds the index and the router models
python -m src.app serve                          # http://localhost:8000/docs
```

Other commands: `query "<question>"`, `evaluate`, `history`, `live-eval --target-url <url> [--questions <file> --report-only]`, `ingest-batch [--dir inbox] [--skip-questions]`, `collect-releases [--repo owner/name]`, `questions export --out <file>`, `routing-report`, `state status`, `state pull`, `get-secret <name>`.

To change dependencies, edit `requirements.in` (or `requirements-dev.in`), recompile with the command in its header, then run the `uv pip sync` line again. Recompiling keeps the existing pins unless you pass `--upgrade-package <name>`.

### Web frontend

Needs Node.js 24. With the API running (`python -m src.app serve`):

```powershell
cd web
npm ci
Copy-Item .env.example .env.local    # points the Azure button at localhost:8000; set NEXT_PUBLIC_AWS_API_URL too for a second button
npm run dev                          # http://localhost:3000
```

Checks: `npm run lint`, `npm test` (Vitest) and `npm run build`. The API accepts `http://localhost:3000` by default (`CORS_ALLOW_ORIGINS`).

### Test

```powershell
python -m pytest tests/ -v
```

The Azure Blob tests also run when `AZURITE_CONNECTION_STRING` points at a running [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite) emulator; otherwise they are skipped.

To run the image the way Lambda does (read-only filesystem, non-root user; `.env` holds the API keys):

```powershell
docker build -t rag-api:dbg .
docker run --rm -p 8000:8000 --read-only --tmpfs /tmp:rw,size=512m --user 993:990 --memory 2g -e SECRET_BACKEND=env -e LOCAL_DIR=/tmp/state/.local -e DATA_DIR=/tmp/state/data -e HOME=/tmp -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 --env-file .env rag-api:dbg
```

### Configuration

Every setting has a working local default, and the cloud values are set by `infra/azure/main.bicep` and `deploy.yml`. The full list is in [`src/config.py`](src/config.py) and [`.env.example`](.env.example). The ones you are most likely to change:

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_BACKEND` | `keyring` | `keyring`, `env`, `azure_keyvault` or `aws_secretsmanager` |
| `MAIN_LLM_MODEL`, `EVAL_LLM_MODEL` | `groq` | The answering LLM, and the judge plus `ingest-batch`'s question writer: `groq`, `gemini` or `claude_local` (the `claude` CLI on your machine with your own Claude subscription; local runs only: refused in the clouds, CI, containers and with a cloud `STATE_BACKEND`, and `serve` needs `--host 127.0.0.1`) |
| `STATE_BACKEND` | `local` | `local`, `azure_blob` (with `AZURE_STORAGE_ACCOUNT_URL`) or `s3` (with `S3_STATE_BUCKET`) |
| `LOCAL_DIR`, `DATA_DIR` | `<repo>/.local`, `<repo>/data` | Working copy of the index and databases, and of the corpus |
| `MCP_DNS_REBINDING_PROTECTION` | `true` | Set to `false` when `/mcp` is served behind a public hostname |
| `CORS_ALLOW_ORIGINS` | `http://localhost:3000` | Browser origins allowed to call the API, separated by spaces (no commas); `deploy.yml` sets it from the GitHub variable |
| `CORPUS_PROBE_THRESHOLD` | `0.55` | Closest-chunk similarity at which a question is retrieved although the classifier says no; re-measure with `routing-report` after large batches |
| `CORPUS_PROBE_FLOOR`, `CORPUS_PROBE_FLOOR_MAX_CONFIDENCE` | `0.45`, `0.65` | A retrieval vote with confidence below the second value needs a chunk at least this similar, so questions no document covers go straight to the LLM |
| `QUESTION_GEN_PAUSE_SECONDS` | `0` | Pause between the LLM calls that write questions during `ingest-batch`; raise it if the LLM's rate limit bites |
| `GENERATE_VISUALIZATION` | `true` | t-SNE picture of the index on every retrain; the scheduled workflow turns it off |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | – | Enables Azure Monitor telemetry |

## Deploy to Azure and AWS

Prerequisites: PowerShell, the Azure CLI signed in (`az login`) with permission to create role assignments and app registrations, the AWS CLI with an admin profile, and your own GitHub copy of this repository (pass `-GitHubRepo <owner>/<repo>` to both setup scripts).

1. Run `.\infra\azure\setup.ps1`, then `$env:AWS_PROFILE = "<admin-profile>"; .\infra\aws\setup.ps1`. Both ask for the Groq and Google API keys (typing is hidden; Enter skips a key) and print the values for the next step. They also give the GitHub Actions identities read/write access to the state snapshots, which the daily data workflow needs. The AWS script rewrites both IAM roles' trust and inline policies on every run, so rerunning it brings existing roles up to date. Its GitHub role only accepts workflow runs on `main` of the repository given by `-GitHubRepo`, and only acts on this app's ECR repository and Lambda function.
2. Add those values in GitHub → Settings → Secrets and variables → Actions:

   | Secrets | Variables |
   |---|---|
   | `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | `RESOURCE_GROUP`, `ACR_NAME`, `CONTAINER_APP_NAME`, `DEPLOYED_APP_URL` |
   | `AWS_ROLE_TO_ASSUME` | `AWS_REGION`, `AWS_ECR_REPO`, `AWS_LAMBDA_ROLE_ARN`, `AWS_STATE_BUCKET` |
   | `GROQ_API_KEY`, `GOOGLE_API_KEY` (used by the evaluation gates and the question generator) | `COLLECT_REPO` (optional, default `pydantic/pydantic-ai`) |
   | | `CORS_ALLOW_ORIGINS`: the frontend's origins separated by spaces, e.g. `https://<project>.vercel.app http://localhost:3000` (use `http://localhost:3000` until the frontend has a domain) |

3. Push to `main`, or run the Deploy workflow manually (targeting `azure`, `aws` or `both`). The first AWS run creates the Lambda function and its Function URL. The deploy stops before changing anything if `CORS_ALLOW_ORIGINS` is missing or contains a comma.

Re-running the Azure `setup.ps1` later resets the Container App to a placeholder image until the next deploy, so run the Deploy workflow right after it.

### Deploy the web frontend (Vercel)

Deploy the backend first: the frontend needs both API URLs, and the Lambda Function URL only exists after the first AWS deploy.

1. Sign up at Vercel with **Continue with GitHub**. Choose **Add New… → Project**; if this repository isn't listed, grant Vercel's GitHub app access to it, then **Import** it.
2. Set **Root Directory** to `web` (Vercel then detects Next.js), add the environment variables `NEXT_PUBLIC_AZURE_API_URL` (the `DEPLOYED_APP_URL`) and `NEXT_PUBLIC_AWS_API_URL` (the Lambda Function URL), and **Deploy**. A cloud left empty is hidden from the switcher.
3. Put the production domain from Settings → Domains into the GitHub variable `CORS_ALLOW_ORIGINS`, and run the Deploy workflow so both APIs accept requests from it.
4. Optional: in Settings → Git, set the Ignored Build Step to `git diff --quiet HEAD^ HEAD -- .`, so commits that don't touch `web/` don't rebuild the frontend.

Shortcut: Vercel names the domain after the project (`https://<project>.vercel.app`) when nobody else holds that name. Set `CORS_ALLOW_ORIGINS` to it before the backend deploy, then use that project name in step 1, and step 3 is only a check. The backend URLs are compiled into the frontend: after changing one, redeploy in Vercel.

### Check a deployment

```powershell
$AZ_URL  = "https://" + (az containerapp list -g rg-ragprod --query "[0].properties.configuration.ingress.fqdn" -o tsv)
$AWS_URL = (aws lambda get-function-url-config --function-name ragprod-api --region us-east-1 --query FunctionUrl --output text).TrimEnd('/')

curl.exe "$AZ_URL/health"                                              # status, revision, state_version, read_only, routing_ok
Invoke-RestMethod -Method Post -Uri "$AWS_URL/query" -ContentType 'application/json' -Body '{"question":"What is a group of cats called?"}' -TimeoutSec 300
$DOC = curl.exe -s -F "file=@notes.pdf" "$AZ_URL/demo/documents" | ConvertFrom-Json        # demo sandbox: doc_id, chunks, expires_in
Invoke-RestMethod -Method Post -Uri "$AZ_URL/demo/documents/$($DOC.doc_id)/query" -ContentType 'application/json' -Body '{"question":"What is this document about?"}' -TimeoutSec 300
python -m src.app live-eval --target-url $AZ_URL --revision manual     # benchmark against the deployment
curl.exe "$AZ_URL/stats/eval-history"                                  # stored eval runs; also /stats/routing and /stats/corpus
curl.exe -i -X OPTIONS "$AWS_URL/query" -H "Origin: https://<project>.vercel.app" -H "Access-Control-Request-Method: POST"   # CORS: 200 with access-control-allow-origin
```

API docs are at `<url>/docs`, and MCP clients connect to `<url>/mcp`. The web frontend shows the same for either cloud: switch Azure/AWS in its header. Logs: `az containerapp logs show -n <app> -g rg-ragprod --follow` and `aws logs tail /aws/lambda/ragprod-api --follow --region us-east-1`.

### Add documents

Shared documents go through `ingest-batch`, run from your machine once per cloud. It needs write access to the state store: the Azure `setup.ps1` grants you "Storage Blob Data Contributor" on the state container, and your AWS admin profile covers the bucket. Put `.txt`, `.md` and `.pdf` files into `inbox/` (gitignored), then in one PowerShell terminal:

```powershell
# Azure (after az login)
$env:STATE_BACKEND = "azure_blob"
$env:AZURE_STORAGE_ACCOUNT_URL = az storage account list -g rg-ragprod --query "[0].primaryEndpoints.blob" -o tsv
$env:LOCAL_DIR = "$env:TEMP\rag-batch-azure\.local"; $env:DATA_DIR = "$env:TEMP\rag-batch-azure\data"
python -m src.app ingest-batch
$APP = az containerapp list -g rg-ragprod --query "[0].name" -o tsv
$REV = az containerapp show -n $APP -g rg-ragprod --query properties.latestRevisionName -o tsv
az containerapp revision restart -n $APP -g rg-ragprod --revision $REV

# AWS (with $env:AWS_PROFILE set)
Remove-Item Env:AZURE_STORAGE_ACCOUNT_URL
$ACCOUNT = aws sts get-caller-identity --query Account --output text
$env:STATE_BACKEND = "s3"; $env:S3_STATE_BUCKET = "ragprod-state-$ACCOUNT"; $env:AWS_REGION = "us-east-1"
$env:LOCAL_DIR = "$env:TEMP\rag-batch-aws\.local"; $env:DATA_DIR = "$env:TEMP\rag-batch-aws\data"
python -m src.app ingest-batch
aws lambda update-function-configuration --function-name ragprod-api --description "batch $(Get-Date -Format s)" --region us-east-1 | Out-Null
aws lambda wait function-updated --function-name ragprod-api --region us-east-1

Remove-Item Env:STATE_BACKEND, Env:S3_STATE_BUCKET, Env:AWS_REGION, Env:LOCAL_DIR, Env:DATA_DIR
```

`ingest-batch` restores the latest snapshot into the scratch folders, reports files it can't read (exit code 1), embeds only new or changed files and publishes one snapshot; running it again with the same inbox publishes nothing. Running apps keep the previous snapshot until restarted, which is what the restart commands do: on Lambda any configuration change retires the warm instances, and the Azure revision restart takes about a minute after it reports success. Check `/health`: the new `state_version`, and `routing_ok: true` (the router models loaded and routed a test question). Then ask `/query` once, which also proves the LLM answers. Run `live-eval` afterwards, since new documents can change routing. To remove a document, `state pull` into scratch folders, delete it from `DATA_DIR/ingested/batch/`, run `index` (it publishes), then restart the app. Never point a batch at the folders of a running local server: the restore replaces the index under it.

Every batch also has the LLM write five questions per new or changed document, and fills them in for indexed documents that have none yet. This uses your Groq key; `--skip-questions` leaves it out, and a later batch catches up. The retrieval classifier retrains on these questions in the same snapshot. Before publishing, the routing guard checks the benchmark questions: a document that would capture one is refused, and the CLI names the question. `routing-report` on the same scratch folders shows how the benchmark and fixture questions route. Don't run a batch while the Collect Data workflow is running for the same cloud: the later publish fails with `SnapshotConflict`, so run it again.

Run it from a venv synced with the lock (see Local setup): `ingest-batch` and `index` refuse to publish when scikit-learn, numpy, joblib or chromadb differ from `requirements.txt`, because the snapshot carries their file formats.

### Scheduled data pipeline

`.github/workflows/collect-data.yml` runs daily at 06:17 UTC, and on demand from Actions → Collect Data → Run workflow (choose `azure`, `aws` or `both`, and whether to skip the LLM questions). For each cloud, on a fresh GitHub runner, it:

1. writes the latest 10 releases of `COLLECT_REPO` into one inbox file, for example `pydantic-pydantic-ai-releases.md` (`collect-releases`);
2. runs `ingest-batch` against that cloud's state store, signed in through OIDC;
3. if a snapshot was published: restarts the app, waits until `/health` reports the new `state_version` (failing if its routing self-test didn't pass), sends a `/query` smoke test, runs the live evaluation gate, and asks the new documents' generated questions as a report-only synthetic evaluation.

A day without a new release publishes nothing and takes a couple of minutes. The job summary shows the batch result, and the run's artifact keeps the inbox file and the result JSON. To pause the pipeline, disable the workflow in the Actions tab (or `gh workflow disable "Collect Data"`).

The release notes are one source with a 10-release window: a new release replaces the oldest, so the corpus doesn't grow. Every change line starts with its release (`* [v2.51.0] …`) and the newest release is labelled as the latest, so questions about the latest release or a named version are answered from the right release.

### Teardown

Disable the Deploy workflow first (otherwise the next push re-creates the Lambda function), then run `.\infra\azure\teardown.ps1` and `.\infra\aws\teardown.ps1`. They list everything the setup scripts and deploys created, including all state snapshots, ask for confirmation, delete it, and are safe to re-run. Add `-DeleteOidcProvider` to the AWS script to also delete the account's GitHub OIDC provider; it is kept while any other IAM role still trusts it. To deploy again, repeat the steps above; only `AZURE_CLIENT_ID` and `DEPLOYED_APP_URL` change. If you created the Vercel project, delete it under its Settings → Advanced.

## Known limitations

- The endpoints, including `/mcp`, the demo sandbox and the read-only `/stats/*`, are public and unauthenticated: anyone with the URL can read the corpus and use your LLM quota. Demo uploads are rate-limited per instance only.
- Both clouds scale to zero, so the first request after an idle period can take a minute or more.
- On Lambda, a warm instance can serve an older snapshot for a while after a write made elsewhere.
- Editing the seed files in `data/` doesn't change an existing snapshot; add or update documents as described above.
- After a manual `ingest-batch`, running instances serve the previous snapshot until they restart (the scheduled workflow restarts them itself).
- Demo documents live in one instance's memory: a restart, scale-to-zero or 30 idle minutes removes them, and on Lambda a follow-up question can reach another instance and get a 404 (upload again). The 2 MB upload limit is checked after the request body has arrived; the platform caps the request itself (6 MB on Lambda).
- The answer-context similarity shown with answers (`groundedness_score` in the API) is the best cosine similarity between the answer and one of its passages. It flags answers that drift off topic, not wrong facts, and short factual answers (a number, a name) score low even when correct.
- Routing to new documents: in measurements, questions about a new document reached retrieval 11 of 11 times once it had generated questions, and 9 of 10 for a document without them; borderline wordings can still go to the LLM without retrieval. With `ROUTING_METHOD=llm`, ingested documents are unreachable (the LLM router's source list is fixed), and the multi-hop agent's second hop can only target the seed files.
- Questions near the corpus's topics that no document covers (for example about an animal other than cats) are kept away from retrieval by the probe floor: 0 of 31 such test questions were retrieved, down from 17. The two floor values are tuned on small question sets, and a confident classifier vote (0.65 or more) is never floored, so such a question can still end in "I don't know".
- Release notes: only releases inside the 10-release window are known, and a question that names no version ("Which release added GPT-Live?") can name the wrong release.
- The routing guard has no override: if a new document should legitimately answer a benchmark question, update `baseline/questions.json` first.
- GitHub pauses scheduled workflows after 60 days without repository activity; re-enable Collect Data in the Actions tab.
- On Groq's free tier (8,000 tokens per minute for `gpt-oss-20b`), a few visitors or a `live-eval` run can hit the rate limit: one multi-hop answer uses several thousand tokens. After the SDK's two retries, the API answers 503 with `Retry-After`.
- The semantic cache is per instance and starts empty after every deploy (by design). Evaluation traffic (`X-RAG-Client: live-eval`) neither reads nor fills it.
- `live-eval` stores each run in the event store next to the state store it's configured for (in CI, the target cloud's), so the dashboard shows it; runs from before Phase 8 weren't kept. A `live-eval` from your machine with the default `STATE_BACKEND=local` goes to your local store.
- Routing and eval events are kept until deleted (no retention rule yet). On Lambda each `/query` waits for its event's S3 write (tens of milliseconds; at most about 7 s if S3 is failing, after which the event is dropped). Stats are cached 60 s per instance, and PSI is shown once there are 30 routed user questions in 7 days.
- The frontend's backend URLs are fixed at build time (redeploy in Vercel to change them), and Vercel's preview deployments aren't in `CORS_ALLOW_ORIGINS`, so previews can't call the clouds.
- The model can return an empty answer. The API asks once more, then answers 502; the warning in the logs names the finish reason and token usage.
- `/health` stays 200 when the routing self-test fails (`status: "degraded"`), so only checks that read `routing_ok` notice; the deploy and data workflows do.
- Every deploy pushes a multi-GB image to both registries; delete old tags periodically.

## License

MIT
