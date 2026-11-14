#!/usr/bin/env bash

set -Eeuo pipefail

readonly REPO_URL="https://github.com/jamestesting0/elo-bot.git"
readonly REPO_BRANCH="main"
readonly REPO_USERNAME="jamestesting0"
readonly APP_USER="elobot"
readonly APP_GROUP="elobot"
readonly APP_DIR="/opt/elobot"
readonly VENV_DIR="${APP_DIR}/.venv"
readonly GIT_SECRET_DIR="/etc/elobot"
readonly GIT_TOKEN_PATH="${GIT_SECRET_DIR}/github-token"
readonly GIT_ASKPASS_PATH="/usr/local/libexec/elobot-git-askpass"
readonly GIT_TOKEN_SETTER_PATH="/usr/local/sbin/elobot-set-git-token"
readonly UPDATER_PATH="/usr/local/sbin/elobot-update"
readonly LAUNCHER_PATH="/usr/local/bin/elobot-start"
readonly SERVICE_PATH="/etc/systemd/system/elobot.service"

if [[ ${EUID} -ne 0 ]]; then
    echo "This cloud-init script must run as root." >&2
    exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This script requires an Ubuntu image with apt-get. Use Ubuntu 24.04 on Oracle Cloud." >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install --yes --no-install-recommends \
    ca-certificates \
    git \
    libgl1 \
    python3.12 \
    python3.12-venv

if ! getent group "${APP_GROUP}" >/dev/null; then
    groupadd --system "${APP_GROUP}"
fi

if ! id --user "${APP_USER}" >/dev/null 2>&1; then
    useradd \
        --system \
        --gid "${APP_GROUP}" \
        --home-dir "${APP_DIR}" \
        --shell /usr/sbin/nologin \
        "${APP_USER}"
fi

install --directory --owner root --group "${APP_GROUP}" --mode 0750 "${GIT_SECRET_DIR}"
install --directory --owner root --group root --mode 0755 /usr/local/libexec

if [[ ! -e "${GIT_TOKEN_PATH}" ]]; then
    install --owner root --group "${APP_GROUP}" --mode 0640 /dev/null "${GIT_TOKEN_PATH}"
else
    chown "root:${APP_GROUP}" "${GIT_TOKEN_PATH}"
    chmod 0640 "${GIT_TOKEN_PATH}"
fi

cat >"${GIT_ASKPASS_PATH}" <<'ELOBOT_GIT_ASKPASS'
#!/usr/bin/env bash

set -Eeuo pipefail

case "${1:-}" in
    *Username*) printf '%s\n' "${ELOBOT_GIT_USERNAME:?}" ;;
    *Password*) cat /etc/elobot/github-token ;;
    *) exit 1 ;;
esac
ELOBOT_GIT_ASKPASS
chmod 0755 "${GIT_ASKPASS_PATH}"

cat >"${GIT_TOKEN_SETTER_PATH}" <<'ELOBOT_GIT_TOKEN_SETTER'
#!/usr/bin/env bash

set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this command through sudo." >&2
    exit 1
fi

read -r -s -p "GitHub token: " token
printf '\n'
if [[ -z "${token}" ]]; then
    echo "The token cannot be empty." >&2
    exit 1
fi

temporary_path="$(mktemp /etc/elobot/github-token.XXXXXX)"
trap 'if [[ -e "${temporary_path}" ]]; then unlink "${temporary_path}"; fi' EXIT
printf '%s' "${token}" >"${temporary_path}"
unset token
chown root:elobot "${temporary_path}"
chmod 0640 "${temporary_path}"
mv -f "${temporary_path}" /etc/elobot/github-token
trap - EXIT
echo "GitHub token stored in /etc/elobot/github-token."
ELOBOT_GIT_TOKEN_SETTER
chmod 0755 "${GIT_TOKEN_SETTER_PATH}"

install --directory --owner "${APP_USER}" --group "${APP_GROUP}" "${APP_DIR}"

if [[ -d "${APP_DIR}/.git" ]]; then
    echo "Using the existing Git checkout in ${APP_DIR}."
