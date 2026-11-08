#!/bin/sh
# Entry point of the all-in-one image (Dockerfile.allinone): broker + City Brain + mocks + timeline.
#
#   BRAIN_MOCKS     twin,guardian (default) | twin | guardian | none   which partner layers are mocked
#   BRAIN_LLM       ollama (default, Ollama on the host) | mock (scripted, no GPU)
#   BROKER_HOSTS    extra names / IPs for the broker certificate, comma-separated (this laptop's LAN
#                   IP, so the Twin and Guardian teams can connect from their machines)
#
# The secrets (CA, broker certificate, accounts, HMAC keys) live in the mounted brain/secrets
# folder: created on the first start if missing, then shared with the partner teams.
set -eu
cd /app/brain

if [ ! -f secrets/broker/passwd ] || [ ! -f secrets/broker/server.crt ]; then
    echo "[allinone] no secrets yet: creating them in brain/secrets (share ca.crt, the passwords and keys with the partners)"
    set --
    for h in $(echo "${BROKER_HOSTS:-}" | tr ',' ' '); do set -- "$@" --host "$h"; done
    python tools/setup.py "$@"
fi

echo "[allinone] starting the MQTT broker (TLS 8883)"
mosquitto -c /app/brain/broker/mosquitto.conf &
BROKER=$!
trap 'kill $BROKER 2>/dev/null || true' EXIT INT TERM

set -- python run.py
case "${BRAIN_MOCKS:-twin,guardian}" in
    none|"") ;;
    *) set -- "$@" --mocks "${BRAIN_MOCKS:-twin,guardian}" ;;
esac
[ "${BRAIN_LLM:-ollama}" = "mock" ] && set -- "$@" --mock-llm
echo "[allinone] $*"
"$@"
