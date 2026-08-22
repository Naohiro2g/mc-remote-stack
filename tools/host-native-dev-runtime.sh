#!/bin/bash
set -euo pipefail

RUNTIME_ROOT=/srv/mc-remote/dev-integration
SERVICE_USER=mcremote-dev
UNIT=mc-remote-dev-integration.service
JAVA_PORT=25565
MCREMOTE_PORT=25575

usage() {
  printf '%s\n' \
    'Usage:' \
    '  tools/host-native-dev-runtime.sh self-test' \
    '  tools/host-native-dev-runtime.sh check|install --paper FILE --paper-sha256 SHA256 --mcremote FILE --mcremote-sha256 SHA256 --config FILE --server-properties FILE --eula FILE --protocol VERSION' \
    '  tools/host-native-dev-runtime.sh verify --paper-sha256 SHA256 --mcremote-sha256 SHA256 --protocol VERSION'
}

log_has_full_readiness() {
  local entries=$1
  grep -F 'Credential domain health: HEALTHY (healthy)' <<<"$entries" >/dev/null &&
    grep -F 'Server started at port 25575' <<<"$entries" >/dev/null &&
    grep -F 'Done (' <<<"$entries" >/dev/null
}

self_test() {
  local health socket done_line
  health='Credential domain health: HEALTHY (healthy)'
  socket="Server started at port $MCREMOTE_PORT"
  done_line='Done (fixture)'
  if log_has_full_readiness "$health"; then exit 1; fi
  if log_has_full_readiness "$health
$socket"; then exit 1; fi
  if log_has_full_readiness "$health
$done_line"; then exit 1; fi
  log_has_full_readiness "$health
$socket
$done_line"
  echo 'PASS readiness barrier requires health+socket+paper-done'
}

mode=${1:-}
[[ -n "$mode" ]] || { usage >&2; exit 2; }
shift || true
if [[ "$mode" == self-test ]]; then
  (($# == 0)) || { usage >&2; exit 2; }
  self_test
  exit 0
fi
case "$mode" in
  check|install|verify) ;;
  *) usage >&2; exit 2 ;;
esac

paper_source=
paper_sha=
mcremote_source=
mcremote_sha=
config_source=
properties_source=
eula_source=
protocol=
while (($#)); do
  case "$1" in
    --paper) paper_source=${2:-}; shift 2 ;;
    --paper-sha256) paper_sha=${2:-}; shift 2 ;;
    --mcremote) mcremote_source=${2:-}; shift 2 ;;
    --mcremote-sha256) mcremote_sha=${2:-}; shift 2 ;;
    --config) config_source=${2:-}; shift 2 ;;
    --server-properties) properties_source=${2:-}; shift 2 ;;
    --eula) eula_source=${2:-}; shift 2 ;;
    --protocol) protocol=${2:-}; shift 2 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$paper_sha" =~ ^[0-9a-f]{64}$ ]] || { echo 'invalid --paper-sha256' >&2; exit 2; }
[[ "$mcremote_sha" =~ ^[0-9a-f]{64}$ ]] || { echo 'invalid --mcremote-sha256' >&2; exit 2; }
[[ "$protocol" =~ ^[0-9]+[.][0-9]+[.][0-9]+$ ]] || { echo 'invalid --protocol' >&2; exit 2; }

if [[ "$mode" != verify ]]; then
  for source in "$paper_source" "$mcremote_source" "$config_source" "$properties_source" "$eula_source"; do
    [[ -f "$source" && ! -L "$source" ]] || { echo "missing or symlink input: $source" >&2; exit 2; }
  done
  printf '%s  %s\n' "$paper_sha" "$paper_source" | sha256sum -c -
  printf '%s  %s\n' "$mcremote_sha" "$mcremote_source" | sha256sum -c -
  grep -Fx 'eula=true' "$eula_source" >/dev/null
  grep -F "credential_store_path: \"$RUNTIME_ROOT/credential-store/snapshot.json\"" "$config_source" >/dev/null
  grep -F "revocation_authority_path: \"$RUNTIME_ROOT/credential-revocations\"" "$config_source" >/dev/null
