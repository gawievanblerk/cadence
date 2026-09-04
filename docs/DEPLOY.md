# Deploying the test server

One small VM, three containers, and a deploy that runs from GitHub on every
merge to `main`. This is the setup for a test server that people click
through. It is not a production setup for real client data: sign in is still a
placeholder, and the notes at the end of `COUNSELLOR-MANUAL.md` still apply.

## What runs where

| Container | Image | Job |
|---|---|---|
| cadence | built from `Dockerfile` | The engine, the API and the three screens |
| postgres | `postgres:16-alpine` | Client records, on a named volume |
| caddy | `caddy:2-alpine` | HTTPS. Fetches and renews the certificate itself |

Everything lives in `/opt/cadence` on the VM. Secrets live in
`/opt/cadence/.env`, which is never in git and never copied by the deploy.

## 1. The VM

A Hetzner CX22 (2 vCPU, 4 GB) running Ubuntu 24.04 is plenty. Pick the
location your data must stay in.

```bash
apt-get update && apt-get install -y ca-certificates curl rsync
curl -fsSL https://get.docker.com | sh
mkdir -p /opt/cadence/backups
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable
```

Point a DNS `A` record at the VM, for example `cadence-test.yourdomain.com`.
Caddy will not get a certificate until that record resolves.

## 2. The `.env` file on the VM

Create `/opt/cadence/.env`:

```bash
DB_PASSWORD=<long random string>
CADENCE_DOMAIN=cadence-test.yourdomain.com
CADENCE_ENCRYPTION_KEY=<32 bytes, base64>

# Optional. Leave these out and the offline counsellor answers.
CADENCE_ROLE_COUNSELLOR=anthropic:claude-sonnet-4-5
CADENCE_ROLE_EXTRACTOR=anthropic:claude-haiku-4-5
CADENCE_ROLE_SUMMARISER=anthropic:claude-haiku-4-5
ANTHROPIC_API_KEY=<key>
```

Generate the two secrets with:

```bash
openssl rand -hex 24
python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
```

Lock the file down: `chmod 600 /opt/cadence/.env`.

## 3. A deploy key

On your laptop, make a key that only GitHub will use:

```bash
ssh-keygen -t ed25519 -C cadence-deploy -f ~/.ssh/cadence-deploy -N ""
ssh-copy-id -i ~/.ssh/cadence-deploy.pub root@<vm ip>
ssh-keyscan -H <vm ip>
```

## 4. GitHub secrets and variables

In the repository settings, under Secrets and variables, Actions.

Secrets:

| Name | Value |
|---|---|
| `SSH_PRIVATE_KEY` | The contents of `~/.ssh/cadence-deploy` |
| `SSH_KNOWN_HOSTS` | The output of the `ssh-keyscan` line above |
| `SSH_USER` | `root`, or the user you created |
| `SSH_HOST` | The VM's IP address |
| `DEPLOY_PATH` | `/opt/cadence` |

Variables:

| Name | Value |
|---|---|
| `DEPLOY_ENABLED` | `true`. The deploy job does not run until this exists |
| `CADENCE_URL` | `https://cadence-test.yourdomain.com`, no trailing slash |

## 5. First deploy

Merge anything to `main`, or open the CI workflow in the Actions tab and press
Run workflow. The deploy job:

1. Runs only after the tests, the Postgres tests and the image build pass.
2. Snapshots the database into `/opt/cadence/backups` if one is running.
3. Copies the code and the programme to `/opt/cadence` over SSH.
4. Rebuilds and restarts the three containers.
5. Checks that every container is running and that `/healthz` answers over
   HTTPS.

The first run takes a few minutes while the image builds and Caddy fetches the
certificate.

## 6. Put demo clients in

The screens are empty until someone enrols. For a demo, seed three scripted
clients:

```bash
ssh root@<vm ip>
cd /opt/cadence
docker compose -f docker-compose.prod.yml exec cadence python scripts/seed_demo.py --reset
```

`--reset` drops every table first. Never run it against real client data.

## 7. Test it

Open these in a browser.

| Page | What to check |
|---|---|
| `/ui/` | Enrol a test client, work through the intake, watch the rules fire |
| `/ui/admin` | The caseload shows the seeded clients and their flags |
| `/ui/author` | Edit a threshold, save, and see the validator accept or refuse it |
| `/docs` | The API |
| `/healthz` | The programme version, its hash, and which model each role uses |

The screens ask for a role, not a password. That is the placeholder sign in.
Do not put real client data on this server.

## Day to day

```bash
cd /opt/cadence
docker compose -f docker-compose.prod.yml ps          # what is running
docker compose -f docker-compose.prod.yml logs -f cadence
docker compose -f docker-compose.prod.yml restart cadence   # after editing .env
ls -lh backups/                                        # pre-deploy snapshots
```

Restore a snapshot:

```bash
gunzip -c backups/pre-deploy-<stamp>.sql.gz | \
  docker compose -f docker-compose.prod.yml exec -T postgres psql -U cadence cadence
```

Change the AI model: edit the `CADENCE_ROLE_*` lines in `.env` and restart
the `cadence` container. No rebuild, no deploy.

## The same stack on a laptop

```bash
make docker-up                                   # Postgres plus the app on :8000
docker compose exec cadence python scripts/seed_demo.py --reset
open http://127.0.0.1:8000/ui/admin
make docker-down
```

`docker-compose.yml` has safe defaults for everything, so it needs no `.env`.
