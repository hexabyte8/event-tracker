# Event Tracker

A containerized event finder for one or more cities and nearby towns. It crawls
configured public websites, stores events in SQLite, and serves a searchable web
interface with pictures, dates, venue/address details, map search, event links,
and the original source. No API keys or external database are required.

## Configure discovery

Edit [`config.json`](config.json). **The example.org URLs are placeholders; replace
them with real event listing pages before crawling.** Each source is assigned to
its configured city. Include sources covering surrounding towns, or add towns as
separate entries; there is no automatic radius/geographic search.

```json
{
  "cities": [
    {"name": "Your city", "sources": ["https://your-event-website.org/whats-on"]}
  ],
  "max_pages_per_source": 25,
  "max_depth": 1,
  "request_delay_seconds": 1
}
```

Choose websites publishing **Schema.org Event JSON-LD** (`<script
type="application/ld+json">`), including event subtypes such as MusicEvent and
Festival. Arrays, `@graph`, and direct JSON documents are supported. The crawler
follows same-origin links to the configured depth (0–3) and page limit (1–100).
Seed detail pages directly if listing pages do not link to them. Repeat crawls
update existing events rather than duplicate them, within each city.

The crawler is not a general search engine: it does not execute JavaScript,
bypass logins/paywalls, or extract events from arbitrary unstructured HTML.
Pages without supported structured data produce no events. Use suitable sources
and check crawler logs. Cities can have empty source lists to pause discovery.

It identifies itself as `EventTracker/1.0`, respects robots.txt, request rates and
crawl delays, and skips sources when robots.txt cannot be retrieved (404 means
no rules). Only public HTTP(S) destinations on ports 80/443 are fetched; private
IPs, DNS rebinding, and cross-origin redirects are blocked. Use the canonical
source URL to avoid redirects to another host. Requests time out after 10
seconds and responses are limited to 2 MiB. Respect each site's terms and rights
to event descriptions and images. Images are linked, not copied; loading them
contacts the original image host.

## Run locally

Python 3.12+:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m event_tracker crawl
python -m event_tracker serve
```

Open <http://127.0.0.1:8000>. `serve` is for local development. For production:

```sh
gunicorn --bind 0.0.0.0:8000 --workers 2 'event_tracker.web:create_app()'
```

`EVENT_TRACKER_CONFIG` selects the JSON config (default `config.json`);
`EVENT_TRACKER_DB` selects the SQLite file (default `data/events.sqlite3`).
Both commands also accept `--config`. Schedule the crawl command with your
scheduler; it exits nonzero if any source/page fails and retains previously
collected events. The web process never performs network crawls on page loads.
Configuration is read again on each crawl and web request.

Search by title/description and filter by city. Past events are hidden by default
using their end date (or start date) and the current UTC calendar date. Undated
events remain visible with a prompt to check the source. Enable “Include past
events” to see older records. Removed cities' records remain in storage but are
not displayed. Results are limited to 500; source details may be stale, so always
confirm with the organizer.

## Docker

```sh
docker compose up --build -d web
docker compose --profile crawl run --rm crawler
docker compose logs web
```

The UI is on <http://127.0.0.1:8000>. Both containers share a persistent named
volume. Repeat the crawler command to refresh events. `docker compose down`
preserves the data; `down -v` deletes it. Containers run as UID/GID 10001.

## Kubernetes

The manifests provide a ConfigMap, persistent volume claim, single-replica
Deployment, ClusterIP Service, and a crawler CronJob (every six hours).

1. A GitHub Actions workflow (`.github/workflows/docker-publish.yml`) builds
   the image on every push to `main` and publishes it to
   `ghcr.io/hexabyte8/event-tracker` (tagged `latest`, by branch, by semver
   tag, and by commit SHA). To publish elsewhere instead, build and push
   manually: `docker build -t REGISTRY/event-tracker:VERSION .`
   and `docker push REGISTRY/event-tracker:VERSION`.
2. Replace `event-tracker:local` in both `k8s/app.yaml` and `k8s/crawler.yaml`
   with the published image, e.g. `ghcr.io/hexabyte8/event-tracker:latest`
   (configure image pull credentials separately if the package is private).
   For kind/minikube, load the local image into the cluster instead.
3. Replace the placeholder sources/cities in `k8s/configmap.yaml`.
4. Deploy and run the initial crawl:

```sh
kubectl apply -f k8s/
kubectl rollout status deployment/event-tracker
kubectl create job --from=cronjob/event-tracker-crawler event-tracker-initial-crawl
kubectl logs -f job/event-tracker-initial-crawl
kubectl port-forward service/event-tracker 8000:80
```

The cluster needs a default StorageClass supporting `ReadWriteOnce`, or set
`storageClassName` in the PVC. The CronJob uses pod affinity to share the web
pod's node, allowing both pods to mount the same RWO volume. SQLite requires a
filesystem with reliable local file locking; do not use an NFS-backed volume.
Keep one web replica and one crawler job at a time. Scaling to multiple nodes
requires replacing SQLite with a shared database.

To change cities/sources, edit the ConfigMap and `kubectl apply -f
k8s/configmap.yaml`; mounted config updates propagate after Kubernetes' normal
delay. To change the schedule, edit/apply `k8s/crawler.yaml`. To crawl immediately,
create another uniquely named Job as above (avoid overlapping manually launched
jobs). Use `kubectl get cronjobs,jobs,pods` and `kubectl logs` to manage discovery.

`/healthz` checks database connectivity for readiness/liveness. Containers have
resource limits, no service-account token, no extra Linux capabilities, and a
read-only root filesystem with writable data/tmp mounts. Allow DNS and public
HTTP(S) egress for the crawler in cluster network policies; also deny access to
cluster/private networks as defense in depth.

The application has no public write/admin endpoints. Configuration and crawling
are operator-managed through files/Kubernetes. For public access, put the
Service behind your cluster's TLS ingress and any required authentication or
rate limiting; these are infrastructure-specific and are not bundled here.
Back up the database volume with SQLite-aware tooling (for example SQLite's
online backup API).

## Tests

```sh
python -m unittest discover -s tests -v
```

Tests use temporary databases and fixture pages; they do not crawl the internet.
