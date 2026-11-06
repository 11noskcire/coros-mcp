# Deploying to Google Cloud Run

This runs coros-mcp as a remote MCP server: stateless, scale-to-zero, one
instance at most. You can then add it to Claude as a custom connector.

How it works:

- **Token storage.** The COROS auth token lives in one Firestore document
  (`coros-mcp/auth`), not on local disk, so cold starts reuse it. COROS is
  logged into again only when the token passes its 24h TTL or COROS rejects
  it (result `1019`, HTTP 401/403). Logins inside the instance are serialized,
  so overlapping requests can't trigger two of them.
- **Web-only.** With `COROS_SKIP_MOBILE=1` the server never logs in to the
  mobile API, because a mobile login signs you out of the COROS phone app.
  `get_sleep_data` returns an explanatory error. HRV and resting HR still come
  from `get_daily_metrics`.
- **Endpoint auth.** Every request except the health check must carry the
  shared secret `MCP_AUTH_TOKEN`, either as `Authorization: Bearer <token>` or
  as a path prefix: `https://…/<token>/mcp`.
- **Cache.** The SQLite cache sits in `/tmp` and is lost when the instance
  stops. The next request simply refetches from COROS.

Nothing below runs automatically. The commands use bash. Do the manual deploy
first (steps 0–8). [Deploy from GitHub](#deploy-from-github) then ships code
on every `deploy-v*` tag.

**Branches.** This fork keeps `main` as an untouched copy of upstream
(cygnusb/coros-mcp). The Cloud Run changes live on `cloud`, which is `main`
plus these changes, and only `cloud` deploys. See
[Sync with upstream](#sync-with-upstream).

---

## 0. Variables

```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="asia-southeast2"
export COROS_REGION="eu"            # your COROS account region: eu or us (see note below)
export SERVICE="coros-mcp"
export SA_NAME="coros-mcp-run"
export SA="$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"
```

> The COROS region is your account's home shard, not where you live. If calls
> fail with `1019` right after a successful login, switch `eu` ↔ `us`.

## 1. Enable APIs

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  logging.googleapis.com
```

## 2. Let source deploys build

`gcloud run deploy --source .` uploads the repository, minus what
`.gcloudignore` excludes, and builds the `Dockerfile` with Cloud Build.
`.dockerignore` keeps `.env` and local state out of the image. The build runs
as the Compute Engine default service account, which needs the Cloud Run
Builder role. The grant can take a couple of minutes to take effect, and it's
harmless if the account already has it.

`--condition=None` on the project-level grants in this guide skips a prompt.
gcloud asks for a condition whenever the project policy already has a
conditional binding, for example the temporary one the console's Developer
Connect setup adds.

```bash
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --role=roles/run.builder \
  --condition=None
```

## 3. Firestore (Native mode)

```bash
gcloud firestore databases create \
  --location="$REGION" \
  --type=firestore-native
```

This creates the `(default)` database, which the server uses. If the project
already has one, skip this step; the server only uses the `coros-mcp`
collection. You don't need to create the document. The server writes it on
first login.

## 4. Secrets

None of the values end up in shell history. `read -s` keeps them off the
screen, and `printf` is a bash builtin, so they don't appear in the process
list. `MCP_AUTH_TOKEN` is generated and piped straight in (64 hex characters,
URL-safe).

```bash
read -rsp 'COROS email, then Enter: ' V; echo
printf %s "$V" | gcloud secrets create COROS_EMAIL --replication-policy=automatic --data-file=-
read -rsp 'COROS password, then Enter: ' V; echo
printf %s "$V" | gcloud secrets create COROS_PASSWORD --replication-policy=automatic --data-file=-
unset V

openssl rand -hex 32 | tr -d '\n' | gcloud secrets create MCP_AUTH_TOKEN \
  --replication-policy=automatic --data-file=-
```

## 5. Dedicated service account (least privilege)

The service account gets only Firestore data access and read access to these
three secrets:

```bash
gcloud iam service-accounts create "$SA_NAME" \
  --display-name="coros-mcp Cloud Run runtime"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA" \
  --role="roles/datastore.user" \
  --condition=None

for S in COROS_EMAIL COROS_PASSWORD MCP_AUTH_TOKEN; do
  gcloud secrets add-iam-policy-binding "$S" \
    --member="serviceAccount:$SA" \
    --role="roles/secretmanager.secretAccessor"
done
```

## 6. Keep the secret out of request logs

**Do this before the first request.** The claude.ai connector carries the
secret in the URL, so it can appear in Cloud Run's automatic request logs,
where anyone with log access could read it. Cloud Run has no setting to turn
those logs off, so this excludes them in Cloud Logging. Entries already
written stay until they expire.

```bash
gcloud logging sinks update _Default --add-exclusion='name=coros-mcp-requests,filter=resource.type="cloud_run_revision" AND resource.labels.service_name="coros-mcp" AND log_id("run.googleapis.com/requests")'
```

You lose request logs for this service, including status and latency. App
logs (stdout/stderr) are unaffected. The app's own access log is already
disabled. Step 8 checks that the exclusion works.

## 7. Deploy

Run this from the repository root with the `cloud` branch checked out. If it
asks to create an Artifact Registry repository, answer yes.

```bash
gcloud run deploy "$SERVICE" \
  --source . \
  --region="$REGION" \
  --service-account="$SA" \
  --min-instances=0 \
  --max-instances=1 \
  --allow-unauthenticated \
  --port=8080 \
  --memory=512Mi \
  --cpu=1 \
  --timeout=300 \
  --set-secrets="COROS_EMAIL=COROS_EMAIL:1,COROS_PASSWORD=COROS_PASSWORD:1,MCP_AUTH_TOKEN=MCP_AUTH_TOKEN:1" \
  --set-env-vars="COROS_TOKEN_BACKEND=firestore,COROS_SKIP_MOBILE=1,COROS_MCP_HIDE_AUTH_TOOLS=1,COROS_TIMEZONE=7,COROS_REGION=$COROS_REGION,COROS_CACHE_DB=/tmp/cache.db"
```

Secrets are pinned to version 1 rather than `latest`, as Google recommends
for environment variables. Otherwise, instances of one revision could start
with different secrets. [Rotation](#rotate-a-secret) bumps the version
explicitly.

Why these flags:

- **`--max-instances=1` is critical.** Every COROS login invalidates the
  previous token. Two instances refreshing independently would keep killing
  each other's tokens. The login lock works only within one process.
  - During a revision rollout an old and a new instance can briefly overlap.
    Under the lock the server re-reads Firestore before it logs in, and it
    adopts a newer token if one exists, so this resolves itself.
- **`--allow-unauthenticated`.** Claude's connector can't present Google IAM
  credentials. The app enforces `MCP_AUTH_TOKEN` itself, and requests without
  it get a bare `401`.
- **`--min-instances=0`.** The service scales to zero when idle, and the first
  request after idle takes a few seconds. The token survives in Firestore, so
  a cold start doesn't trigger a login.
- **`COROS_MCP_HIDE_AUTH_TOOLS=1`.** Your password never passes through the
  model, and login happens only via the env secrets.
- **Optional `COROS_MCP_TOOLSET=readonly`.** Add it to `--set-env-vars` if the
  remote server shouldn't expose tools that write to your COROS account
  (create, schedule, delete workouts).

## 8. Test it

Build the MCP URL. Your history keeps these commands, but not the secret
they fetch.

```bash
SERVICE_URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
MCP_URL="$SERVICE_URL/$(gcloud secrets versions access latest --secret=MCP_AUTH_TOKEN)/mcp"
```

**Test 1: health.** Expect `200`.

```bash
curl -s -o /dev/null -w '%{http_code}\n' "$SERVICE_URL/health"
```

> `/healthz` also exists, but Cloud Run reserves URL paths ending in `z` on
> its public frontend, so use `/health` from outside. Inside the container
> both work.

**Test 2: use a wrong secret.** Expect `401`.

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$SERVICE_URL/wrong-secret-0123456789abcdef/mcp"
```

**Test 3: list the tools.** This doesn't call COROS. Expect a tool list
without `authenticate_coros`.

```bash
npx @modelcontextprotocol/inspector --cli "$MCP_URL" --transport http --method tools/list
```

**Test 4: one real read.** Call `get_daily_metrics` once, for example from
Claude after step 9. Then open the Firestore console and check that
`coros-mcp/auth` exists with an `updated_at` field. Call it again a minute
later, and again after about 20 idle minutes, when the instance has scaled to
zero. `updated_at` must not change: that confirms a cold start reuses the
stored token instead of logging in again.

**Check the log exclusion:**

```bash
gcloud logging read 'resource.labels.service_name="coros-mcp" AND log_id("run.googleapis.com/requests")' --limit=5 --freshness=1h
```

If this prints entries, the exclusion isn't working. Fix step 6, then
[rotate `MCP_AUTH_TOKEN`](#mcp_auth_token), because the logged value should
be treated as exposed.

## 9. Add it to Claude

**claude.ai / Claude desktop (custom connector).** The connector UI can't set
headers, so the secret goes in the path. The URL contains the secret, so
paste it only into the connector form.

```bash
echo "$MCP_URL"
```

1. Go to Settings → Connectors → **Add custom connector**.
2. Fill in:
   - Name: `COROS`
   - Remote MCP server URL: the printed URL
   - Leave the OAuth fields empty.
3. Save, then enable the connector in a chat from the tools menu.

**Claude Code.** It can send a header, which keeps the secret out of the URL:

```bash
claude mcp add --transport http coros "$SERVICE_URL/mcp" \
  --header "Authorization: Bearer $(gcloud secrets versions access latest --secret=MCP_AUTH_TOKEN)"
```

---

## Deploy from GitHub

Once this is set up, pushing a tag that starts with `deploy-v`, such as
`deploy-v1.0.0`, runs ruff, mypy and the tests, then deploys that commit. The
same workflow, [deploy.yml](../.github/workflows/deploy.yml), runs the
checks without deploying on every push and pull request to `cloud`. Upstream's
[CI workflow](../.github/workflows/ci.yml) only covers `main`.

The prefix is `deploy-v`, not `v`, because this fork carries upstream's `v*`
release tags, and those must never deploy: upstream's code has no
`serve-http`.

GitHub signs in to Google with Workload Identity Federation, so no Google key
is stored in GitHub. Only `deploy-v*` tags in your repository can sign in,
and the deployer account can't read your secrets.

Finish the manual deploy above first. The first deploy sets the secrets,
service account, scaling and env vars, and creates the build resources. CI
only ships code, so it leaves those settings alone. Secret rotation works the
same as before.

### 1. Enable Actions on the fork

GitHub disables workflows on forks until you turn them on. Open the
repository's **Actions** tab and enable them.

```bash
export GITHUB_REPO=11noskcire/coros-mcp
export PROJECT_ID=your-project-id
```

### 2. Let GitHub sign in to Google

If you already created the `github` pool in this project (for telegram-mcp,
say), skip the `workload-identity-pools create` command. The provider is
separate per repository.

```bash
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com

gcloud iam workload-identity-pools create github \
  --location=global \
  --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc coros-mcp \
  --location=global \
  --workload-identity-pool=github \
  --issuer-uri=https://token.actions.githubusercontent.com \
  --attribute-mapping='google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref' \
  --attribute-condition="assertion.repository == '$GITHUB_REPO' && assertion.ref.startsWith('refs/tags/deploy-v')"
```

### 3. Create the deployer account

```bash
DEPLOYER="coros-mcp-deployer@$PROJECT_ID.iam.gserviceaccount.com"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
POOL="projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github"

gcloud iam service-accounts create coros-mcp-deployer --display-name="coros-mcp deployer"

for ROLE in roles/run.sourceDeveloper roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$DEPLOYER" \
    --role="$ROLE" \
    --condition=None
done

# The service's account, and the Compute Engine default account that runs the build.
for ACCOUNT in "coros-mcp-run@$PROJECT_ID.iam.gserviceaccount.com" \
  "$PROJECT_NUMBER-compute@developer.gserviceaccount.com"; do
  gcloud iam service-accounts add-iam-policy-binding "$ACCOUNT" \
    --member="serviceAccount:$DEPLOYER" \
    --role=roles/iam.serviceAccountUser
done

gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/$POOL/attribute.repository/$GITHUB_REPO"
```

These changes can take up to five minutes to take effect.

The second grant lets CI run builds as the Compute Engine default account. On
older projects that account often has `roles/editor`, which is far more than
it needs. If yours does, consider removing it once you've confirmed nothing
else depends on it. Builds need `roles/run.builder` (step 2).

### 4. Set the repository variables

None of these values are secret, so they're variables rather than GitHub
secrets.

```bash
gh variable set GCP_PROJECT_ID --repo "$GITHUB_REPO" --body "$PROJECT_ID"
gh variable set DEPLOYER_SERVICE_ACCOUNT --repo "$GITHUB_REPO" --body "$DEPLOYER"
gh variable set WIF_PROVIDER --repo "$GITHUB_REPO" --body "$POOL/providers/coros-mcp"
```

### 5. Limit who can push deploy tags

Anyone who can push a `deploy-v*` tag can deploy. The workflow's check that a
tag is on `cloud` guards against mistakes, not against someone with write
access, because they could edit the workflow in the tagged commit. This repo
is public, so tag rulesets are free. In the repository settings, go to
**Rules → Rulesets → New tag ruleset**, target `deploy-v*`, and restrict
creation, update and deletion to yourself.

### 6. Push and tag

Pushing `cloud` runs the checks only. Pushing the tag triggers the deploy.

```bash
git push -u origin cloud
git tag deploy-v1.0.0 cloud
git push origin deploy-v1.0.0
```

Push the tag by name. Don't use `git push --tags`, which would also push any
upstream tags you've fetched (harmless now, but noisy).

For later releases, push your changes to `cloud`, then tag that commit with a
new version. The deploy fails if the tagged commit isn't on `cloud`. To roll
back, tag the older commit with a new version. A failed test blocks the
deploy, and a revision that fails its startup checks doesn't receive traffic.

### 7. Check the deploy

When the run succeeds, confirm the service kept its settings. The output
should still list all three secrets, at the versions you set, the
`coros-mcp-run` service account, and the env vars from step 7.

```bash
gcloud run services describe coros-mcp --region asia-southeast2
```

Then repeat test 3 from step 8.

---

## Sync with upstream

`main` never gets your own commits, so updating it is always a fast-forward.
Then merge it into `cloud`:

```bash
git remote add upstream https://github.com/cygnusb/coros-mcp   # once

git fetch upstream --no-tags
git checkout main && git merge --ff-only upstream/main && git push origin main

git checkout cloud && git merge main
pytest -q && ruff check . && mypy . --exclude tests/
git push origin cloud              # runs the checks; nothing deploys yet
```

Since `main` has no commits of its own, GitHub's **Sync fork** button on
`main` is also safe, in place of the first two git lines. Never use it on
`cloud`. On a diverged branch it can offer to **discard your commits**.

Rules of thumb:

- **Merge, don't rebase `cloud`.** Deploy tags point at `cloud` commits.
  Rewriting them takes old tags off the branch, and then rolling back by
  re-tagging an older commit fails the "on cloud" check.
- **`uv.lock` conflicts** are the usual case, because upstream merges
  dependency bumps often. Take upstream's file and re-add the `cloud` extra:
  `git checkout --theirs uv.lock && uv lock && git add uv.lock`.
- **`--no-tags`** keeps upstream's `v*` release tags out of your repository.
  They can't deploy anyway, but this avoids the clutter.
- **Deploying the result** is a separate, deliberate step: push a new
  `deploy-v*` tag once the checks on `cloud` pass.
- **Changes meant for upstream:** branch from `main` (= upstream), not from
  `cloud`, so the PR doesn't drag in the Cloud Run changes.

---

## Rotate a secret

### `MCP_AUTH_TOKEN`

The connector stops working between the deploy and the URL update.

```bash
openssl rand -hex 32 | tr -d '\n' | gcloud secrets versions add MCP_AUTH_TOKEN --data-file=-
```

That prints the new version number. Use it below, then update the connector.

```bash
NEW_VERSION=2   # replace with the printed number
gcloud run services update "$SERVICE" --region "$REGION" \
  --update-secrets="MCP_AUTH_TOKEN=MCP_AUTH_TOKEN:$NEW_VERSION"

SERVICE_URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo "$SERVICE_URL/$(gcloud secrets versions access "$NEW_VERSION" --secret=MCP_AUTH_TOKEN)/mcp"
```

1. In claude.ai, remove the connector and add it again with the new URL.
   For Claude Code, rerun `claude mcp add` with the new header.
2. Check that the connector works.
3. Disable the old version:

```bash
gcloud secrets versions disable 1 --secret=MCP_AUTH_TOKEN   # replace 1 with the old version
```

### `COROS_PASSWORD` (after changing it in COROS)

```bash
read -rsp 'New COROS password, then Enter: ' V; echo
printf %s "$V" | gcloud secrets versions add COROS_PASSWORD --data-file=-
unset V

NEW_VERSION=2   # replace with the printed number
gcloud run services update "$SERVICE" --region "$REGION" \
  --update-secrets="COROS_PASSWORD=COROS_PASSWORD:$NEW_VERSION"
```

Then delete the `coros-mcp/auth` document in the Firestore console. The next
request logs in with the new password. The connector URL doesn't change.

## Logs

```bash
gcloud run services logs read "$SERVICE" --region="$REGION"
```