elif [[ -n "$(find "${APP_DIR}" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    echo "${APP_DIR} is not empty and is not a Git checkout; refusing to overwrite it." >&2
    exit 1
else
    if [[ -s "${GIT_TOKEN_PATH}" ]]; then
        ELOBOT_GIT_USERNAME="${REPO_USERNAME}" \
        GIT_ASKPASS="${GIT_ASKPASS_PATH}" \
        GIT_TERMINAL_PROMPT=0 \
            git clone --branch "${REPO_BRANCH}" --single-branch "${REPO_URL}" "${APP_DIR}"
        chown --recursive "${APP_USER}:${APP_GROUP}" "${APP_DIR}"
    else
        runuser --user "${APP_USER}" -- \
            git clone --branch "${REPO_BRANCH}" --single-branch "${REPO_URL}" "${APP_DIR}"
    fi
fi

if [[ ! -f "${APP_DIR}/requirements.txt" ]]; then
    echo "Missing ${APP_DIR}/requirements.txt after cloning the repository." >&2
    exit 1
fi

if [[ ! -f "${APP_DIR}/config.json" ]]; then
    cat >"${APP_DIR}/config.json" <<'ELOBOT_CONFIG'
{
  "start_file": "main.py",
  "repository_url": "https://github.com/jamestesting0/elo-bot.git",
  "repository_branch": "main",
  "repository_username": "jamestesting0",
  "update_on_start": true
}
ELOBOT_CONFIG
    chown "${APP_USER}:${APP_GROUP}" "${APP_DIR}/config.json"
fi

install --directory --owner "${APP_USER}" --group "${APP_GROUP}" \
    "${APP_DIR}/data" \
    "${APP_DIR}/temp"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    runuser --user "${APP_USER}" -- python3.12 -m venv "${VENV_DIR}"
fi

runuser --user "${APP_USER}" -- "${VENV_DIR}/bin/python" -m pip install --upgrade pip
runuser --user "${APP_USER}" -- "${VENV_DIR}/bin/python" -m pip install \
    --requirement "${APP_DIR}/requirements.txt"
runuser --user "${APP_USER}" -- "${VENV_DIR}/bin/python" -m pip check

cat >"${UPDATER_PATH}" <<'ELOBOT_UPDATE'
#!/usr/bin/env bash

set -Eeuo pipefail

readonly APP_DIR="/opt/elobot"
readonly CONFIG_FILE="${APP_DIR}/config.json"
readonly REQUIREMENTS_FILE="${APP_DIR}/requirements.txt"
readonly REQUIREMENTS_STAMP="${APP_DIR}/.venv/.requirements.sha256"
readonly PYTHON_BIN="${APP_DIR}/.venv/bin/python"
readonly GIT_TOKEN_PATH="/etc/elobot/github-token"
readonly GIT_ASKPASS_PATH="/usr/local/libexec/elobot-git-askpass"

if [[ "$(id --user --name)" != "elobot" ]]; then
    echo "Run this updater as the elobot user." >&2
    exit 1
fi

mapfile -d '' -t settings < <("${PYTHON_BIN}" - "${CONFIG_FILE}" <<'PYTHON'
import json
import re
import sys
from urllib.parse import urlsplit

config_path = sys.argv[1]
try:
    with open(config_path, encoding="utf-8") as config_file:
        config = json.load(config_file)
except (OSError, json.JSONDecodeError) as error:
    raise SystemExit(f"Could not read {config_path}: {error}") from error

if not isinstance(config, dict):
    raise SystemExit(f"{config_path} must contain a JSON object")

repository_url = config.get("repository_url")
repository_branch = config.get("repository_branch")
repository_username = config.get("repository_username")
update_on_start = config.get("update_on_start")

for field_name, value in (
    ("repository_url", repository_url),
    ("repository_branch", repository_branch),
    ("repository_username", repository_username),
):
    if not isinstance(value, str) or not value:
        raise SystemExit(f'{config_path} field "{field_name}" must be a nonempty string')

parsed_url = urlsplit(repository_url)
if parsed_url.scheme != "https" or not parsed_url.hostname:
    raise SystemExit("repository_url must be a valid HTTPS Git URL")
if parsed_url.username is not None or parsed_url.password is not None:
    raise SystemExit("repository_url must not contain embedded credentials")
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", repository_branch):
    raise SystemExit("repository_branch contains unsupported characters")
if ".." in repository_branch.split("/"):
    raise SystemExit("repository_branch contains an invalid path segment")
if not re.fullmatch(r"[A-Za-z0-9-]+", repository_username):
    raise SystemExit("repository_username contains unsupported characters")
if not isinstance(update_on_start, bool):
    raise SystemExit(f'{config_path} field "update_on_start" must be true or false')

for value in (
    repository_url,
    repository_branch,
    repository_username,
    "1" if update_on_start else "0",
):
    sys.stdout.buffer.write(value.encode("utf-8") + b"\0")
PYTHON
)

if (( ${#settings[@]} != 4 )); then
    echo "Could not read Git settings from ${CONFIG_FILE}." >&2
    exit 1
fi

repository_url="${settings[0]}"
repository_branch="${settings[1]}"
repository_username="${settings[2]}"
update_on_start="${settings[3]}"

if [[ "${update_on_start}" != "1" ]]; then
    echo "Git update on start is disabled."
    exit 0
fi

if [[ ! -d "${APP_DIR}/.git" ]]; then
    echo "Missing Git checkout in ${APP_DIR}." >&2
    exit 1
fi

git_command=(env GIT_TERMINAL_PROMPT=0)
if [[ -s "${GIT_TOKEN_PATH}" ]]; then
    git_command+=(
        GIT_ASKPASS="${GIT_ASKPASS_PATH}"
        ELOBOT_GIT_USERNAME="${repository_username}"
    )
fi
git_command+=(git -C "${APP_DIR}")

commit_before="$(git -C "${APP_DIR}" rev-parse --short HEAD)"
echo "[git] Repository: ${repository_url}"
echo "[git] Branch: ${repository_branch}"
echo "[git] Fetching origin/${repository_branch}..."
"${git_command[@]}" remote set-url origin "${repository_url}"
"${git_command[@]}" fetch --prune origin "${repository_branch}"

if git -C "${APP_DIR}" show-ref --verify --quiet "refs/heads/${repository_branch}"; then
    git -C "${APP_DIR}" checkout --quiet "${repository_branch}"
else
    git -C "${APP_DIR}" checkout --quiet --track -b "${repository_branch}" "origin/${repository_branch}"
fi

git -C "${APP_DIR}" merge --ff-only "origin/${repository_branch}"
commit_after="$(git -C "${APP_DIR}" rev-parse --short HEAD)"
requirements_after="$(sha256sum "${REQUIREMENTS_FILE}" | awk '{print $1}')"
requirements_installed=""
if [[ -f "${REQUIREMENTS_STAMP}" ]]; then
    requirements_installed="$(<"${REQUIREMENTS_STAMP}")"
fi

if [[ "${requirements_installed}" != "${requirements_after}" ]]; then
    echo "[dependencies] requirements.txt has not been successfully installed; installing dependencies..."
    "${PYTHON_BIN}" -m pip install --requirement "${REQUIREMENTS_FILE}"
    "${PYTHON_BIN}" -m pip check
    printf '%s\n' "${requirements_after}" > "${REQUIREMENTS_STAMP}"
    echo "[dependencies] Installation and dependency check succeeded."
fi

if [[ "${commit_before}" == "${commit_after}" ]]; then
    echo "[git] Already up to date at ${commit_after}."
else
    echo "[git] Updated ${commit_before} -> ${commit_after}."
fi
ELOBOT_UPDATE
chmod 0755 "${UPDATER_PATH}"

cat >"${LAUNCHER_PATH}" <<'ELOBOT_START'
#!/usr/bin/env bash

set -Eeuo pipefail

readonly APP_DIR="${ELOBOT_APP_DIR:-/opt/elobot}"
readonly CONFIG_FILE="${ELOBOT_CONFIG_FILE:-${APP_DIR}/config.json}"
readonly PYTHON_BIN="${ELOBOT_PYTHON_BIN:-${APP_DIR}/.venv/bin/python}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Python executable not found: ${PYTHON_BIN}" >&2
    exit 1
fi

start_file="$("${PYTHON_BIN}" - "${CONFIG_FILE}" <<'PYTHON'
import json
import sys

config_path = sys.argv[1]
try:
    with open(config_path, encoding="utf-8") as config_file:
        config = json.load(config_file)
except (OSError, json.JSONDecodeError) as error:
    raise SystemExit(f"Could not read {config_path}: {error}") from error

start_file = config.get("start_file")
if not isinstance(start_file, str):
    raise SystemExit(f'{config_path} must contain a string field named "start_file"')

print(start_file)
PYTHON
)"

if [[ ! "${start_file}" =~ ^[[:alnum:]_-]+\.py$ ]]; then
    echo "start_file must name a Python file in ${APP_DIR}, for example main.py or beta.py." >&2
    exit 1
fi

start_path="${APP_DIR}/${start_file}"
if [[ ! -f "${start_path}" ]]; then
    echo "Configured start file does not exist: ${start_path}" >&2
    exit 1
fi

if [[ ${1:-} == "--check" ]]; then
    echo "Configured entrypoint: ${start_path}"
    exit 0
fi

if (( $# != 0 )); then
    echo "Usage: elobot-start [--check]" >&2
    exit 2
fi

exec "${PYTHON_BIN}" "${start_path}"
ELOBOT_START
chmod 0755 "${LAUNCHER_PATH}"

cat >"${SERVICE_PATH}" <<'ELOBOT_SERVICE'
[Unit]
Description=EloBot Discord bot
Wants=network-online.target
After=network-online.target
ConditionPathExists=/opt/elobot/.env

[Service]
Type=simple
User=elobot
Group=elobot
WorkingDirectory=/opt/elobot
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/opt/elobot/.env
ExecStartPre=/usr/local/sbin/elobot-update
ExecStart=/usr/local/bin/elobot-start
TimeoutStartSec=15min
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
ELOBOT_SERVICE

runuser --user "${APP_USER}" -- "${LAUNCHER_PATH}" --check

systemctl daemon-reload
systemctl enable elobot.service

if [[ -f "${APP_DIR}/.env" ]]; then
    systemctl restart elobot.service
else
    echo "EloBot is installed and enabled, but not started because ${APP_DIR}/.env does not exist."
    echo "Create that file with the bot credentials, then run: systemctl start elobot"
fi
