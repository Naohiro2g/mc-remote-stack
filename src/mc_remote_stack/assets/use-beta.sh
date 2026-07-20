#!/usr/bin/env bash
set -euo pipefail

test "$(id -u)" -eq 0
generated="$(cd "$(dirname "$0")/.." && pwd)"
cd "$generated"

stable_id="$(docker compose --profile stable ps -q minecraft-stable)"
beta_id="$(docker compose --profile beta ps -q minecraft-beta)"
stable_running=false
beta_started=false

if test -n "$stable_id" && test "$(docker inspect --format '{{.State.Running}}' "$stable_id")" = true; then
    stable_running=true
fi
if test -n "$beta_id" && test "$(docker inspect --format '{{.State.Running}}' "$beta_id")" = true; then
    if "$stable_running"; then
        echo "both stable and beta are running; refusing to continue" >&2
        exit 1
    fi
    echo "active-channel=beta (already running)"
    exit 0
fi

rollback() {
    status=$?
    set +e
    echo "switch to beta: FAIL (status=$status source-line=${BASH_LINENO[0]})" >&2
    if "$beta_started"; then
        docker compose --profile beta stop --timeout 120 minecraft-beta
    fi
    if "$stable_running"; then
        docker compose --profile stable up -d minecraft-stable
    fi
    exit "$status"
}
trap rollback ERR

if "$stable_running"; then
    docker exec --user 10001 "$stable_id" mc-send-to-console \
        say "[Maintenance] Switching to the beta server in 60 seconds."
    sleep 30
    docker exec --user 10001 "$stable_id" mc-send-to-console \
        say "[Maintenance] Switching to beta in 30 seconds."
    sleep 20
    docker exec --user 10001 "$stable_id" mc-send-to-console \
        say "[Maintenance] Switching to beta in 10 seconds."
    sleep 10
    docker exec --user 10001 "$stable_id" mc-send-to-console save-all flush
    sleep 5
    docker compose --profile stable stop --timeout 120 minecraft-stable
    echo "stable graceful stop: PASS"
fi

docker compose --profile beta up -d minecraft-beta
beta_started=true

ready=false
for elapsed in $(seq 0 5 300); do
    beta_id="$(docker compose --profile beta ps -q minecraft-beta)"
    test -n "$beta_id"
    state="$(docker inspect --format '{{.State.Status}}' "$beta_id")"
    if test "$state" != running; then
        echo "minecraft-beta stopped during startup: state=$state" >&2
        exit 1
    fi
    if timeout 1 bash -c '</dev/tcp/127.0.0.1/25565' 2>/dev/null \
        && timeout 1 bash -c '</dev/tcp/127.0.0.1/25575' 2>/dev/null; then
        ready=true
        echo "minecraft-beta readiness: PASS (${elapsed}s)"
        break
    fi
    sleep 5
done
test "$ready" = true
test -z "$(docker compose --profile stable ps -q minecraft-stable)"

trap - ERR
echo "switch to beta: PASS"
echo "active-channel=beta"
echo "java=sb-beta.mc-remote.com:25565"
echo "mcremote=sb-beta.mc-remote.com:25575"
