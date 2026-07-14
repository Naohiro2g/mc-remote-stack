#!/usr/bin/env bash
set -euo pipefail

test "$(id -u)" -eq 0
generated="$(cd "$(dirname "$0")/.." && pwd)"
cd "$generated"

prod_id="$(docker compose ps -q minecraft)"
dev_id="$(docker compose --profile staging ps -q minecraft-dev)"
prod_running=false
dev_started=false

if test -n "$prod_id" && test "$(docker inspect --format '{{.State.Running}}' "$prod_id")" = true; then
    prod_running=true
fi
if test -n "$dev_id" && test "$(docker inspect --format '{{.State.Running}}' "$dev_id")" = true; then
    if "$prod_running"; then
        echo "both production and staging are running; refusing to continue" >&2
        exit 1
    fi
    echo "active-instance=staging (already running)"
    exit 0
fi

rollback() {
    status=$?
    set +e
    echo "switch to staging: FAIL (status=$status source-line=${BASH_LINENO[0]})" >&2
    if "$dev_started"; then
        docker compose --profile staging stop --timeout 120 minecraft-dev
    fi
    if "$prod_running"; then
        docker compose up -d minecraft
    fi
    exit "$status"
}
trap rollback ERR

if "$prod_running"; then
    docker exec --user 10001 "$prod_id" mc-send-to-console \
        say "[Maintenance] Switching to the sb-dev staging server in 60 seconds."
    sleep 30
    docker exec --user 10001 "$prod_id" mc-send-to-console \
        say "[Maintenance] Switching to sb-dev in 30 seconds."
    sleep 20
    docker exec --user 10001 "$prod_id" mc-send-to-console \
        say "[Maintenance] Switching to sb-dev in 10 seconds."
    sleep 10
    docker exec --user 10001 "$prod_id" mc-send-to-console save-all flush
    sleep 5
    docker compose stop --timeout 120 minecraft
    echo "production graceful stop: PASS"
fi

docker compose --profile staging up -d minecraft-dev
dev_started=true

ready=false
for elapsed in $(seq 0 5 300); do
    dev_id="$(docker compose --profile staging ps -q minecraft-dev)"
    test -n "$dev_id"
    state="$(docker inspect --format '{{.State.Status}}' "$dev_id")"
    if test "$state" != running; then
        echo "minecraft-dev stopped during startup: state=$state" >&2
        exit 1
    fi
    if timeout 1 bash -c '</dev/tcp/127.0.0.1/25566' 2>/dev/null \
        && timeout 1 bash -c '</dev/tcp/127.0.0.1/25576' 2>/dev/null; then
        ready=true
        echo "minecraft-dev readiness: PASS (${elapsed}s)"
        break
    fi
    sleep 5
done
test "$ready" = true
test -z "$(docker compose ps -q minecraft)"

trap - ERR
echo "switch to staging: PASS"
echo "active-instance=staging"
echo "java=sb-dev.mc-remote.com:25566"
echo "mcremote=sb-dev.mc-remote.com:25576"
