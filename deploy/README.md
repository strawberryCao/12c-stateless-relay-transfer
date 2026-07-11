# 12C production deployment

This deployment runs Registry, Relay, Console, the Web gateway and Caddy with
Docker Compose. Only Caddy publishes ports 80/443. Console binds to
`127.0.0.1:8070`; Registry and Relay stay on an internal Docker network.

## Public route allowlist

- `/` and static assets: Web client
- `POST /registry/api/relay/resolve`
- `POST /registry/api/relay/reserve-tokens`
- `POST /registry/api/relay/abandon-replica-placements`
- `GET|PUT /relay/<64-hex-token>`

Registry registration, block registration, heartbeat, overwrite verification,
both admin APIs, health details, docs/OpenAPI and the Console are not routable
from the public gateway. Nginx also applies per-client rate limits, a 17 MiB body
limit and browser security headers.

## Server setup

Requirements: a Linux server, Docker Engine with Compose v2, a DNS A/AAAA record
pointing to the server, and inbound TCP 80/443 plus UDP 443 allowed by the host
firewall.

```bash
git clone https://github.com/Bamder/12c-stateless-relay-transfer.git
cd 12c-stateless-relay-transfer
git switch <deployment-branch>
cd deploy
chmod +x init-env.sh deploy.sh approve-relay.sh
./init-env.sh share.example.com
./deploy.sh
```

Caddy obtains and renews the public TLS certificate. The first deployment sends
Relay's registration request over the internal network and `deploy.sh` approves
that exact install ID with the Registry admin key.

Access Console only through an SSH tunnel:

```bash
ssh -L 8070:127.0.0.1:8070 user@server
```

Then open `http://127.0.0.1:8070`. Do not open port 8070 in the cloud security
group or host firewall.

Persistent data lives in named volumes `registry_data`, `relay_data`,
`caddy_data` and `caddy_config`. Back up both application data volumes together.
