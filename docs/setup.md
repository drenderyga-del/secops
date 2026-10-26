# Deployment and configuration

For a local tour, use the [README quickstart](../README.md#quickstart).
This guide covers a deployment connected to your own infrastructure.

## 1. Build a versioned image

The production [Dockerfile](../Dockerfile) uses public base images and package
indexes. Build and, if needed, push an image to a registry you control:

```bash
docker build --platform linux/amd64 -t YOUR_REGISTRY/scanops:YOUR_VERSION .
docker push YOUR_REGISTRY/scanops:YOUR_VERSION
```

Replace the uppercase placeholders with your registry and an immutable version.
The public repository does not publish container images or deploy for you.

## 2. Configure the stack

Copy [.env.example](../.env.example) to `.env` and set at least:

| Setting | Purpose |
| --- | --- |
| `SECPIPE_IMAGE` | The versioned image you built. |
| `POSTGRES_PASSWORD` | Password for the deployment database. |
| `UI_AUTH_USERNAME`, `UI_AUTH_PASSWORD` | Operator access to the UI and protected reads. |
| `PIPE_BEARER_TOKEN` | CI authentication for execution telemetry and report delivery. |
| `INTEGRATION_CREDENTIALS_KEY` | Stable key protecting UI-managed integration credentials. |
| `GITLAB_API_URL` | Your GitLab REST API base URL, ending in `/api/v4`. |
| `SECPIPE_PUBLIC_URL` | The external URL of your ScanOps deployment. |

Generate separate random secrets, for example with `openssl rand -hex 32`.
Keep them outside Git. Preserve the encryption key across restarts and services;
changing it makes existing stored credentials unreadable.

Run the local configuration check, then start the stack:

```bash
python3 scripts/preflight_internal.py --env-file .env
docker compose --env-file .env up -d
```

Preflight needs Python 3; the image build does not need a local Node installation.
It checks configuration without contacting your services. Despite its historical
name, the preflight script works with your own deployment configuration.

The stack runs PostgreSQL, the web app, a GitLab pipeline worker and an IaC worker.
Ordered SQL migrations are applied when the web app starts. The web port is
`8081`; use HTTPS and access controls when exposing the deployment.

## 3. Connect GitLab and scanners

Open **Integrations** and add your GitLab API URL and access token. Multiple
GitLab connections are supported. Use **Test connection** for each enabled
connector before relying on worker results.

The UI also configures Vampy/Hexway ASOC, PT AI and Mattermost. Saved connector
credentials are encrypted in PostgreSQL. Environment values are a fallback until
a connector is explicitly managed or disabled in the UI.

- [Execution events](execution-event-ingestion-v1.md): CI lifecycle telemetry.
- [ASOC adapters](asoc-adapters.md): explicit report destinations for Vampy,
  DefectDojo and Dependency-Track. These are separate from the legacy Vampy
  inventory and scan-orchestration connector.
- [External agents](agent-api.md): a dedicated `SCANOPS_AGENT_API_TOKEN`,
  explicitly shared diagnostics and operator-reviewed proposals.

## 4. Check readiness

```bash
curl --fail http://127.0.0.1:8081/api/health
```

Check `operational_status` as well as the HTTP status. For a connected deployment,
it should be `healthy`; `degraded_reasons` explains missing or stale workers,
polling delays and configuration issues. HTTP 200 alone is not an operational
readiness check.

Back up PostgreSQL and the stable encryption key. Reports live in named Docker
volumes. See [.env.example](../.env.example) for the complete environment contract
and the [extended operator notes](operations-reference.md) for
retention, credential recovery and private deployment exports.

## Demo data

The README demo uses a separate Compose project and synthetic data. Stopping it
preserves its database. To remove that demo's data as well:

```bash
docker compose -f docker-compose.demo.yml down -v
```

This removes the demo volumes. Use it only when you want to reset the local demo.
