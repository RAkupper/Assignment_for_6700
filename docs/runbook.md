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

We have one release so far, so the rollback hasn't been run on TEAM02 yet; the
script's failure and rollback paths were tested with stand-ins for `git`, `docker` and
`curl` (#62). After the next release, roll back once and paste the output here.

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

Each backup is a `data-<UTC time>` release in this repo, off Jetstream2. What it
holds and how big it is: [Backups off Jetstream2](deployment.md#backups-off-jetstream2).
Take one after every load that changes the data.

### Take one (manual upload)

We have no upload token, so a backup is made on TEAM02 and uploaded through GitHub's
website. Anyone with push access to the repo can do this. Restoring and listing read
the public releases and need no token.

1. On TEAM02, make the backup and copy it out of the Docker volume:

   ```bash
   tag=data-$(date -u +%Y%m%dT%H%M%SZ)
   docker compose run --rm -T jobs python -m myapp.backup create --out /data/backups/$tag
   mkdir -p ~/backup-out
   docker compose run --rm -T --no-deps --user 0 -v ~/backup-out:/out jobs \
     sh -c "cp -r /data/backups/$tag /out/ && chown -R $(id -u):$(id -g) /out"
   echo $tag
   ```

   Later backups leave `/data/backups/` out, so they don't nest.

2. On your laptop, copy it down (use the tag the last command printed):

   ```bash
   scp -r exouser@team02.cis240466.projects.jetstream-cloud.org:~/backup-out/<tag> .
   ```

3. On GitHub, **Releases → Draft a new release**. Create a new tag on `main` named
   exactly like the folder (`data-...`; restore only finds tags that start with
   `data-`), and use the same name as the title. Attach `catalog.sqlite`,
   `data-files.tar.gz`, `qdrant.snapshot` and `manifest.json`. Under **Release label**
   choose **Pre-release**, so the latest release stays the app's version (GitHub never
   marks a pre-release as latest; restore still finds it), then publish.

4. Check that the app sees it:

   ```bash
   docker compose run --rm jobs python -m myapp.backup list
   ```

What you should see, from the first backup on TEAM02 (Nov 24, on v0.3.0). Step 1:

```text
INFO backed up provisions_v2 (3882 points)
data-20261124T202116Z
```

`ls -la ~/backup-out/<tag>` then shows the four files, 43 MB in all:

```text
-rw-r--r-- 1 exouser exouser  4050944 Nov 24 20:21 catalog.sqlite
-rw-r--r-- 1 exouser exouser 13590269 Nov 24 20:21 data-files.tar.gz
-rw-r--r-- 1 exouser exouser      656 Nov 24 20:21 manifest.json
-rw-r--r-- 1 exouser exouser 25363456 Nov 24 20:21 qdrant.snapshot
```

And step 4, after publishing:

```text
data-20261124T202116Z  43 MB  https://github.com/RAkupper/Assignment_for_6700/releases/tag/data-20261124T202116Z
```

Old backups are not deleted automatically. Delete them on the Releases page when
there are more than a few.

### Automatic uploads (needs a token)

`scripts/backup.sh` does steps 1 to 4 in one command and keeps the newest 5, but it
needs a token we don't have yet. Until there is one, don't install the timer below:
it would fail every week.

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

Then a backup is one command: `scripts/backup.sh`. It creates a draft release,
uploads the four files, and only then publishes it, so a failed upload never looks
like a backup. A systemd timer can run it every week; `Persistent=true` catches up
after a shelve. Install it once, on the instance:

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

What you should see, from the first drill on TEAM02 (Nov 24), restoring the backup
above from GitHub onto empty volumes:

```text
INFO downloading data-20261124T202116Z
INFO verified backup of provisions_v2 from 2026-11-24T20:21:22+00:00
INFO loaded provisions_v1: 3882 points
INFO restored the catalog: 3882 versions
INFO alias provisions: None -> provisions_v1
alias provisions -> provisions_v1
provisions_v1: 3882 points  (live)
```

The collection comes back as `provisions_v1` because the drill's Qdrant was empty: a
restore always loads into the next free version and moves the alias there. The last
command removes the drill's volumes and leaves the live stack untouched.

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

   If a backup exists, run `scripts/restore.sh` instead of the fetch, parse and
   loader commands; it takes seconds rather than 25 minutes. See [Restore](#restore).

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