fi

command -v java >/dev/null
command -v journalctl >/dev/null
command -v runuser >/dev/null
command -v ss >/dev/null
command -v systemctl >/dev/null
java -version 2>&1 | grep -F 'version "21.0.11"' >/dev/null
if command -v docker >/dev/null 2>&1; then
  test -z "$(docker ps -q --filter label=com.docker.compose.project=dev-integration)"
fi

if [[ "$mode" == check ]]; then
  test -z "$(ss -H -lnt | awk -v java_port="$JAVA_PORT" -v mcremote_port="$MCREMOTE_PORT" '
    $4 ~ (":" java_port "$") || $4 ~ (":" mcremote_port "$") {print}
  ')"
  echo 'OK host-native dev preflight inputs=exact docker-runtime=absent java=21'
  exit 0
fi

test "$(id -u)" -eq 0 || { echo 'install and verify must run as root' >&2; exit 2; }

bootstrap_pid=
bootstrap_fifo="$RUNTIME_ROOT/data/bootstrap-console.fifo"
bootstrap_log="$RUNTIME_ROOT/data/bootstrap-console.log"
cleanup() {
  local status=$?
  trap - EXIT
  if [[ $status -ne 0 ]]; then
    systemctl stop "$UNIT" 2>/dev/null || true
    if [[ -n "$bootstrap_pid" ]] && kill -0 "$bootstrap_pid" 2>/dev/null; then
      kill -- "-$bootstrap_pid" 2>/dev/null || true
      wait "$bootstrap_pid" 2>/dev/null || true
    fi
    rm -f "$bootstrap_fifo"
    echo "FAIL host-native dev runtime mode=$mode; inspect journalctl -u $UNIT and $bootstrap_log" >&2
  fi
  exit "$status"
}
trap cleanup EXIT

journal_cursor() {
  journalctl -u "$UNIT" -n 0 --show-cursor --no-pager \
    | sed -n 's/^-- cursor: //p'
}

wait_for_full_readiness_after() {
  local cursor=$1 entries
  for _ in $(seq 1 180); do
    entries=$(journalctl -u "$UNIT" --after-cursor="$cursor" --no-pager)
    if systemctl is-active --quiet "$UNIT" && \
      log_has_full_readiness "$entries" && \
      ss -H -lnt | awk -v java_port="$JAVA_PORT" -v mcremote_port="$MCREMOTE_PORT" '
        $4 ~ (":" java_port "$") {java=1}
        $4 ~ (":" mcremote_port "$") {mcremote=1}
        END {exit !(java && mcremote)}
      '; then
      return 0
    fi
    sleep 1
  done
  return 1
}

credential_domain_id() {
  python3 -c "import json; print(json.load(open('$RUNTIME_ROOT/credential-store/snapshot.json'))['credential_domain_id'])"
}

probe_auth_required() {
  MCREMOTE_DEV_PROTOCOL="$protocol" MCREMOTE_DEV_PORT="$MCREMOTE_PORT" python3 - <<'PY'
import json
import os
import socket
import time

request = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "hello",
    "params": {
        "protocol": os.environ["MCREMOTE_DEV_PROTOCOL"],
        "client": {"name": "stack-host-native-preflight", "version": "1"},
    },
}
deadline = time.monotonic() + 30
while True:
    try:
        connection = socket.create_connection(
            ("127.0.0.1", int(os.environ["MCREMOTE_DEV_PORT"])), timeout=5
        )
        break
    except OSError:
        if time.monotonic() >= deadline:
            raise
        time.sleep(0.1)
with connection:
    connection.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
    response = b""
    while not response.endswith(b"\n"):
        chunk = connection.recv(65536)
        if not chunk:
            break
        response += chunk
