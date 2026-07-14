#!/usr/bin/env bash
set -euo pipefail

test "$(id -u)" -eq 0
generated="$(cd "$(dirname "$0")/.." && pwd)"
cd "$generated"

prod_id="$(docker compose ps -q minecraft)"
dev_id="$(docker compose --profile staging ps -q minecraft-dev)"
prod_running=false
dev_running=false

if test -n "$prod_id" && test "$(docker inspect --format '{{.State.Running}}' "$prod_id")" = true; then
    prod_running=true
fi
if test -n "$dev_id" && test "$(docker inspect --format '{{.State.Running}}' "$dev_id")" = true; then
    dev_running=true
fi
if "$prod_running" && "$dev_running"; then
    echo "both production and staging are running; refusing to continue" >&2
    exit 1
fi
if "$prod_running"; then
    echo "active-instance=production (already running)"
    exit 0
fi

rollback() {
    status=$?
    set +e
    echo "switch to production: FAIL (status=$status source-line=${BASH_LINENO[0]})" >&2
    if "$dev_running"; then
        current_prod_id="$(docker compose ps -q minecraft)"
        if test -n "$current_prod_id" \
            && test "$(docker inspect --format '{{.State.Running}}' "$current_prod_id")" = true \
            && timeout 1 bash -c '</dev/tcp/127.0.0.1/25565' 2>/dev/null \
            && timeout 1 bash -c '</dev/tcp/127.0.0.1/25575' 2>/dev/null; then
            docker compose --profile staging stop --timeout 120 minecraft-dev
        else
            docker compose --profile staging up -d minecraft-dev
        fi
    fi
    exit "$status"
}
trap rollback ERR

if "$dev_running"; then
    docker exec --user 10001 "$dev_id" mc-send-to-console \
        say "[Maintenance] Switching to the stable server in 60 seconds."
    sleep 30
    docker exec --user 10001 "$dev_id" mc-send-to-console \
        say "[Maintenance] Switching to stable in 30 seconds."
    sleep 20
    docker exec --user 10001 "$dev_id" mc-send-to-console \
        say "[Maintenance] Switching to stable in 10 seconds."
    sleep 10
    docker exec --user 10001 "$dev_id" mc-send-to-console save-all flush
    sleep 5
    docker compose --profile staging stop --timeout 120 minecraft-dev
    echo "staging graceful stop: PASS"
fi

docker compose up -d minecraft

ready=false
for elapsed in $(seq 0 5 300); do
    prod_id="$(docker compose ps -q minecraft)"
    test -n "$prod_id"
    state="$(docker inspect --format '{{.State.Status}}' "$prod_id")"
    if test "$state" != running; then
        echo "production Minecraft stopped during startup: state=$state" >&2
        exit 1
    fi
    if timeout 1 bash -c '</dev/tcp/127.0.0.1/25565' 2>/dev/null \
        && timeout 1 bash -c '</dev/tcp/127.0.0.1/25575' 2>/dev/null; then
        ready=true
        echo "production readiness: PASS (${elapsed}s)"
        break
    fi
    sleep 5
done
test "$ready" = true
test -z "$(docker compose --profile staging ps -q minecraft-dev)"

trap - ERR
echo "switch to production: PASS"
echo "active-instance=production"
echo "java=sb.mc-remote.com:25565"
echo "mcremote=sb.mc-remote.com:25575"
