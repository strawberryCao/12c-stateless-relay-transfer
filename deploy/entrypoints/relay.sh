#!/bin/sh
set -eu
python /opt/12c/render_config.py \
  /opt/12c/config/relay_server.config.template.json \
  /run/12c/relay_server.config.json
exec python -m relay_server
