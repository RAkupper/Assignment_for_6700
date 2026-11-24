# Deployment on Jetstream2

How the service runs on our team instance, how to start and check it, and what to
do when something breaks. The stack is defined in [`compose.yml`](../compose.yml).

## What runs where

```text
TEAM02 (Jetstream2 m3.small: 2 vCPUs, 6 GB RAM, 20 GB root disk + 40 GB volume)
├── caddy container, ports 80 and 443 (public), with COMPOSE_PROFILES=proxy
│     └── HTTPS for APP_DOMAIN, passes each request to app:8000
├── app   container, port 8000 (instance only, APP_BIND=127.0.0.1)
│     ├── GET  /health    the app process is up
│     ├── GET  /ready     configured LLM and enabled retrieval dependencies are ready
│     ├── POST /admin/generate  asks the llm container; needs X-Admin-Key
│     ├── POST /retrieve  returns matching provisions from Qdrant
│     └── POST /ask       retrieves evidence and generates a cited answer
├── llm   container, port 8080 (internal only)
│     └── llama.cpp server with Qwen2.5-1.5B-Instruct (Q4_K_M)
│         weights in the named volume llm-models
├── qdrant container, port 6333 (internal only)
│     └── vector database for retrieval, storage on the volume
└── jobs  one-off container (docker compose run), same image as app
      └── ingestion and Qdrant loading, writes /data on the volume
```

- The LLM runs in its own container so the app image stays small and the model can
  be swapped through env vars. `/admin/generate` and `/ready` return 503
  while it is unavailable; `/health` stays 200.
- We use llama.cpp rather than Ollama because the Ollama image (3.78 GB) does not
  fit next to a model on the 20 GB disk. The llama.cpp image is 309 MB.

## Start the stack

