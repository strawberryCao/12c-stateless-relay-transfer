#!/bin/sh
set -eu
cp /opt/12c/config/registry_server.config.json /run/12c/registry_server.config.json
exec python -m registry_server
