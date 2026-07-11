# GitHub Actions deployment

After the production PR is merged into `main`, configure the repository **production** environment with these secrets:

| Secret | Value |
|---|---|
| `PRODUCTION_SSH_HOST` | Server public IP or SSH hostname |
| `PRODUCTION_SSH_USER` | Non-root Linux SSH user |
| `PRODUCTION_SSH_PORT` | SSH port, usually `22` |
| `PRODUCTION_SSH_PRIVATE_KEY` | Private deploy key accepted by the server |
| `PRODUCTION_SSH_KNOWN_HOSTS` | Pinned known-hosts line for the server |

Do not paste the private key into source files, issues, pull requests or chat logs.

Generate the known-hosts entry from a trusted administrator machine and compare its fingerprint with the cloud provider/server console before saving it:

```bash
ssh-keyscan -p 22 SERVER_IP
```

Then open **Actions → Deploy production server → Run workflow** and enter:

- Web/PWA domain
- Registry client API domain
- Relay data-plane domain
- ACME certificate email

The workflow connects with strict host-key checking, updates the server checkout, preserves `deploy/.env`, generates secrets on the server when missing, and runs `deploy/configure-production.sh`.
