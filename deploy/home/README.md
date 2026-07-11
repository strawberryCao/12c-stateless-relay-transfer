# Windows home server with Cloudflare Quick Tunnel

This mode is for temporary public demos from a Windows 11 laptop without a domain or Cloudflare account.

## Network boundary

- `127.0.0.1:8088`: only local public gateway; Quick Tunnel connects here.
- `127.0.0.1:8070`: local management console.
- Registry `8080` and Relay `9090`: Docker internal network only.
- Public Registry routes are limited to `resolve`, `reserve-tokens`, and `abandon-replica-placements`.
- Public Relay routes are limited to `GET/PUT /block/<64-hex-token>`.
- Registration, heartbeat, overwrite verification, admin APIs, health endpoints, and Console are not published.

## First-time prerequisites

Run an elevated PowerShell:

```powershell
wsl --install --no-distribution
winget install --id Docker.DockerDesktop -e --accept-package-agreements --accept-source-agreements
winget install --id Cloudflare.cloudflared -e --accept-package-agreements --accept-source-agreements
```

Restart Windows if requested, open Docker Desktop once, accept its terms, and wait until `docker info` succeeds.

## Start

```powershell
cd C:\Users\ASUS\Desktop\bigsoft\12c-stateless-relay-transfer
git switch main
git pull origin main
Set-ExecutionPolicy -Scope Process Bypass
.\deploy\home\start-home.ps1 -DataRoot "C:\Users\ASUS\Desktop\bigsoftdata"
```

The first build compiles WASM and can take a long time. The script prints and saves the random public URL in:

```text
deploy\home\runtime\public-url.txt
```

## Status

```powershell
.\deploy\home\status-home.ps1
```

## Stop

```powershell
.\deploy\home\stop-home.ps1
```

Data remains under the selected data directory.

## Important limitation

A Quick Tunnel URL is temporary and changes when the tunnel is restarted. It is intended for testing and demos, not a stable production service. For a permanent URL and reliable PWA installation, use a domain and a named Cloudflare Tunnel later.
