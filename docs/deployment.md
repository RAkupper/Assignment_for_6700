# Deployment on Jetstream2

Notes on running the stack on our team instance (TEAM02). The stack is defined in
[`compose.yml`](../compose.yml).

## What runs where

```text
TEAM02 (Jetstream2 m3.small: 2 vCPUs, 6 GB RAM, 20 GB disk)
├── app   container, port 8000 (public)
│     ├── GET  /health    the app process is up
│     ├── GET  /ready     the LLM server answers
│     ├── POST /predict   digits classifier (scikit-learn), loaded inside the app
│     └── POST /generate  asks the llm container over the compose network
└── llm   container, port 8080 (internal only)
      └── llama.cpp server with Qwen2.5-1.5B-Instruct (Q4_K_M)
          weights in the named volume llm-models
```

- The digits model (`models/digits.joblib`, 91 KB) is inside the app image, so
  `/predict` still works if the LLM is down.
- The LLM has its own container. If it's down, `/generate` and `/ready` give 503 but
  `/health` stays 200.
- We used llama.cpp instead of Ollama because the Ollama image (3.78 GB) didn't fit
  on the 20 GB disk next to the model. The llama.cpp image is 309 MB.

## Start the stack

1. In [Exosphere](https://jetstream2.exosphere.app), open TEAM02. If it's shelved, do
   **Actions → Unshelve** and wait for **Ready**.
2. Get the public IP from the instance page. We keep the IP when shelving, so it should
   be `149.165.150.147`.
3. SSH in (your key has to be in the instance's `authorized_keys`):

   ```bash
   ssh exouser@<public-ip>
   ```

4. On the instance:

   ```bash
   git clone https://github.com/RAkupper/Assignment_for_6700.git
   cd Assignment_for_6700
   docker compose up -d
   docker compose ps
   ```

The first start downloads the model (1.1 GB) into the `llm-models` volume. After that
it's reused. `app` waits until `llm` is healthy.

## Check it from your laptop

```bash
curl http://<public-ip>:8000/health
curl http://<public-ip>:8000/ready
curl -X POST http://<public-ip>:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is OPT?"}'
curl -X POST http://<public-ip>:8000/predict \
  -H "Content-Type: application/json" \
  -d '{"pixels":[0,0,7,15,13,1,0,0,0,8,13,6,15,4,0,0,0,2,1,13,13,0,0,0,0,0,2,15,11,1,0,0,0,0,0,1,12,12,1,0,0,0,0,0,1,10,8,0,0,0,8,4,5,14,9,0,0,0,7,13,13,9,0,0]}'
```

The `/predict` example is a 3 from the scikit-learn digits dataset, so it should
return `{"digit":3}`.

## Everyday commands (on the instance, in `~/Assignment_for_6700`)

| Task | Command |
| --- | --- |
| Status | `docker compose ps` |
| Logs | `docker compose logs -f app` (or `llm`) |
| Pull new code and rebuild | `git pull && docker compose up -d --build` |
| Stop (keeps the model) | `docker compose down` |
| Disk usage | `docker system df` and `df -h /` |

Shelve TEAM02 in Exosphere when you're done, but say so in the group chat first in
case someone is testing. Both containers have `restart: unless-stopped`, so they start
again by themselves after an unshelve.

## Configuration

Config is passed in when the containers start, nothing is baked into the image.

- Defaults are in `.env.production.example` (committed, no secrets).
- Put changes in a `.env` next to `compose.yml`, it overrides the example.
- `LLM_MAX_TOKENS` is 128. The model does about 25 tokens/s on 2 vCPUs, so longer
  answers with several requests at once would hit the 120 s timeout.

To use the class model server instead (Qwen 14B on a GPU, only up when the instructors
run it), put this in `.env` and run `docker compose up -d`:

```bash
LLM_BASE_URL=http://10.1.82.181:8000/v1
LLM_MODEL=Qwen/Qwen2.5-14B-Instruct-AWQ
```

## Troubleshooting

| Problem | What to do |
| --- | --- |
| `ssh` times out | TEAM02 is probably shelved. Check Exosphere. |
| `ssh` says the host key changed | You're using an old IP that now belongs to another machine. Get the current IP from Exosphere. |
| `/generate` or `/ready` gives 503 | The llm container isn't ready. Check `docker compose ps` and `docker compose logs llm`. |
| `app` keeps restarting | Probably a config error. `docker compose logs app` shows which setting failed, e.g. a missing `models/digits.joblib`. |
| Build fails with "no space left on device" | Check `df -h /` and remove old images with `docker image prune`. Don't delete the `llm-models` volume unless you want to download the model again. |

## Measured on TEAM02

From the final `main` on TEAM02 (m3.small, 2 vCPUs, 6 GB RAM, 20 GB disk):

- Fresh clone + `docker compose up -d --build`: both containers healthy after 37 s
  (14 s before scikit-learn was added).
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

- Shelve/unshelve (Oct 20 and Oct 21): the containers came back by themselves and
  `/health` worked from outside right away. The IP stayed the same since we keep it
  when shelving.
- llm container: healthy in 6 s when the model is already downloaded, 8 s after a
  Docker restart.
- Speed: ~25 tokens/s for one request. 10 requests at once (64 tokens each) took 23 s,
  slowest 22.9 s.
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

  3 of the 5 images are Exosphere's web shell. 3.4 GB of disk left.
