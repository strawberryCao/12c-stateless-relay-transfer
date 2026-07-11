# Production deployment

This directory deploys the Web client, Registry, Relay and private Console on one Linux server with Docker Compose and automatic HTTPS.

## Public/private boundary

Public through Caddy:

- `https://APP_DOMAIN`: static Web client and WASM.
- `https://API_DOMAIN`: only the client Registry data-plane routes:
  - `POST /api/relay/resolve`
  - `POST /api/relay/reserve-tokens`
  - `POST /api/relay/abandon-replica-placements`
- `https://RELAY_DOMAIN`: only `GET` and `PUT` requests whose path is one 64-hex token.

Not public:

- Relay registration request/status, heartbeat, register and overwrite-verification routes.
- Registry and Relay Admin APIs.
- Console.
- Registry port `8080` and Relay port `9090`.

Registry, Relay and Console communicate on an isolated Docker network. Console is bound to `127.0.0.1:8070` and should be opened through an SSH tunnel only.

## Server requirements

Recommended baseline for a course/demo deployment:

- Ubuntu 24.04 LTS x86-64.
- 2 CPU cores, 4 GB RAM, 30 GB disk or more.
- Docker Engine with Docker Compose v2.
- Git and OpenSSL.
- A non-root sudo user with SSH key login.

The first gateway build compiles OpenSSL and the WASM module and can take several minutes.

## DNS and firewall

Create three DNS records pointing to the server public IP:

```text
app.example.com    -> server IP
api.example.com    -> server IP
relay.example.com  -> server IP
```

Allow inbound TCP `22`, `80`, `443` and UDP `443`. Do not open `8070`, `8080` or `9090` in the cloud firewall or UFW.

Example UFW policy:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 443/udp
sudo ufw enable
```

## First deployment

```bash
git clone https://github.com/strawberryCao/12c-stateless-relay-transfer.git
cd 12c-stateless-relay-transfer
git switch deploy/production-hardening

bash deploy/init-env.sh
nano deploy/.env
```

Replace these four values in `deploy/.env`:

```dotenv
APP_DOMAIN=app.example.com
API_DOMAIN=api.example.com
RELAY_DOMAIN=relay.example.com
ACME_EMAIL=admin@example.com
```

Do not modify or publish the generated secret values. Then deploy:

```bash
bash deploy/deploy.sh
```

The script builds the images, starts Registry and Relay, reads the local Relay install ID, approves only that registration request, waits for key bootstrap, then starts Console and the HTTPS gateway.

## Verify

```bash
cd deploy
docker compose --env-file .env -f compose.yaml ps
docker compose --env-file .env -f compose.yaml logs --tail=100 gateway registry relay
```

Open `https://APP_DOMAIN`, upload a small test file, copy the 12-character credential, and download it from another browser/device.

The following public requests should return `404`:

```text
https://API_DOMAIN/api/relay/registration-request
https://API_DOMAIN/api/relay/heartbeat
https://API_DOMAIN/api/admin/allowlist
https://RELAY_DOMAIN/api/admin/overview
```

## Private Console

From the administrator computer:

```bash
ssh -L 8070:127.0.0.1:8070 USER@SERVER_IP
```

Keep the SSH session open and browse to `http://127.0.0.1:8070`.

## Update

```bash
cd 12c-stateless-relay-transfer
git fetch --all
git switch deploy/production-hardening
git pull --ff-only
bash deploy/deploy.sh
```

Named volumes preserve the Registry database, Relay database, stored blocks and Caddy certificates across container replacement.

## Backup

Stop writes briefly, then archive the two data volumes:

```bash
cd deploy
docker compose --env-file .env -f compose.yaml stop gateway relay registry
mkdir -p backups
docker run --rm -v twelve-c-production_registry-data:/data -v "$PWD/backups:/backup" alpine \
  tar czf /backup/registry-data.tgz -C /data .
docker run --rm -v twelve-c-production_relay-data:/data -v "$PWD/backups:/backup" alpine \
  tar czf /backup/relay-data.tgz -C /data .
docker compose --env-file .env -f compose.yaml start registry relay gateway
```

Copy backups away from the server. Never commit `deploy/.env`, database files, Relay RSA keys or generated key stores.

## Security notes

- Relay registration is not reachable through the public gateway.
- `heartbeatUrlPolicy` is `strict`, preventing an approved Relay from changing its advertised URL through heartbeat.
- Admin keys and the block-auth master key are generated locally and are not stored in Git.
- Containers run as non-root where possible, drop Linux capabilities, use read-only root filesystems and persist only required data volumes.
- Use a CDN/WAF rate limit for `API_DOMAIN` and `RELAY_DOMAIN` before opening the service to untrusted high-volume traffic.
- Keep SSH key authentication enabled and disable password/root SSH login after confirming key access.

## Windows installer

The Electron desktop program is a hardened shell for the deployed HTTPS Web client. After the public site works, build locally on Windows:

```powershell
cd client
npm ci
$env:TWELVEC_APP_URL = "https://app.example.com"
npm run electron:build
```

The installer is written to `client/release/`. It contains no server/admin secret. A GitHub Actions workflow named **Build Windows desktop app** can also build the installer after this branch is merged into the default branch.
