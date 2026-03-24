#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Connection: set SSH_DEST directly (e.g. ubuntu@13.112.35.6), or set EC2_HOST/EC2_USER.
SSH_DEST="${SSH_DEST:-}"
EC2_HOST="${EC2_HOST:-}"
EC2_USER="${EC2_USER:-ubuntu}"
if [[ -z "${SSH_DEST}" ]]; then
  if [[ -n "${EC2_HOST}" ]]; then
    SSH_DEST="${EC2_USER}@${EC2_HOST}"
  else
    SSH_DEST="AWS_VPS"
  fi
fi

# Optional SSH key path. If empty, ssh/scp use your ~/.ssh/config rules.
SSH_KEY="${SSH_KEY:-${PROJECT_DIR}/SuperScaner.pem}"

DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
REPO_SSH_URL="${REPO_SSH_URL:-git@github.com:TK88101/Super-Scaner.git}"
REMOTE_APP_DIR="${REMOTE_APP_DIR:-/home/ubuntu/apps/super-scaner}"
REMOTE_SECRETS_DIR="${REMOTE_SECRETS_DIR:-/home/ubuntu/super-scaner-secrets}"
DOCKER_IMAGE="${DOCKER_IMAGE:-super-scaner:prod}"
DOCKER_CONTAINER="${DOCKER_CONTAINER:-scan-bot}"
DOCKER_CPUS="${DOCKER_CPUS:-0.9}"
DOCKER_MEMORY="${DOCKER_MEMORY:-768m}"
DOCKER_MEMORY_SWAP="${DOCKER_MEMORY_SWAP:-4g}"

LOCAL_ENV_FILE="${LOCAL_ENV_FILE:-${PROJECT_DIR}/.env}"
LOCAL_SERVICE_ACCOUNT_FILE="${LOCAL_SERVICE_ACCOUNT_FILE:-${PROJECT_DIR}/service_account.json}"

log() {
  printf "\n[%s] %s\n" "$(date +"%Y-%m-%d %H:%M:%S")" "$*"
}

die() {
  printf "\n[ERROR] %s\n" "$*" >&2
  exit 1
}

[[ -f "${LOCAL_ENV_FILE}" ]] || die "Missing ${LOCAL_ENV_FILE}"
[[ -f "${LOCAL_SERVICE_ACCOUNT_FILE}" ]] || die "Missing ${LOCAL_SERVICE_ACCOUNT_FILE}"

ssh_opts=(-o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30)
if [[ -n "${SSH_KEY}" ]]; then
  [[ -f "${SSH_KEY}" ]] || die "SSH key not found: ${SSH_KEY}"
  chmod 400 "${SSH_KEY}"
  ssh_opts+=(-i "${SSH_KEY}")
fi

ssh_cmd=(ssh "${ssh_opts[@]}")
scp_cmd=(scp "${ssh_opts[@]}")

run_ssh() {
  "${ssh_cmd[@]}" "${SSH_DEST}" "$1"
}

log "Checking SSH connectivity (${SSH_DEST})"
run_ssh "echo 'SSH OK' && uname -a && whoami"

log "Installing git + docker on EC2"
run_ssh "set -euo pipefail; sudo apt-get update; sudo apt-get install -y git docker.io; sudo systemctl enable --now docker; sudo usermod -aG docker \$USER || true"

log "Preparing GitHub deploy key on EC2"
PUBKEY="$(run_ssh "set -euo pipefail; mkdir -p ~/.ssh; chmod 700 ~/.ssh; if [ ! -f ~/.ssh/id_ed25519 ]; then ssh-keygen -t ed25519 -C 'ec2-super-scaner' -f ~/.ssh/id_ed25519 -N ''; fi; cat ~/.ssh/id_ed25519.pub")"
printf "\n=== Add this key to GitHub Deploy Keys (Read-only) ===\n%s\n==============================================\n" "${PUBKEY}"

