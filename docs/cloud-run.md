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

Nothing below runs automatically. Run each block yourself, in order.

---

## 0. Variables

```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="asia-southeast2"
export COROS_REGION="eu"            # your COROS account region: eu or us (see note below)
export SERVICE="coros-mcp"
export REPO="coros-mcp"
export IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/coros-mcp:latest"
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
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com
```

## 2. Artifact Registry and image build

Run this from the repository root. Cloud Build uses the `Dockerfile` there.
`.dockerignore` keeps `.env` and local state out of the image.

```bash
gcloud artifacts repositories create "$REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --description="coros-mcp images"

gcloud builds submit --tag "$IMAGE" .
```

> On newer projects, Cloud Build runs as the Compute Engine default service
> account. If the build can't push to Artifact Registry, grant that account
> `roles/artifactregistry.writer` (and `roles/logging.logWriter` for build
> logs), then retry.

## 3. Firestore (Native mode)

```bash
gcloud firestore databases create \
  --location="$REGION" \
  --type=firestore-native
```

This creates the `(default)` database, which the server uses. You don't need
to create the document. The server writes it on first login.

## 4. Secrets

The values are read interactively, so they never land in shell history:

```bash
read -rsp "COROS email: " V && printf '%s' "$V" | gcloud secrets create COROS_EMAIL --data-file=- ; unset V; echo
read -rsp "COROS password: " V && printf '%s' "$V" | gcloud secrets create COROS_PASSWORD --data-file=- ; unset V; echo

# Random endpoint secret (64 hex chars, URL-safe)
openssl rand -hex 32 | tr -d '\n' | gcloud secrets create MCP_AUTH_TOKEN --data-file=-
```

To read the endpoint secret back later (you need it for the connector URL):

```bash
gcloud secrets versions access latest --secret=MCP_AUTH_TOKEN
```

## 5. Dedicated service account (least privilege)

The service account gets only Firestore data access and read access to these
three secrets:

```bash
gcloud iam service-accounts create "$SA_NAME" \
  --display-name="coros-mcp Cloud Run runtime"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA" \
  --role="roles/datastore.user"

for S in COROS_EMAIL COROS_PASSWORD MCP_AUTH_TOKEN; do
  gcloud secrets add-iam-policy-binding "$S" \
    --member="serviceAccount:$SA" \
    --role="roles/secretmanager.secretAccessor"
done
```

## 6. Deploy

```bash
gcloud run deploy "$SERVICE" \
  --image="$IMAGE" \
  --region="$REGION" \
  --service-account="$SA" \
  --min-instances=0 \
  --max-instances=1 \
  --allow-unauthenticated \
  --port=8080 \
  --memory=512Mi \
  --cpu=1 \
  --timeout=300 \
  --set-secrets="COROS_EMAIL=COROS_EMAIL:latest,COROS_PASSWORD=COROS_PASSWORD:latest,MCP_AUTH_TOKEN=MCP_AUTH_TOKEN:latest" \
  --set-env-vars="COROS_TOKEN_BACKEND=firestore,COROS_SKIP_MOBILE=1,COROS_MCP_HIDE_AUTH_TOOLS=1,COROS_TIMEZONE=7,COROS_REGION=$COROS_REGION,COROS_CACHE_DB=/tmp/cache.db"
```

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

Get the service URL:

```bash
export URL="$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')"
echo "$URL"
```

## 7. Smoke test

```bash
export TOKEN="$(gcloud secrets versions access latest --secret=MCP_AUTH_TOKEN)"

curl -i "$URL/health"                     # 200 ok
curl -i -X POST "$URL/mcp"                # 401, empty body

# List tools (does not call COROS)
npx @modelcontextprotocol/inspector --cli "$URL/$TOKEN/mcp" --transport http --method tools/list
```

> `/healthz` also exists, but Cloud Run reserves URL paths ending in `z` on
> its public frontend, so use `/health` from outside. Inside the container
> both work.

## 8. Add it to Claude

**claude.ai / Claude desktop (custom connector).** The connector UI can't set
headers, so put the secret in the path:

1. Go to Settings → Connectors → **Add custom connector**.
2. Fill in:
   - Name: `COROS`
   - Remote MCP server URL: `https://<service-url>/<MCP_AUTH_TOKEN>/mcp`
   - Leave the OAuth fields empty.
3. Save, then enable the connector in a chat from the tools menu.

**Claude Code.** It can send a header, which keeps the secret out of the URL:

```bash
claude mcp add --transport http coros "$URL/mcp" --header "Authorization: Bearer $TOKEN"
```

## Operations

- **Logs:** `gcloud run services logs read "$SERVICE" --region="$REGION"`
- **The secret in the URL path is logged.** Cloud Run's own request logs
  record the full URL, so anyone with log-viewer access to the project can
  see it. (The app's own access log is disabled.) Prefer the bearer header
  wherever the client supports it.
- **Rotate the endpoint secret:**
  ```bash
  openssl rand -hex 32 | tr -d '\n' | gcloud secrets versions add MCP_AUTH_TOKEN --data-file=-
  gcloud run services update "$SERVICE" --region="$REGION" \
    --update-secrets="MCP_AUTH_TOKEN=MCP_AUTH_TOKEN:latest"
  ```
  The `update` creates a new revision so the new value is picked up. Then
  update the connector URL in Claude.
- **Change COROS password:** add a new version of `COROS_PASSWORD`, run the
  same `services update … --update-secrets=COROS_PASSWORD=COROS_PASSWORD:latest`,
  and delete the `coros-mcp/auth` document in the Firestore console. The
  next request then logs in with the new password.
- **Update the code:** rerun `gcloud builds submit --tag "$IMAGE" .`, then
  `gcloud run deploy "$SERVICE" --image="$IMAGE" --region="$REGION"`. The
  other settings carry over.