document = json.loads(response)
error = document.get("error", {})
assert error.get("message") == "auth_required", document
assert error.get("data", {}).get("reason") == "auth_required", document
PY
}

verify_mutable_ownership() {
  runuser -u "$SERVICE_USER" -- test -r "$RUNTIME_ROOT/data/eula.txt"
  runuser -u "$SERVICE_USER" -- test -w "$RUNTIME_ROOT/data/plugins/McRemote/config.yml"
  runuser -u "$SERVICE_USER" -- test -w "$RUNTIME_ROOT/credential-store"
  runuser -u "$SERVICE_USER" -- test -w "$RUNTIME_ROOT/credential-revocations"
}

verify_runtime_artifacts() {
  printf '%s  %s\n' "$paper_sha" "$RUNTIME_ROOT/artifacts/paper.jar" | sha256sum -c -
  printf '%s  %s\n' "$mcremote_sha" "$RUNTIME_ROOT/data/plugins/mc-remote.jar" | sha256sum -c -
  test "$(find "$RUNTIME_ROOT/data/plugins" -maxdepth 1 -type f -name 'mc-remote*.jar' | wc -l)" -eq 1
}

write_unit() {
  local temporary
  temporary=$(mktemp)
  printf '%s\n' \
    '[Unit]' \
    'Description=McRemote host-native normal dev runtime' \
    'Wants=network-online.target' \
    'After=network-online.target' \
    '' \
    '[Service]' \
    'Type=simple' \
    "User=$SERVICE_USER" \
    "Group=$SERVICE_USER" \
    "WorkingDirectory=$RUNTIME_ROOT/data" \
    "ExecStart=/usr/bin/java -Xms1G -Xmx4G -jar $RUNTIME_ROOT/artifacts/paper.jar --nogui" \
    'Restart=on-failure' \
    'RestartSec=5s' \
    'SuccessExitStatus=0 143' \
    'NoNewPrivileges=true' \
    'PrivateTmp=true' \
    'ProtectSystem=strict' \
    'ProtectHome=true' \
    "ReadWritePaths=$RUNTIME_ROOT/data $RUNTIME_ROOT/credential-store $RUNTIME_ROOT/credential-revocations" \
    '' \
    '[Install]' \
    'WantedBy=multi-user.target' \
    >"$temporary"
  install -o root -g root -m 0644 "$temporary" "/etc/systemd/system/$UNIT"
  rm -f "$temporary"
}

bootstrap_credential_domain() {
  rm -f "$bootstrap_log" "$bootstrap_fifo"
  mkfifo -m 0600 "$bootstrap_fifo"
  chown "$SERVICE_USER:$SERVICE_USER" "$bootstrap_fifo"
  exec 3<>"$bootstrap_fifo"
  (
    cd "$RUNTIME_ROOT/data"
    exec setsid runuser -u "$SERVICE_USER" -- /usr/bin/java -Xms1G -Xmx4G \
      -jar "$RUNTIME_ROOT/artifacts/paper.jar" --nogui
  ) <"$bootstrap_fifo" >"$bootstrap_log" 2>&1 &
  bootstrap_pid=$!
  for _ in $(seq 1 180); do
    grep -F 'Done (' "$bootstrap_log" >/dev/null 2>&1 && break
    kill -0 "$bootstrap_pid" 2>/dev/null || break
    sleep 1
  done
  grep -F 'Done (' "$bootstrap_log" >/dev/null
  printf 'mcremote credential bootstrap\n' >&3
  for _ in $(seq 1 60); do
    grep -F 'Credential domain bootstrapped:' "$bootstrap_log" >/dev/null 2>&1 && break
    kill -0 "$bootstrap_pid" 2>/dev/null || break
    sleep 1
  done
  grep -F 'Credential domain bootstrapped:' "$bootstrap_log" >/dev/null
  printf 'mcremote credential status\n' >&3
  for _ in $(seq 1 30); do
    grep -F 'Credential domain: HEALTHY / healthy / id=' "$bootstrap_log" >/dev/null 2>&1 && break
    kill -0 "$bootstrap_pid" 2>/dev/null || break
    sleep 1
  done
  grep -F 'Credential domain: HEALTHY / healthy / id=' "$bootstrap_log" >/dev/null
  printf 'stop\n' >&3
  for _ in $(seq 1 60); do
    kill -0 "$bootstrap_pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$bootstrap_pid" 2>/dev/null; then
    return 1
  fi
  wait "$bootstrap_pid"
  bootstrap_pid=
  exec 3>&-
  rm -f "$bootstrap_fifo"
}