1. In [Exosphere](https://jetstream2.exosphere.app), open TEAM02. If it is shelved,
   choose **Actions → Unshelve** and wait for **Ready**.
2. Use the instance's hostname, `team02.cis240466.projects.jetstream-cloud.org`,
   not its IP. The public IP can change on unshelve (it changed twice on Oct 28 and
   twice on Nov 20, and one old IP went to another project's machine), but the
   hostname's DNS record follows it, so nothing needs updating.
3. From your laptop (your SSH key must be in the instance's `authorized_keys`):

   ```bash
   ssh exouser@team02.cis240466.projects.jetstream-cloud.org
   ```

   The first time, check that SSH shows the host key
   `SHA256:P3MOMLulOLoEUUm1IJ8DnZTVCjap2jvZdEr9cztdOHA` before answering `yes`.

4. On the instance, from a fresh clone:

   ```bash
   git clone https://github.com/RAkupper/Assignment_for_6700.git
   cd Assignment_for_6700
   docker compose up -d
   docker compose ps
   ```

The first start downloads the model weights (1.1 GB) into the `llm-models`
volume; later starts reuse them. `app` starts once `llm` reports healthy.
Compose also enables generation and waits for Qdrant's health check. `/ready`
returns 503 until a compatible, nonempty retrieval index is loaded and activated.
`/health` can pass before that happens. The complete sequence is in
[the RAG quickstart](rag-quickstart.md).

## Data volume (first time only)

The root disk is 20 GB and Docker filled most of it. Everything large goes on the
40 GB volume `team02-data`: Docker's images and volumes, which include the ingestion
data, Qdrant storage and the embedding models. Done on TEAM02 on Oct 28; repeat only for a new
instance or volume.

TEAM02 uses Docker's containerd image store, so images live in `/var/lib/containerd`
and volumes in `/var/lib/docker`. Both move. Exosphere's own automount is replaced
because it mounts after boot, too late for Docker to wait on, and with
`nosuid,nodev`, which can break setuid binaries in containers.

1. In Exosphere: **Create → Volume**, name `team02-data`, 40 GB, then **Attach** it
   to TEAM02 (unshelve first).

2. Over SSH, not the web shell (the web shell runs in Docker). Mount the volume from
   fstab instead of Exosphere's automount. Check the device name with `lsblk` and
   the UUID with `sudo blkid /dev/sdb`:

   ```bash
   V=/media/volume/team02-data
   docker compose down
   sudo cp /etc/fstab /etc/fstab.bak
   sudo systemctl stop automount-volume@sdb.service
   sudo systemctl mask automount-volume@sdb.service
   echo "UUID=<uuid> $V ext4 defaults,nofail,x-systemd.device-timeout=90s 0 2" | sudo tee -a /etc/fstab
   sudo systemctl daemon-reload
   sudo mkdir -p $V && sudo mount $V
   findmnt -no SOURCE,OPTIONS -M $V   # /dev/sdb rw,relatime
   ```

3. Copy Docker and containerd onto the volume and bind-mount them back. The old
   folders are renamed, not deleted, until the reboot test passes.

   ```bash
   sudo systemctl stop docker docker.socket containerd
   sudo rsync -aHAX --numeric-ids /var/lib/containerd/ $V/containerd/
   sudo rsync -aHAX --numeric-ids /var/lib/docker/ $V/docker/
   sudo mv /var/lib/containerd /var/lib/containerd.old
   sudo mv /var/lib/docker /var/lib/docker.old
   sudo mkdir /var/lib/containerd /var/lib/docker
   printf '%s\n' \
     "$V/containerd /var/lib/containerd none bind,nofail,x-systemd.requires-mounts-for=$V 0 0" \
     "$V/docker /var/lib/docker none bind,nofail,x-systemd.requires-mounts-for=$V 0 0" \
     | sudo tee -a /etc/fstab
   sudo mkdir -p /etc/systemd/system/containerd.service.d /etc/systemd/system/docker.service.d
   printf '[Unit]\nRequiresMountsFor=/var/lib/containerd\n' | sudo tee /etc/systemd/system/containerd.service.d/volume.conf
   printf '[Unit]\nRequiresMountsFor=/var/lib/docker /var/lib/containerd\n' | sudo tee /etc/systemd/system/docker.service.d/volume.conf
   sudo systemctl daemon-reload
   sudo mount /var/lib/containerd && sudo mount /var/lib/docker
   sudo systemctl start containerd docker
   docker images && docker volume ls   # images and llm-models still there
   ```

4. Start the stack. Compose creates its named volumes inside `/var/lib/docker`, which
   is now on the volume:

   ```bash
   docker compose up -d
   docker volume ls   # llm-models plus <project>_catalog-data, _qdrant-storage, ...
   ```

5. Reboot test, then free the root disk:

   ```bash
   sudo reboot
   # after reconnecting:
   for p in $V /var/lib/docker /var/lib/containerd; do findmnt -no SOURCE,OPTIONS -M $p; done
   ls /media/volume                       # only team02-data, no team02-data-sdb
   curl -s localhost:8000/ready
   sudo rm -rf /var/lib/docker.old /var/lib/containerd.old ./volume
   ```

Docker and containerd will not start unless the volume is mounted, so a missing
volume stops the stack instead of filling the root disk.

| On the volume | Mounted at | Holds |
| --- | --- | --- |
| `containerd/` | `/var/lib/containerd` | image layers (~3.6 GB) |
| `docker/` | `/var/lib/docker` | containers and every named volume below |

| Named volume | Mounted at | Used by |
| --- | --- | --- |
| `llm-models` | `/models` | llm: model weights (~1.1 GB) |
| `catalog-data` | `/data` | jobs: raw downloads, manual captures, `catalog.sqlite` (17 MB on Nov 5) |
| `embed-cache` | `/embed-cache` | app and jobs: BGE and BM25 models (65 MB) |
| `qdrant-storage` | `/qdrant/storage` | qdrant (50 MB) |
| `qdrant-snapshots` | `/qdrant/snapshots` | qdrant snapshots |

Compose prefixes all but `llm-models` with the project name, the clone's folder name
(`assignment_for_6700_catalog-data`). The image creates `/data` and `/embed-cache`
owned by the app user (UID 10001), so new volumes need no `chown`.

Measured Oct 28: root disk went from 2.8 GB to 7.5 GB free; the volume holds 4.3 GB.

## Move the data into Docker volumes (once, #52)

Until #52, Compose bind-mounted `$DATA_VOLUME_DIR/{data,embed-cache,qdrant,qdrant-snapshots}`.
On an instance that still has those folders, move their contents into the named
volumes after pulling #52, while the old containers are still running:

```bash
cd ~/Assignment_for_6700
git pull
scripts/migrate_to_volumes.sh /media/volume/team02-data
```

The script builds the new app and jobs images, records the alias, collections, point
counts and catalog integrity from the running stack, stops app and qdrant, copies each
folder into its volume, starts everything on the new images, and checks the same
things again. It prints `migrated` when they match and
exits with an error otherwise. It never changes the old folders, so a failed run can
simply be repeated.

Measured Nov 6 on a copy of TEAM02's data (3,882 points, catalog 17 MB), starting from
the main branch's stack: 25 s, before and after identical, the app afterwards on
`127.0.0.1:8000` only. The volumes came out the same size as the folders (Qdrant 25 MB,
its files stay sparse).

Run on TEAM02 itself on Nov 21: before and after identical (alias `provisions` ->
`provisions_v2`, 3,882 points in both collections, catalog ok), with `/var/lib/docker`
on the 40 GB volume. The evaluation set from #58 scored 19/25 before and after the
move.

Once it passes, remove `DATA_VOLUME_DIR` from `.env`; Compose no longer reads it.
Keep the old folders until a backup has been restored successfully (#52), then
delete them:

```bash
sudo rm -rf /media/volume/team02-data/{data,embed-cache,qdrant,qdrant-snapshots}
```

## Look at or copy the data

The volumes are only reachable through containers. A few commands, from the clone:

```bash
docker compose run --rm jobs ls -la /data                            # what is there
docker compose run --rm jobs python -m myapp.vectordb.collection status
docker run --rm -v assignment_for_6700_catalog-data:/data:ro busybox du -sh /data
```

To use an existing catalog instead of fetching, copy it into the volume and migrate:

```bash
docker compose run --rm -T -v "$PWD/catalog.sqlite:/import.sqlite:ro" jobs \
  python -c "import sqlite3; sqlite3.connect('/import.sqlite').backup(sqlite3.connect('/data/catalog.sqlite'))"
docker compose run --rm jobs python -m myapp.ingest.catalog migrate
```

## Resource limits

The m3.small has 2 vCPUs and 6 GB RAM, shared by every container.

- `llm` is capped at 3 GB (`LLM_MEM_LIMIT`). The model needs about 1.3 GB, so the
  cap only matters if something goes wrong, and then only `llm` is killed.
- A `jobs` run gets one CPU (`JOBS_CPUS`), so `llm` keeps the other while a load
  embeds. A full load of 8 CFR 214 then takes about twice as long as on two cores.
  Run loads when nobody is testing, and say so in the group chat.
- `llm` runs two slots with 4096 tokens each (`-c 8192 -np 2`), and the app lets at
  most that many questions use them (`GENERATION_MAX_CONCURRENT`, set from
  `LLM_PARALLEL`). A third question waits up to `GENERATION_QUEUE_TIMEOUT_S` (60 s)
  for a slot, then gets the retrieved passages with `status: "model_unavailable"`.
  Measured Nov 6, six questions at once still all got answers within 64 s. `/retrieve` and questions the index cannot
  answer do not call the model, so they never wait for a slot. Change both slot settings together: `LLM_CTX_SIZE` is 4096 times
  `LLM_PARALLEL`.

Measured on TEAM02 on Oct 28, full 8 CFR 214 index (3,882 points):

| What | Result |
| --- | --- |
| Loader (`--activate`, one CPU) | 20 min |
| `scripts/smoke_rag.py`, one question | 25 s, all checks pass |
| One `/ask` prompt | about 1,400 tokens |
| 3 `/ask` at once, 4 slots on one 4096 cache | all 503 after 47 s: "Context size has been exceeded" |
| 3 identical `/ask` at once, `-c 8192 -np 2` | all 200 in 48 to 49 s; llm at 1.0 of 3 GB |
| 3 different `/ask` at once, 30 s queue timeout | 200 in 43 s and 46 s; third 503 "busy" at 30 s |
| Same 3 again right after, 60 s queue timeout | 200 in 3, 17 and 21 s (prompts reused from llama.cpp's cache) |
| 3 new `/ask` at once after restarting `llm`, 60 s queue timeout | all 200 in 33 to 35 s |

On two cores, two questions at once each take about as long as two in a row, so
expect about 20 s for one question, 48 s for two at once, and a third to wait for
a slot. With a 30 s wait, the third always gave up about 15 s before a slot freed,
so the default is 60 s: a waiting answer still finishes well inside the 120 s LLM
timeout. llama.cpp keeps each slot's last prompt, so a repeated question can take
a few seconds; restart `llm` before measuring cold latency.

## Load the regulations into Qdrant

Ingestion and loading are one-off jobs, never part of app startup (see
[ingestion.md](ingestion.md)). The same Compose file supplies jobs and the API
with a shared Qdrant URL and embedding cache.

```bash
docker compose run --rm jobs python -m myapp.ingest.catalog migrate
docker compose run --rm jobs python -m myapp.ingest fetch --part "8 CFR 214"
docker compose run --rm jobs python -m myapp.ingest parse --part "8 CFR 214"
docker compose run --rm jobs python -m myapp.vectordb.loader --activate
docker compose run --rm jobs python -m myapp.vectordb.search "Can I work on campus?" --visa F-1
```

`run` starts qdrant first if it is not up. The loader defaults to
`INGEST_DATA_DIR/catalog.sqlite`, which is `/data/catalog.sqlite` inside jobs.
The loader builds a new collection
and moves the `provisions` alias to it. To roll back, move the alias back:
`docker compose run --rm jobs python -m myapp.vectordb.collection activate provisions_v1`.

## HTTPS with Caddy (first time only)

1. Find the instance's DNS name. Jetstream2 names each instance
   `<instance>.<project>.projects.jetstream-cloud.org`; Exosphere shows it on the
   instance page. Check it resolves to the current public IP:
   `dig +short <name>`.
2. Ports 80 and 443 must be open in the instance's security group (Exosphere,
   instance page). Caddy needs 80 to get the certificate, even though it then
   redirects to 443.
3. On the instance, add to `.env`:

   ```bash
   COMPOSE_PROFILES=proxy
   APP_DOMAIN=<name>
   APP_BIND=127.0.0.1
   FORWARDED_ALLOW_IPS=*
   APP_ADMIN_API_KEY=<output of: python3 -c "import secrets; print(secrets.token_urlsafe(32))">
   ```

4. `docker compose up -d --build`, then `docker compose logs caddy` should show
   `certificate obtained successfully` within a minute.

`.env` is gitignored; keep the admin key there and nowhere else. The certificate is
in the `caddy-data` volume and renews itself.

On TEAM02 (Nov 21) the app is at https://team02.cis240466.projects.jetstream-cloud.org:

- Exosphere's default security group already let 80 and 443 through: before Caddy
  ran, both answered "connection refused" at once instead of timing out. No rule
  was added.
- Caddy had a Let's Encrypt certificate within a minute of `up`.
- After a shelve and unshelve that changed the IP, the site came back over HTTPS by
  itself about a minute after boot, with nobody logged in.

## Check it from your laptop

```bash
HOST=https://<name>
curl $HOST/health
curl $HOST/ready
curl -X POST $HOST/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What does Form I-983 relate to?", "visa_type": "F-1"}'
curl -X POST $HOST/admin/generate \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: <key from .env>" \
  -d '{"prompt": "What is OPT?"}'
```

Plain http redirects, and nothing else answers from outside:

```bash
curl -I http://<name>              # 308 Permanent Redirect to https
curl -m 5 http://<public-ip>:8000/  # refused or times out
curl -m 5 http://<public-ip>:6333/  # refused or times out (Qdrant)
```

Without a key, `/admin/generate` answers 404 when `APP_ADMIN_API_KEY` is unset and
401 when it is set. The 11th `/ask` within a minute from one address answers 429.

## Time limits and retries

Every wait on another program has a limit, and each layer gives up after the one
behind it, so the user sees the inner layer's clear answer instead of a hung page:

| Layer | Gives up after | Then the user sees |
| --- | --- | --- |
| A question waiting for an answer slot | 60 s (`GENERATION_QUEUE_TIMEOUT_S`) | the passages, `status: "model_unavailable"` |
| The model call, retries included | 120 s (`LLM_TIMEOUT_S`) | the passages, `status: "model_unavailable"` |
| Caddy waiting for the app | 200 s (`response_header_timeout` in `Caddyfile`) | "The answer took too long" (504) |
| The page waiting for Caddy | 210 s (`ASK_LIMIT_MS` in `app.js`) | "The answer took too long" |

The app's worst case is about 185 s: a full queue wait, then a model call that runs
out its budget. Caddy's 200 s sits above that, so Caddy only answers 504 when the app
itself is stuck. A stopped app gets a 502 at once, which the page shows as "The
service is restarting".

Retries happen in one place, the model client, so they don't multiply (page × app ×
client). It retries only when nothing was generated: the connection was refused or
never opened, or llama.cpp answered 429, 502 or 503 (still loading, or full). It waits with exponential
backoff and full jitter, so many requests failing together don't come back together.
It honors `Retry-After`, and gives up at once if the server asks for more than
`LLM_RETRY_MAX_WAIT_S`. A read timeout is never retried: the model may still be
working, and a second request would double its load. Ingestion's `PoliteClient`,
which retries federal sites on 429 and 5xx, now jitters its backoff too, but never
below half of it, to stay polite.

`LLM_MAX_RETRIES` is 3 because a llama.cpp restart took 2.8 s until it served again.
With a question sent 0.5 s into a restart, 2 retries answered 3 of 8 times and 3
retries 7 of 8; the rest got the passages.

Measured Nov 6 through Caddy on a 2-CPU, 7 GB machine (TEAM02 has 2 vCPUs, 6 GB), with
Qwen2.5-1.5B Q4_K_M, the real 8 CFR 214 index, and the page in Chrome:

| What happened | What the user got |
| --- | --- |
| One question, model just restarted | answer in 18.7 s |
| 3 questions at once (2 slots) | all answered, in 22, 29 and 37 s |
| 6 questions at once (5 need the model) | all answered within 64 s; the queue never hit 60 s |
| Queue limit set to 5 s, 3 at once | 2 answered; the third got its passages at 5.2 s |
| llm stopped | passages in 1.4 s (2 retries) and 5.3 s on the page (3 retries) |
| llm frozen | passages at 120.1 s |
| App stopped, page already open | "The service is restarting" at once (Caddy 502 in 0.01 s) |
| App frozen | "The answer took too long" at 200.1 s (Caddy 504 at 200.0 s) |
| 11th question in a minute | "Too many requests: at most 10 a minute. Try again in 60 seconds." |

`scripts/smoke_rag.py` passed on the healthy stack (13.4 s) and failed with llm
stopped, so an evidence-only answer can't pass for a working one.

## Release and deploy

TEAM02 runs released images from the GitHub Container Registry, not code built on
the instance. A release is a version in `pyproject.toml`, a Git tag, and the image
CI publishes under that tag.

### Cut a release (on your laptop)

1. In a pull request, set the new version in `pyproject.toml` (`0.2.0` → `0.3.0`,
   following [semantic versioning](https://semver.org)), run `uv lock`, and add a
   section to `CHANGELOG.md` saying what changed for users. Merge it.
2. Tag the merge commit on `main` and push the tag:

   ```bash
   git switch main && git pull
   git tag v0.3.0
   git push origin v0.3.0
   ```

3. Wait for the CI run on the tag (Actions tab). It fails if the tag doesn't match
   `pyproject.toml`, and its summary shows the digest it published.

### Deploy it, or roll back (on TEAM02)

The same command does both: give it the version to run.

```bash
cd ~/Assignment_for_6700
scripts/deploy.sh v0.3.0   # deploy
scripts/deploy.sh v0.2.0   # roll back to the release before
```

It checks out the tag (so `compose.yml` and the `Caddyfile` match the image), pulls the
image, pins its digest in `.env` as `APP_IMAGE`, runs pending catalog migrations
(each backs the catalog up first), starts the stack, waits for `/ready`, checks that
`/health` reports the version, and runs the evaluation set (about 10 minutes;
`SKIP_EVAL=1` skips it). When it worked, the last line says
`deployed v0.3.0: eval 19/25`, and `deploy.log` gets a line like:

```text
2026-11-24T20:15:02Z v0.3.0 ghcr.io/rakupper/assignment_for_6700@sha256:eb14b8b1... ok eval 19/25
```

When a check fails, it writes a `failed` line with the reason, prints the app's last
log lines, and tells you which version to roll back to. The checkout stays on the
tag (a detached HEAD), which is expected: deploy with the script, not `git pull`.

`APP_IMAGE` in `.env` is what makes a release stick: restarts, reboots and a plain
`docker compose up -d` all use that digest. Without it, Compose builds the image from
the checkout. That is how you try an unreleased branch: delete the `APP_IMAGE` line
from `.env`, then `git switch <branch> && docker compose up -d --build`. Run
`scripts/deploy.sh` again afterwards to get back to a release.

## Everyday commands (on the instance, in `~/Assignment_for_6700`)

| Task | Command |
| --- | --- |
| Status and health | `docker compose ps` |
| Follow the logs | `docker compose logs -f app` (or `llm`) |
| Deploy a release, or roll back to an older one | `scripts/deploy.sh v0.2.0` |
| Which releases ran, and how they did | `cat deploy.log` |
| Try an unreleased branch | `git switch <branch> && docker compose up -d --build`, after removing `APP_IMAGE` from `.env` |
| Stop the stack (keeps the model volume) | `docker compose down` |
| Disk usage | `docker system df`, `df -h /` and `df -h /media/volume/team02-data` |
| Qdrant collections and point counts | `docker compose run --rm jobs python -m myapp.vectordb.collection status` |

Shelve TEAM02 in Exosphere when you are done, and say so in the group chat first so
you don't cut off someone who is testing. Both containers use
`restart: unless-stopped`, so they come back on their own after an unshelve.

## Configuration

Compose passes config to the app at run time; nothing is baked into the image.

- Defaults come from `.env.production.example` (no secrets, committed).
- Put overrides in an untracked `.env` next to `compose.yml`; its values win.
- `LLM_MAX_TOKENS` defaults to 128. On 2 vCPUs the model generates about 25 tokens/s,
  so long answers under concurrent load would exceed the 120 s client timeout.

To use the class model server (Qwen 14B on a GPU, only up when the instructors run it)
instead of the local llm container, put this in `.env` and run `docker compose up -d`:

```bash
LLM_BASE_URL=http://10.1.82.181:8000/v1
LLM_MODEL=Qwen/Qwen2.5-14B-Instruct-AWQ
```

## When something breaks

A tester writes "it's been down for an hour". Check in this order, on the instance:

1. `docker compose ps`. `Restarting (1)` is a crash loop; `unhealthy` means the
   health check fails; a missing service means it was stopped (`unless-stopped` keeps a
   stopped container down, even after a reboot).
2. `docker compose logs --tail 50 app` (or `llm`, `qdrant`, `caddy`). A crash loop
   usually ends in a settings `ValidationError` that names the variable. Restarting
   won't fix a bad setting; fix `.env` and `docker compose up -d`.
3. `curl -s localhost:8000/ready`. It names the missing dependency: the model, the
   Qdrant alias, or an empty index.
4. `df -h / /media/volume/team02-data` and `free -m`. A full disk or memory stops
   Docker, Qdrant or llama.cpp before any log says why.
5. From your laptop, `curl -I https://<name>/health`. A 502 means Caddy is up and
   the app isn't; no answer at all means Caddy, the instance or the IP is down (the IP
   can change on unshelve).

| What users report | Most likely | Check |
| --- | --- | --- |
| "The answer took too long" | the model is slow or stuck | `docker compose logs llm`, `docker stats` |
| "No written answer right now" on every question | llm is down or unhealthy | `docker compose ps llm`; the passages still work |
| "Too many requests" for everyone | everyone shares Caddy's address | `FORWARDED_ALLOW_IPS=*` in `.env` |
| "The service is restarting" for minutes | app crash loop | steps 1 and 2 |
| Answers cite old rules | the alias points at an old collection | `python -m myapp.vectordb.collection status` via jobs |

After anything users noticed, write a short postmortem from
[postmortems/TEMPLATE.md](postmortems/TEMPLATE.md) as
`docs/postmortems/<date>-<what>.md`, and link it in the group chat.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| `ssh` times out | TEAM02 is shelved, or it is still booting. Check Exosphere, and connect with the hostname rather than an IP. |
| `ssh` says the host key changed | You used an old IP that now belongs to another machine (this happened on Nov 20). Don't override the warning; connect with the hostname. |
| `/admin/generate` or `/ready` returns 503 | The llm container is not ready. Check `docker compose ps` and `docker compose logs llm`. |
| `app` keeps restarting | Usually a config error at startup. `docker compose logs app` shows which setting failed validation, e.g. `LLM_PROVIDER=echo` in production. |
| Build fails with "no space left on device" | Check `df -h /`; remove old images with `docker image prune`. Do not delete the `llm-models` volume unless you want to download the model again. |
| Docker does not start after an unshelve | The volume is not attached or not mounted, so the `RequiresMountsFor` rules hold Docker and containerd back. Check **Volumes** in Exosphere, then `sudo mount -a && sudo systemctl start containerd docker`. If the volume was reattached under a new device name (`lsblk`), mask `automount-volume@<name>.service` too. |
| `https://<name>` doesn't load, `caddy` logs certificate errors | Port 80 or 443 is closed in the security group, or `APP_DOMAIN` doesn't resolve to the current public IP (it can change on unshelve). |
| Everyone gets 429 at once | `FORWARDED_ALLOW_IPS` isn't `*`, so the app sees every request as coming from Caddy. |
| `app` never starts, `qdrant` is unhealthy | `docker compose logs qdrant`. Usually `team02-data` is not mounted, so Docker's storage is missing; see the unshelve row above. |
| `PermissionError` under `/data` or `/embed-cache` | The volume was filled by a root container, for example a manual copy. Fix it once: `docker compose run --rm --user 0 jobs chown -R 10001:10001 /data /embed-cache`. |

## Measured on TEAM02

### Final main (Oct 21)

- Fresh clone + `docker compose up -d --build`: both containers healthy after 37 s
  (this includes installing scikit-learn into the image).
- Calls from a laptop:

  | Request | Result |
  | --- | --- |
  | `GET /health` | 200, `{"status":"ok","version":"0.1.0"}`, 0.06 s |
  | `GET /ready` | 200, `{"status":"ready"}` |
  | `POST /predict` (a 0 from the digits dataset) | 200, `{"digit":0}`, 1.08 s (1 s is the audit delay) |
  | `POST /predict` with 3 pixels | 422 |
  | `POST /generate` | 200, correct one-sentence answer about OPT, 1.7 s |
  | `POST /generate` with a blank prompt | 422 |
  | port 8080 (model server) | not reachable from outside |

- Shelve/unshelve checked again: the containers came back by themselves and `/health`
  worked from outside right away. The IP stayed the same since we keep it when
  shelving.
- App image 472 MB. Rebuild after a one-line change: 13.1 s cached, 15.6 s without
  cache (see [docker.md](docker.md#build-measurements)).
- `docker system df` (stack running, after `docker builder prune`):

  ```text
  TYPE            TOTAL     ACTIVE    SIZE      RECLAIMABLE
  Images          5         5         2.944GB   2.944GB (100%)
  Containers      5         4         27.12MB   12.29kB (0%)
  Local Volumes   1         1         1.117GB   0B (0%)
  Build Cache     0         0         0B        0B
  ```

  3.4 GB of disk left.

### Before scikit-learn (Oct 20)

- llm container: healthy 6 s after start with the weights already in the volume, and
  again 8 s after a Docker restart.
- Generation: about 25 tokens/s for one request; 10 concurrent requests of up to 64
  tokens finish in 23 s (slowest 22.9 s).
- Full stack from a fresh clone: `docker compose up -d --build` builds the app image
  and has both services healthy in 14 s. Starting again from an empty `llm-models`
  volume (model download and load included) takes 13 s to a healthy app.
- From a laptop: `/health` returns 200 in 0.06 s; the model port 8080 is not reachable
  from outside.
- Shelve and unshelve: both containers came back on their own at boot
  (`restart: unless-stopped`), and `/health` answered from outside without anyone
  logging in.
- App image: 208 MB. With the stack up, 4.4 GB of the root disk stays free.
- `docker system df` with the stack up:

  ```text
  TYPE            TOTAL     ACTIVE    SIZE      RECLAIMABLE
  Images          5         5         2.82GB    2.82GB (100%)
  Containers      5         4         27.11MB   12.29kB (0%)
  Local Volumes   1         1         1.117GB   0B (0%)
  Build Cache     19        0         359.4MB   151.5MB
  ```

  Three of the five images are Exosphere's own web shell (Guacamole) containers.
