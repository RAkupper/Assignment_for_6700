# Runbook

The exact commands for each routine task on TEAM02, and what you should see when it
worked. The outputs below are copied from real runs. Background, settings and limits
are in [deployment.md](deployment.md); when something looks wrong, see its
[Troubleshooting](deployment.md#troubleshooting) table.

| Task | When |
| --- | --- |
| [Get in](#get-in) | Before any task on the instance |
| [Check it's healthy](#check-its-healthy) | Any time; after every task below |
| [Deploy a release](#deploy-a-release) | A new version is tagged |
| [Roll back](#roll-back) | A release misbehaves |
| [Reboot](#reboot) | After system updates, or to test recovery |
| [Shelve and unshelve](#shelve-and-unshelve) | When nobody needs the app, and before using it again |
| [Back up and restore](#back-up-and-restore) | After loading new data, and to prove a backup works |
| [Fresh install](#fresh-install) | A new instance, or rebuilding TEAM02 from nothing |

## Get in

1. In [Exosphere](https://jetstream2.exosphere.app), open TEAM02. If it says
   **Shelved**, choose **Actions → Unshelve** and wait for **Ready** (about a minute).
2. From your laptop, with your SSH key in the instance's `authorized_keys`:

   ```bash
   ssh exouser@team02.cis240466.projects.jetstream-cloud.org
   ```

3. On the instance, every command below runs from the repo:

   ```bash
   cd ~/Assignment_for_6700
   ```

Always use the hostname: the IP changes on unshelve, and the hostname follows it. The
first time, SSH asks you to trust the host; the fingerprint must be
`SHA256:P3MOMLulOLoEUUm1IJ8DnZTVCjap2jvZdEr9cztdOHA`. If SSH ever says the host key
*changed*, stop: you are talking to another machine.

Right after an unshelve, SSH on a Mac can say `Could not resolve hostname`: it asked
while the instance had no IP yet and cached the "no such name" answer. `dig +short
team02.cis240466.projects.jetstream-cloud.org` shows the real IP. Clear the cache
(it asks for your Mac's password), then connect again:

```bash
sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder
```

## Check it's healthy

From your laptop:

```bash
curl https://team02.cis240466.projects.jetstream-cloud.org/health
curl https://team02.cis240466.projects.jetstream-cloud.org/ready
```

You should see:

```text
{"status":"ok","version":"0.2.0"}
{"status":"ready"}
```

`version` is the release that's running. `/ready` answers 503 until the model and the
search index are loaded, which takes under a minute after a start.

On the instance:

```bash
docker compose ps --format '{{.Service}} {{.Status}}'
tail -3 deploy.log
```

```text
app Up 31 minutes (healthy)
caddy Up 41 minutes
llm Up 41 minutes (healthy)
qdrant Up 41 minutes (healthy)
2026-11-24T14:09:14Z v0.2.0 ghcr.io/rakupper/assignment_for_6700@sha256:77bd6507c0ff2fbd86f1549afa103790e08377a0b68c3c46c74bac1ee6d86a49 ok eval 19/25
```

To check the answers too, not just the status codes, run the evaluation set (about
10 minutes; [evaluation.md](evaluation.md)):

```bash
docker compose run --rm jobs python -m myapp.evaluation --base-url http://app:8000
```

It should end with `| All cases passed | 19/25 (76%) |` or better.

## Deploy a release

A release is a version in `pyproject.toml`, a Git tag and the image CI publishes
under that tag. TEAM02 only runs released images.

**On your laptop**, after the pull request that sets the new version in
`pyproject.toml` and `CHANGELOG.md` is merged:

```bash
git switch main && git pull
git tag v0.3.0
git push origin v0.3.0
```

Wait for the CI run on the tag (GitHub → Actions) to finish green. Its summary shows
the published digest. It fails on purpose if the tag doesn't match `pyproject.toml`.

**On TEAM02:**

```bash
scripts/deploy.sh v0.3.0
```

(The very first time on an instance that doesn't have the script yet, run
`git pull` first.) What you should see, from the first deploy of v0.2.0 on Nov 24:

```text
deploying v0.2.0 (last good: none)
image ghcr.io/rakupper/assignment_for_6700@sha256:77bd6507c0ff2fbd86f1549afa103790e08377a0b68c3c46c74bac1ee6d86a49
schema is at 2: nothing to do
 ✔ Container assignment_for_6700-qdrant-1  Healthy
 ✔ Container assignment_for_6700-llm-1     Healthy
 ✔ Container assignment_for_6700-app-1     Healthy
 ✔ Container assignment_for_6700-caddy-1   Healthy
running the evaluation set (about 10 minutes)
[1/25] pass oncampus-hours (26.1s)
...
| All cases passed | 19/25 (76%) |
...
deployed v0.2.0: eval 19/25
```

Then [check it's healthy](#check-its-healthy): `/health` reports the new version.

The script checks out the tag, pulls the image, pins its digest in `.env` as
`APP_IMAGE`, runs pending catalog migrations (each backs the catalog up first),
restarts the stack on the image, waits for `/ready`, checks the version on
`/health`, runs the evaluation set and appends a line to `deploy.log`. Afterwards the
checkout is a detached HEAD at the tag; that is expected.

**If it fails**, it prints `FAILED: <reason>`, the app's last log lines and
`roll back with: scripts/deploy.sh <last good version>`, and writes a `failed` line to
`deploy.log`. Do what it says, then look into the reason. In an emergency,
`SKIP_EVAL=1 scripts/deploy.sh <version>` skips the 10-minute evaluation.

## Roll back

The same command as a deploy, with the older version:

```bash
scripts/deploy.sh v0.2.0
```

You should see the same output as a deploy, ending in `deployed v0.2.0: eval …`, and
`/health` reports the older version. To find the last version that worked:
`grep ' ok ' deploy.log | tail -2`.

What you should see, from rolling v0.2.1 back to v0.2.0 on Nov 24 (with
`SKIP_EVAL=1`, since v0.2.1 had just passed the evaluation set):

```text
deploying v0.2.0 (last good: v0.2.1)
image ghcr.io/rakupper/assignment_for_6700@sha256:77bd6507c0ff2fbd86f1549afa103790e08377a0b68c3c46c74bac1ee6d86a49
schema is at 2: nothing to do
 ✔ Container assignment_for_6700-qdrant-1  Healthy
 ✔ Container assignment_for_6700-llm-1     Healthy
 ✔ Container assignment_for_6700-app-1     Healthy
 ✔ Container assignment_for_6700-caddy-1   Healthy
deployed v0.2.0: eval skipped
```

It took about a minute, and `/health` reported `0.2.0` right after. The digest is the
one v0.2.0 was first deployed with, so a rollback runs the exact image that worked
before, not a rebuild. Rolling forward again is the same command with the newer
version. `deploy.log` afterwards:

```text
2026-11-24T15:27:22Z v0.2.1 ghcr.io/rakupper/assignment_for_6700@sha256:9967adaa... ok eval 19/25
2026-11-24T15:29:32Z v0.2.0 ghcr.io/rakupper/assignment_for_6700@sha256:77bd6507... ok eval skipped
2026-11-24T15:47:57Z v0.2.1 ghcr.io/rakupper/assignment_for_6700@sha256:9967adaa... ok eval skipped
```

A rollback restarts the older image on the current data. Catalog migrations only move
forward, so a release that changes the catalog's shape must keep the previous release
working (expand and contract) before it ships.

## Reboot

On TEAM02:

```bash
sudo reboot
```

SSH disconnects (`Connection reset by peer`). Don't log back in; everything comes back
by itself. Watched from a laptop on Nov 24:

| Time (UTC) | What we saw |
| --- | --- |
| 14:33:57 | `sudo reboot` |
| 14:34:06 | The site stops answering |
| 14:34:19 | Caddy is up and answers 502: the app is still starting |
| 14:34:29 | `/health` and `/ready` answer 200, version 0.2.0 |

About 30 seconds in all, on the same IP, and `/ask` answered normally right after.
This works because every service has `restart: unless-stopped`, the release is pinned
in `.env`, and the certificate and data live in named volumes. To confirm the pinned
release came back, the last two lines of this must show the same digest:

```bash
grep APP_IMAGE .env
docker inspect --format '{{.Config.Image}}' assignment_for_6700-app-1
```

## Shelve and unshelve

Shelving stops the instance and its billing. Say so in the group chat first, so you
don't cut off someone who is testing.

1. Exosphere → TEAM02 → **Actions → Shelve**. Keeping the IP is not needed.
2. To use it again: **Actions → Unshelve**, wait for **Ready**, then give it a minute
   or two. Nobody needs to log in.

What we saw on Nov 20: after an unshelve that gave TEAM02 a new IP, the hostname
already pointed at it, and https://team02.cis240466.projects.jetstream-cloud.org
answered again about a minute after boot. Then [check it's healthy](#check-its-healthy).

Keep TEAM02 running from Dec 1 through the presentation on Dec 2.

## Back up and restore

Not available yet: #55 adds `scripts/backup.sh` (copies the catalog with SQLite's
backup API and takes a Qdrant snapshot, then uploads both to a GitHub release, off
the instance) and `scripts/restore.sh` (checks a backup before replacing anything).
It waits for a fine-grained token that only the repo owner can create. This section
gets the exact commands and the result of our first restore once it's merged.

Until then: catalog migrations copy the catalog to `/data/backups/` before changing
it, and the data can always be rebuilt from eCFR ([fresh install](#fresh-install),
step 3, about 25 minutes).

## Fresh install

For a new instance, or TEAM02 from nothing. About 35 minutes, most of it loading the
search index.

1. **Instance.** In Exosphere, an m3.small (2 CPUs, 6 GB) with a 40 GB volume
   attached, and Docker's storage on that volume: see
   [Data volume](deployment.md#data-volume-first-time-only). Note its hostname
   (`<instance>.<project>.projects.jetstream-cloud.org`). The default security group
   already lets 80 and 443 in.
2. **Code and settings.** On the instance:

   ```bash
   git clone https://github.com/RAkupper/Assignment_for_6700.git
   cd Assignment_for_6700
   HOST=team02.cis240466.projects.jetstream-cloud.org   # the hostname from step 1
   printf 'COMPOSE_PROFILES=proxy\nAPP_DOMAIN=%s\nAPP_BIND=127.0.0.1\nFORWARDED_ALLOW_IPS=*\nAPP_ADMIN_API_KEY=%s\n' \
     "$HOST" "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" > .env
   grep -v ADMIN .env   # four lines; the key stays out of your terminal history
   ```

   `.env` holds only what differs from `.env.production.example` and the admin key.
   It is gitignored; never commit it or paste the key anywhere.
3. **Data.** Build the catalog and the search index from eCFR (about 25 minutes; the
   loader takes 20 on one CPU):

   ```bash
   docker compose run --rm jobs python -m myapp.ingest.catalog migrate
   docker compose run --rm jobs python -m myapp.ingest fetch --part "8 CFR 214"
   docker compose run --rm jobs python -m myapp.ingest parse --part "8 CFR 214"
   docker compose run --rm jobs python -m myapp.vectordb.loader --activate
   docker compose run --rm jobs python -m myapp.vectordb.collection status
   ```

   The first command builds the image from the checkout, which is fine for loading;
   Compose first says `pull access denied for assignment_for_6700-app`, which is
   expected.
   The loader prints progress such as `upserted 128 points (1664 total)`. The last
   command should show the alias on a collection with 3,882 points:

   ```text
   alias provisions -> provisions_v1
   provisions_v1: 3882 points  (live)
   ```

4. **Release.** Deploy the current version, which also starts Caddy and gets the
   certificate:

   ```bash
   scripts/deploy.sh v0.2.0
   ```

   It should end with `deployed v0.2.0: eval 19/25`. Then
   `docker compose logs caddy | grep -i "certificate obtained"` should show
   `certificate obtained successfully`, and the [health check](#check-its-healthy)
   should pass from your laptop.
