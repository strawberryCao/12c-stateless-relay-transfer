#!/bin/sh
set -eu
printf '{}\n' > /tmp/registry_server.config.json
printf '{}\n' > /tmp/relay_server.config.json
python /opt/12c/render_config.py \
  /opt/12c/config/console_server.config.template.json \
  /run/12c/console_server.config.json
exec python -m console_server
