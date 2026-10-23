#!/usr/bin/env bash
set -euo pipefail

: "${DUCKDNS_DOMAIN:?set DUCKDNS_DOMAIN}"
: "${DUCKDNS_TOKEN:?set DUCKDNS_TOKEN}"

response="$(curl -fsS --retry 3 "https://www.duckdns.org/update?domains=${DUCKDNS_DOMAIN}&token=${DUCKDNS_TOKEN}&ip=")"
if [ "${response}" != "OK" ]; then
  echo "duckdns update failed" >&2
  exit 1
fi
