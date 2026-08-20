#!/usr/bin/env bash
set -euo pipefail

UV_BOOTSTRAP_VERSION=0.12.3
MINIMUM_COMPOSE_VERSION=2.33.1

mode=check
repair_project=
repair_artifact_store=
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"

usage() {
  cat <<'EOF'
Usage:
  tools/bootstrap-ubuntu-operator.sh --check
  tools/bootstrap-ubuntu-operator.sh --install [--repair-project PATH] [--repair-artifact-store PATH]

--check reports every missing operator prerequisite without changing the host.
--install installs missing Ubuntu packages, pinned uv, Docker Engine/Compose when
absent, and explicitly grants the current trusted sudo administrator direct
Docker access. Re-login is required after group membership changes.
--repair-project is accepted only with --install and only below
$HOME/mc-remote-deployments; it repairs ownership left by historical root execution.
--repair-artifact-store is accepted only with --install and only for
$HOME/.local/share/mc-remote/artifacts; it repairs the same historical ownership drift.
EOF
}

while (($#)); do
  case "$1" in
    --check)
      mode=check
      ;;
    --install)
      mode=install
      ;;
    --repair-project)
      shift
      (($#)) || { echo "--repair-project requires PATH" >&2; exit 2; }
      repair_project=$1
      ;;
    --repair-artifact-store)
      shift
      (($#)) || { echo "--repair-artifact-store requires PATH" >&2; exit 2; }
      repair_artifact_store=$1
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if ((EUID == 0)); then
  echo "FAIL operator bootstrap must run as the trusted login user, not root" >&2
  echo "The script invokes sudo only for package, group, and exact ownership mutations." >&2
  exit 2
fi

if [[ -n "$repair_project" && "$mode" != install ]]; then
  echo "FAIL --repair-project requires explicit --install" >&2
  exit 2
fi
if [[ -n "$repair_artifact_store" && "$mode" != install ]]; then
  echo "FAIL --repair-artifact-store requires explicit --install" >&2
  exit 2
fi

if [[ ! -r /etc/os-release ]]; then
  echo "FAIL /etc/os-release is unavailable" >&2
  exit 2
fi
# shellcheck disable=SC1091
. /etc/os-release
if [[ "${ID:-}" != ubuntu ]]; then
  echo "FAIL this bootstrap supports Ubuntu only; observed ID=${ID:-unknown}" >&2
  exit 2
fi
case "${VERSION_ID:-}" in
  22.04|24.04|26.04) ;;
  *)
    echo "FAIL unsupported Ubuntu VERSION_ID=${VERSION_ID:-unknown}" >&2
    exit 2
    ;;
esac

version_at_least() {
  local actual=$1 minimum=$2
  [[ "$(printf '%s\n%s\n' "$minimum" "$actual" | sort -V | head -n1)" == "$minimum" ]]
}

uv_path() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
  elif [[ -x "$HOME/.local/bin/uv" ]]; then
    printf '%s\n' "$HOME/.local/bin/uv"
  else
    return 1
  fi
}

install_base_packages() {
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl git
}

install_uv() {
  local installer
  installer="$(mktemp)"
  trap 'rm -f -- "$installer"' RETURN
  curl --fail --silent --show-error --location \
    "https://astral.sh/uv/${UV_BOOTSTRAP_VERSION}/install.sh" \
    --output "$installer"
  env UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh "$installer"
  rm -f -- "$installer"
  trap - RETURN
}

install_docker_engine() {
  local codename architecture
  codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
  architecture="$(dpkg --print-architecture)"
  [[ -n "$codename" ]] || { echo "FAIL Ubuntu codename is unavailable" >&2; exit 2; }

  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl --fail --silent --show-error --location \
    https://download.docker.com/linux/ubuntu/gpg \
    --output /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  printf '%s\n' \
    'Types: deb' \
    'URIs: https://download.docker.com/linux/ubuntu' \
    "Suites: $codename" \
    'Components: stable' \
    "Architectures: $architecture" \
    'Signed-By: /etc/apt/keyrings/docker.asc' \
    | sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null
  sudo apt-get update
  sudo apt-get install -y \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo systemctl enable --now docker.service containerd.service
}

repair_project_ownership() {
  local requested resolved allowed_root
  requested=$1
  resolved="$(realpath -e -- "$requested")"
  allowed_root="$(realpath -e -- "$HOME/mc-remote-deployments")"
  if [[ "$resolved" == "$allowed_root" || "$resolved" != "$allowed_root/"* ]]; then
    echo "FAIL repair target must be one project below $allowed_root" >&2
    exit 2
  fi
  if [[ ! -f "$resolved/mc-remote.toml" ]]; then
    echo "FAIL repair target is not a TOML deployment project: $resolved" >&2
    exit 2
  fi
  sudo chown -R "$(id -u):$(id -g)" "$resolved"
  echo "OK repaired project ownership path=$resolved owner=$(id -un)"
}