GITHUB_AUTH_OUTPUT="$(run_ssh "ssh -o StrictHostKeyChecking=accept-new -T git@github.com 2>&1 || true")"
if ! grep -qi "successfully authenticated" <<<"${GITHUB_AUTH_OUTPUT}"; then
  printf "\nGitHub SSH test output:\n%s\n" "${GITHUB_AUTH_OUTPUT}"
  die "GitHub deploy key is not active yet. Add the key shown above to repo Deploy Keys, then rerun."
fi

log "Cloning/updating branch ${DEPLOY_BRANCH}"
run_ssh "set -euo pipefail; mkdir -p \"$(dirname "${REMOTE_APP_DIR}")\"; if [ -d \"${REMOTE_APP_DIR}/.git\" ]; then cd \"${REMOTE_APP_DIR}\"; git fetch origin; git checkout \"${DEPLOY_BRANCH}\"; git pull --ff-only origin \"${DEPLOY_BRANCH}\"; else git clone -b \"${DEPLOY_BRANCH}\" \"${REPO_SSH_URL}\" \"${REMOTE_APP_DIR}\"; fi"

log "Creating remote secrets directory"
run_ssh "set -euo pipefail; mkdir -p \"${REMOTE_SECRETS_DIR}\"; chmod 700 \"${REMOTE_SECRETS_DIR}\""

log "Uploading .env and service_account.json only"
"${scp_cmd[@]}" "${LOCAL_ENV_FILE}" "${SSH_DEST}:${REMOTE_SECRETS_DIR}/.env"
"${scp_cmd[@]}" "${LOCAL_SERVICE_ACCOUNT_FILE}" "${SSH_DEST}:${REMOTE_SECRETS_DIR}/service_account.json"

log "Fixing secrets permissions and SERVICE_ACCOUNT_FILE"
run_ssh "set -euo pipefail; chmod 600 \"${REMOTE_SECRETS_DIR}/.env\" \"${REMOTE_SECRETS_DIR}/service_account.json\"; if grep -q '^SERVICE_ACCOUNT_FILE=' \"${REMOTE_SECRETS_DIR}/.env\"; then sed -i 's|^SERVICE_ACCOUNT_FILE=.*|SERVICE_ACCOUNT_FILE=service_account.json|' \"${REMOTE_SECRETS_DIR}/.env\"; else echo 'SERVICE_ACCOUNT_FILE=service_account.json' >> \"${REMOTE_SECRETS_DIR}/.env\"; fi; grep '^SERVICE_ACCOUNT_FILE=' \"${REMOTE_SECRETS_DIR}/.env\""

log "Building image and starting container"
run_ssh "set -euo pipefail; cd \"${REMOTE_APP_DIR}\"; sudo docker build -t \"${DOCKER_IMAGE}\" .; sudo docker rm -f \"${DOCKER_CONTAINER}\" >/dev/null 2>&1 || true; sudo docker run -d --name \"${DOCKER_CONTAINER}\" --restart unless-stopped --cpus \"${DOCKER_CPUS}\" --memory \"${DOCKER_MEMORY}\" --memory-swap \"${DOCKER_MEMORY_SWAP}\" --env-file \"${REMOTE_SECRETS_DIR}/.env\" -v \"${REMOTE_SECRETS_DIR}/service_account.json:/app/service_account.json:ro\" \"${DOCKER_IMAGE}\""

log "Validating container and logs"
run_ssh "sudo docker ps --filter name=${DOCKER_CONTAINER}"
run_ssh "sudo docker logs --tail 100 ${DOCKER_CONTAINER}"

log "Checking image layer for secrets"
run_ssh "sudo docker run --rm --entrypoint sh \"${DOCKER_IMAGE}\" -c 'ls -1 /app | grep -E \"SuperScaner\\.pem|\\.env|service_account\\.json\" && exit 1 || echo \"No secrets in image layer\"'"

log "Installing daily backup cron"
run_ssh "set -euo pipefail; cd \"${REMOTE_APP_DIR}\"; chmod +x scripts/install_daily_cron.sh; bash scripts/install_daily_cron.sh || true"

log "Deployment finished"
printf "Container: %s\nImage: %s\nHost: %s\n" "${DOCKER_CONTAINER}" "${DOCKER_IMAGE}" "${SSH_DEST}"
