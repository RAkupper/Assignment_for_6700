# Deployment on Jetstream2

How the service runs on our team instance, how to start and check it, and what to
do when something breaks. The stack is defined in [`compose.yml`](../compose.yml).

## What runs where

```text
TEAM02 (Jetstream2 m3.small: 2 vCPUs, 6 GB RAM, 20 GB root disk + 40 GB volume)
├── app   container, port 8000 (public)
│     ├── GET  /health    the app process is up
│     ├── GET  /ready     configured LLM and enabled retrieval dependencies are ready
│     ├── POST /predict   digits classifier (scikit-learn), loaded inside the app
│     ├── POST /generate  asks the llm container over the compose network
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

- The digits model is a 91 KB file (`models/digits.joblib`) that ships inside the
  app image, so `/predict` works even when the LLM is down.
- The LLM runs in its own container so the app image stays small and the model can
  be swapped through env vars. `/generate` returns 503 and `/ready` returns 503
  while it is unavailable; `/health` stays 200.
- We use llama.cpp rather than Ollama because the Ollama image (3.78 GB) does not
  fit next to a model on the 20 GB disk. The llama.cpp image is 309 MB.

## Start the stack

1. In [Exosphere](https://jetstream2.exosphere.app), open TEAM02. If it is shelved,
   choose **Actions → Unshelve** and wait for **Ready**.
2. Copy the **public IP** from the instance page. We keep it when shelving, so it
   stays `149.165.173.215`; if it ever differs, use the one shown there.
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
40 GB volume `team02-data`: Docker's images and volumes, ingestion data, Qdrant
storage and the embedding models. Done on TEAM02 on Oct 28; repeat only for a new
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

4. Make the app's folders and point Compose at the volume. The app runs as UID
   10001, so it owns the two it writes to:

   ```bash
   sudo mkdir -p $V/data $V/embed-cache $V/qdrant $V/qdrant-snapshots
   sudo chown -R 10001:10001 $V/data $V/embed-cache
   echo "DATA_VOLUME_DIR=$V" >> .env
   docker compose config | grep -c "$V"   # nonzero, or .env is not being read
   docker compose up -d
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
volume stops the stack instead of filling the root disk. Without `DATA_VOLUME_DIR`,
Compose uses `./volume` in the clone. That is fine on a laptop but puts data on the
root disk on TEAM02.

| On the volume | Mounted at | Used by |
| --- | --- | --- |
| `containerd/` | `/var/lib/containerd` | image layers (~3.6 GB) |
| `docker/` | `/var/lib/docker` | containers, `llm-models` (~1.1 GB) |
| `data/` | `/data` | jobs: raw downloads, manual captures, `catalog.sqlite` |
| `embed-cache/` | `/embed-cache` | app and jobs: BGE and BM25 models (~70 MB) |
| `qdrant/` | `/qdrant/storage` | qdrant |
| `qdrant-snapshots/` | `/qdrant/snapshots` | qdrant snapshots |

Measured Oct 28: root disk went from 2.8 GB to 7.5 GB free; the volume holds 4.3 GB.

## Resource limits

The m3.small has 2 vCPUs and 6 GB RAM, shared by every container.

- `llm` is capped at 3 GB (`LLM_MEM_LIMIT`). The model needs about 1.3 GB, so the
  cap only matters if something goes wrong, and then only `llm` is killed.
- A `jobs` run gets one CPU (`JOBS_CPUS`), so `llm` keeps the other while a load
  embeds. A full load of 8 CFR 214 then takes about twice as long as on two cores.
  Run loads when nobody is testing, and say so in the group chat.

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

## Check it from your laptop

```bash
curl http://<public-ip>:8000/health
curl http://<public-ip>:8000/ready
curl -X POST http://<public-ip>:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is OPT?"}'
curl -X POST http://<public-ip>:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What does Form I-983 relate to?", "visa_type": "F-1"}'
curl -X POST http://<public-ip>:8000/predict \
  -H "Content-Type: application/json" \
  -d '{"pixels":[0,0,7,15,13,1,0,0,0,8,13,6,15,4,0,0,0,2,1,13,13,0,0,0,0,0,2,15,11,1,0,0,0,0,0,1,12,12,1,0,0,0,0,0,1,10,8,0,0,0,8,4,5,14,9,0,0,0,7,13,13,9,0,0]}'
```

The `/predict` example is a handwritten 3 from scikit-learn's digits dataset, so
the answer should be `{"digit":3}`.

Qdrant must not answer from outside. This should time out or be refused:

```bash
curl -m 5 http://<public-ip>:6333/
```

## Everyday commands (on the instance, in `~/Assignment_for_6700`)

| Task | Command |
| --- | --- |
| Status and health | `docker compose ps` |
| Follow the logs | `docker compose logs -f app` (or `llm`) |
| Pull new code and rebuild the app | `git pull && docker compose up -d --build` |
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

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| `ssh` times out | TEAM02 is shelved, or the IP changed after an unshelve. Check Exosphere. |
| `ssh` says the host key changed | You are using an old IP that now belongs to another machine. Get the current IP from Exosphere. |
| `/generate` or `/ready` returns 503 | The llm container is not ready. Check `docker compose ps` and `docker compose logs llm`. |
| `app` keeps restarting | Usually a config error at startup. `docker compose logs app` shows which setting failed validation, e.g. a missing `models/digits.joblib`. |
| Build fails with "no space left on device" | Check `df -h /`; remove old images with `docker image prune`. Do not delete the `llm-models` volume unless you want to download the model again. |
| Docker does not start after an unshelve | The volume is not attached or not mounted, so the `RequiresMountsFor` rules hold Docker and containerd back. Check **Volumes** in Exosphere, then `sudo mount -a && sudo systemctl start containerd docker`. If the volume was reattached under a new device name (`lsblk`), mask `automount-volume@<name>.service` too. |
| `app` never starts, `qdrant` is unhealthy | `docker compose logs qdrant`. Usually the volume is not mounted or `DATA_VOLUME_DIR` in `.env` is wrong. |
| `PermissionError` under `/data` or `/embed-cache` | The folders on the volume belong to root. Run the `chown` from [Data volume](#data-volume-first-time-only). |

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
