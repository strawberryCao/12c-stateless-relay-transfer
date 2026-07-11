#!/bin/sh
set -eu

: "${PUBLIC_REGISTRY_URL:?PUBLIC_REGISTRY_URL is required}"

cat > /srv/relay.config.json <<EOF
{
  "registry": {
    "url": "${PUBLIC_REGISTRY_URL}"
  }
}
EOF

exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
