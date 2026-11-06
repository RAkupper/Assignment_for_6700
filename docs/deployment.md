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
2. Copy the **public IP** from the instance page. It can change on unshelve while
   Jetstream2 is short of floating IPs (it moved to `149.165.153.173` on Oct 28),
   so check it each time. SSH then says the host key is known under the old IP;
   answer `yes`.
3. From your laptop (your SSH key must be in the instance's `authorized_keys`):

   ```bash
   ssh exouser@<public-ip>
   ```

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

Once it passes, remove `DATA_VOLUME_DIR` from `.env`; Compose no longer reads it.
Keep the old folders until a backup has been taken and the
[restore drill](#restore-drill) passes, then delete them:

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
  for a slot, then gets 503 "busy". `/retrieve` and questions the index cannot
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

After every load that changes the data, take a backup: `scripts/backup.sh`.
A full load measured Nov 6 took 19.5 min on one CPU, with `jobs` peaking at 2.3 GB
of memory, so run it when nobody is testing.

## Backups off Jetstream2

A backup is a `data-<UTC time>` release in this repo, so it outlives TEAM02, the
Jetstream2 allocation and anyone's laptop. The repo is public and so is the data
(federal regulation text). `.env` and keys are never included. Each release holds:

| Asset | What |
| --- | --- |
| `catalog.sqlite` | the catalog, copied with SQLite's backup API (safe while in use) |
| `data-files.tar.gz` | everything else in `/data`: raw downloads, manual captures |
| `qdrant.snapshot` | a Qdrant snapshot of the collection the `provisions` alias points to |
| `manifest.json` | sha256 and size of each file, the collection, point count, embedding model, catalog schema and row count |

Measured Nov 6 on a copy of TEAM02's data (3,882 points): 43 MB per backup
(`catalog.sqlite` 4.1 MB, `data-files.tar.gz` 13.6 MB, `qdrant.snapshot` 25.4 MB),
made in 5 s. The newest 5 are kept; older releases and their tags are deleted after
each upload.

### Token (once)

Uploading needs a fine-grained token. Fine-grained tokens only reach repos owned by
the account that makes them, so the repo owner (RAkupper) creates it:
**Settings → Developer settings → Fine-grained tokens → Generate new token**, resource
owner RAkupper, **Only select repositories** → `Assignment_for_6700`, permission
**Contents: Read and write**, expiry after the semester. On TEAM02, put it in its own
file, which git and Docker builds ignore like `.env`:

```bash
echo "BACKUP_GITHUB_TOKEN=<token>" > .env.backup && chmod 600 .env.backup
```

Only the `jobs` container reads `.env.backup`. The app, which faces the internet, never
holds the token. Restoring and listing need no token.

### Take one

```bash
scripts/backup.sh
docker compose run --rm jobs python -m myapp.backup list
```

It creates a draft release, uploads the four files, and only then publishes it, so a
failed upload never looks like a backup. A systemd timer also runs it every week;
`Persistent=true` catches up after a shelve. Install it once, on the instance:

`/etc/systemd/system/team02-backup.service`:

```ini
[Unit]
Description=Back up the catalog and Qdrant to a GitHub data release
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
User=exouser
ExecStart=/home/exouser/Assignment_for_6700/scripts/backup.sh
```

`/etc/systemd/system/team02-backup.timer`:

```ini
[Timer]
OnCalendar=Sun *-*-* 06:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now team02-backup.timer
systemctl list-timers team02-backup.timer   # next run
journalctl -u team02-backup.service -n 20   # last run's output
```

### Restore

```bash
scripts/restore.sh                          # the newest backup
scripts/restore.sh data-20261106T060000Z    # a specific one
scripts/restore.sh latest --no-activate     # load it, leave the alias alone
```

Nothing is replaced until every check passes:

- every file matches its sha256 in the manifest;
- the catalog passes `PRAGMA integrity_check` and has the manifest's schema and row count;
- the snapshot loads into a new `provisions_v<N>` with the manifest's point count.

Only then is the current catalog saved to `/data/backups/`, the backup's catalog and
files put in place, and the alias moved to the new collection. If any step fails, the
new collection is dropped and the old catalog put back. Existing collections are never
overwritten, so rolling back is one `activate` command.

### Restore drill

Practice on empty volumes without touching the live stack. `-p drill` is a separate
Compose project with its own volumes:

```bash
docker compose -p drill up -d --wait qdrant
docker compose -p drill run --rm jobs python -m myapp.backup restore latest
docker compose -p drill run --rm jobs python -m myapp.vectordb.collection status
docker compose -p drill down -v
```

The status should show the manifest's collection and point count as live. The same
steps without `-p drill`, followed by `docker compose up -d`, rebuild the stack on a
new instance or a laptop.

Measured Nov 6 with that 43 MB backup: the restore onto empty volumes took 4 s and
gave back all 3,882 points, the alias, the catalog and the raw files, and a search
returned the same ranking as the original. Rebuilding the same data from eCFR took
23 minutes: 170 s to fetch 64 snapshots, 12 s to parse, and 19.5 min to load on one CPU.


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

## Everyday commands (on the instance, in `~/Assignment_for_6700`)

| Task | Command |
| --- | --- |
| Status and health | `docker compose ps` |
| Follow the logs | `docker compose logs -f app` (or `llm`) |
| Pull new code and rebuild the app | `git pull && docker compose up -d --build` |
| Stop the stack (keeps the model volume) | `docker compose down` |
| Disk usage | `docker system df`, `df -h /` and `df -h /media/volume/team02-data` |
| Qdrant collections and point counts | `docker compose run --rm jobs python -m myapp.vectordb.collection status` |
| Back up now, list backups | `scripts/backup.sh`, `docker compose run --rm jobs python -m myapp.backup list` |

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

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| `ssh` times out | TEAM02 is shelved, or the IP changed after an unshelve. Check Exosphere. |
| `ssh` says the host key changed | You are using an old IP that now belongs to another machine. Get the current IP from Exosphere. |
| `/admin/generate` or `/ready` returns 503 | The llm container is not ready. Check `docker compose ps` and `docker compose logs llm`. |
| `app` keeps restarting | Usually a config error at startup. `docker compose logs app` shows which setting failed validation, e.g. `LLM_PROVIDER=echo` in production. |
| Build fails with "no space left on device" | Check `df -h /`; remove old images with `docker image prune`. Do not delete the `llm-models` volume unless you want to download the model again. |
| Docker does not start after an unshelve | The volume is not attached or not mounted, so the `RequiresMountsFor` rules hold Docker and containerd back. Check **Volumes** in Exosphere, then `sudo mount -a && sudo systemctl start containerd docker`. If the volume was reattached under a new device name (`lsblk`), mask `automount-volume@<name>.service` too. |
| `https://<name>` doesn't load, `caddy` logs certificate errors | Port 80 or 443 is closed in the security group, or `APP_DOMAIN` doesn't resolve to the current public IP (it can change on unshelve). |
| Everyone gets 429 at once | `FORWARDED_ALLOW_IPS` isn't `*`, so the app sees every request as coming from Caddy. |
| `app` never starts, `qdrant` is unhealthy | `docker compose logs qdrant`. Usually `team02-data` is not mounted, so Docker's storage is missing; see the unshelve row above. |
| `scripts/backup.sh` says GitHub answered 401 or 403 | `BACKUP_GITHUB_TOKEN` in `.env.backup` is missing, expired, or lacks Contents write on this repo. Make a new one as in [Token](#token-once). |
| `restore` says a file does not match the manifest | The download is damaged or the release was edited. Nothing was changed; try the previous backup with `scripts/restore.sh <older tag>`. |
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