verify_restart_lifecycle() {
  local domain_before start_cursor first_pid restart_cursor restart_pid
  domain_before=$(credential_domain_id)
  start_cursor=$(journal_cursor)
  [[ -n "$start_cursor" ]]
  systemctl start "$UNIT"
  wait_for_full_readiness_after "$start_cursor"
  first_pid=$(systemctl show "$UNIT" --property MainPID --value)
  [[ "$first_pid" -gt 0 ]]
  [[ "$domain_before" == "$(credential_domain_id)" ]]
  probe_auth_required

  restart_cursor=$(journal_cursor)
  [[ -n "$restart_cursor" ]]
  systemctl restart "$UNIT"
  wait_for_full_readiness_after "$restart_cursor"
  restart_pid=$(systemctl show "$UNIT" --property MainPID --value)
  [[ "$restart_pid" -gt 0 && "$restart_pid" -ne "$first_pid" ]]
  [[ "$domain_before" == "$(credential_domain_id)" ]]
  probe_auth_required
}

systemctl stop "$UNIT" 2>/dev/null || true
test -z "$(ss -H -lnt | awk -v java_port="$JAVA_PORT" -v mcremote_port="$MCREMOTE_PORT" '
  $4 ~ (":" java_port "$") || $4 ~ (":" mcremote_port "$") {print}
')"

if [[ "$mode" == install ]]; then
  if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$RUNTIME_ROOT" --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
  if [[ -e "$RUNTIME_ROOT" ]]; then
    archive="$RUNTIME_ROOT.failed-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    [[ ! -e "$archive" ]]
    mv "$RUNTIME_ROOT" "$archive"
  fi
  install -d -o root -g root -m 0755 /srv/mc-remote "$RUNTIME_ROOT" "$RUNTIME_ROOT/artifacts"
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 \
    "$RUNTIME_ROOT/data" "$RUNTIME_ROOT/data/plugins" "$RUNTIME_ROOT/data/plugins/McRemote"
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 \
    "$RUNTIME_ROOT/credential-store" "$RUNTIME_ROOT/credential-revocations"
  install -o root -g root -m 0644 "$paper_source" "$RUNTIME_ROOT/artifacts/paper.jar"
  install -o root -g root -m 0644 "$mcremote_source" "$RUNTIME_ROOT/data/plugins/mc-remote.jar"
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0600 \
    "$config_source" "$RUNTIME_ROOT/data/plugins/McRemote/config.yml"
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0640 \
    "$properties_source" "$RUNTIME_ROOT/data/server.properties"
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0640 \
    "$eula_source" "$RUNTIME_ROOT/data/eula.txt"
  write_unit
  systemctl daemon-reload
  verify_mutable_ownership
  verify_runtime_artifacts
  bootstrap_credential_domain
else
  id "$SERVICE_USER" >/dev/null
  verify_mutable_ownership
  verify_runtime_artifacts
fi

verify_restart_lifecycle
verify_runtime_artifacts
printf 'OK host-native dev runtime mode=%s service=active full-readiness=true auth-required=true same-domain-after-normal-restart=true\n' "$mode"
