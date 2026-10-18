# Deployment on Jetstream2

How the service runs on our team instance, how to start and check it, and what to
do when something breaks. The stack is defined in [`compose.yml`](../compose.yml).

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
2. Copy the **public IP** from the instance page. It changes after every unshelve.
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

The `/predict` example is a handwritten 3 from scikit-learn's digits dataset, so
the answer should be `{"digit":3}`.

## Everyday commands (on the instance, in `~/Assignment_for_6700`)

| Task | Command |
| --- | --- |
| Status and health | `docker compose ps` |
| Follow the logs | `docker compose logs -f app` (or `llm`) |
| Pull new code and rebuild the app | `git pull && docker compose up -d --build` |
| Stop the stack (keeps the model volume) | `docker compose down` |
| Disk usage | `docker system df` and `df -h /` |

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

## Measured on TEAM02

- llm container: healthy 6 s after start with the weights already in the volume, and
  again 8 s after a Docker restart.
- Generation: about 25 tokens/s for one request; 10 concurrent requests of up to 64
  tokens finish in 23 s (slowest 22.9 s).
- Disk: the llama.cpp image takes about 1.1 GB and the model 1.1 GB; 4.7 GB stays free
  before the app image.