repair_artifact_store_ownership() {
  local requested resolved allowed
  requested=$1
  resolved="$(realpath -e -- "$requested")"
  allowed="$(realpath -m -- "$HOME/.local/share/mc-remote/artifacts")"
  if [[ "$resolved" != "$allowed" || ! -d "$resolved" ]]; then
    echo "FAIL repair target must be the existing artifact store $allowed" >&2
    exit 2
  fi
  sudo chown -R "$(id -u):$(id -g)" "$resolved"
  echo "OK repaired artifact store ownership path=$resolved owner=$(id -un)"
}

missing=()
for command_name in curl git; do
  command -v "$command_name" >/dev/null 2>&1 || missing+=("$command_name")
done
uv_bin="$(uv_path || true)"
[[ -n "$uv_bin" ]] || missing+=(uv)
command -v docker >/dev/null 2>&1 || missing+=(docker)

if [[ "$mode" == check && ${#missing[@]} -gt 0 ]]; then
  printf 'FAIL operator tools missing:' >&2
  printf ' %s' "${missing[@]}" >&2
  printf '\nRun this same script with --install.\n' >&2
  exit 2
fi

if [[ "$mode" == install ]]; then
  install_base_packages
  if [[ -z "$uv_bin" ]]; then
    install_uv
    uv_bin="$(uv_path)"
  fi
  if ! command -v docker >/dev/null 2>&1; then
    install_docker_engine
  fi
  if [[ -n "$repair_project" ]]; then
    repair_project_ownership "$repair_project"
  fi
  if [[ -n "$repair_artifact_store" ]]; then
    repair_artifact_store_ownership "$repair_artifact_store"
  fi
  uv_bin="$(uv_path)"
  "$uv_bin" python install 3.11
  (
    cd -- "$repo_root"
    PATH="$(dirname -- "$uv_bin"):$PATH" uv sync --extra dev
  )
elif [[ ! -x "$repo_root/.venv/bin/mcrctl" ]]; then
  echo "FAIL repo environment is absent: $repo_root/.venv/bin/mcrctl" >&2
  echo "Run this same script with --install." >&2
  exit 2
fi

uv_bin="$(uv_path)"
git --version >/dev/null
"$uv_bin" --version >/dev/null
if ! "$repo_root/.venv/bin/python" -c \
  'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "FAIL repo environment does not use Python 3.11 or newer" >&2
  echo "Run this same script with --install." >&2
  exit 2
fi

compose_version="$(docker compose version --short 2>/dev/null || true)"
compose_version="${compose_version#v}"
if [[ "$mode" == install ]] && {
  [[ -z "$compose_version" ]] || ! version_at_least "$compose_version" "$MINIMUM_COMPOSE_VERSION"
}; then
  if dpkg-query -W -f='${Status}' docker-ce-cli 2>/dev/null | grep -Fq 'install ok installed'; then
    sudo apt-get update
    sudo apt-get install -y docker-compose-plugin
    compose_version="$(docker compose version --short 2>/dev/null || true)"
    compose_version="${compose_version#v}"
  fi
fi
if [[ -z "$compose_version" ]]; then
  echo "FAIL Docker Compose v2 is unavailable" >&2
  exit 2
fi
if ! version_at_least "$compose_version" "$MINIMUM_COMPOSE_VERSION"; then
  echo "FAIL Docker Compose $compose_version is older than $MINIMUM_COMPOSE_VERSION" >&2
  echo "Upgrade the existing Docker installation from its configured package repository." >&2
  exit 2
fi

if ! id -nG | tr ' ' '\n' | grep -Fxq docker; then
  if [[ "$mode" == check ]]; then
    echo "FAIL operator is not a member of the docker group" >&2
    echo "Run this same script with --install, then log out and back in." >&2
    exit 2
  fi
  sudo groupadd --force docker
  sudo usermod -aG docker "$(id -un)"
  echo "RELOGIN REQUIRED: docker group membership was added for $(id -un)."
  echo "Log out and back in, then rerun this script with --check."
  exit 3
fi

if ! docker --context default version >/dev/null 2>&1; then
  echo "FAIL direct Docker access is unavailable in this login session" >&2
  echo "Log out and back in; do not run the whole CLI as root." >&2
  exit 2
fi

echo "OK operator bootstrap tools=ready docker-access=direct compose=$compose_version"
echo "OK repo environment=$repo_root/.venv"
if [[ -n "$repair_project" ]]; then
  "$repo_root/.venv/bin/mcrctl" operator check \
    --project "$(realpath -e -- "$repair_project")" \
    --docker-context default
fi
