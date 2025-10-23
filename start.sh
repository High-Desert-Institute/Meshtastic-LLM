#!/bin/bash
set -euo pipefail

if [ -z "${BASH_VERSION:-}" ]; then
	echo "This script must be executed with bash." >&2
	exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root." >&2
    exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VENV_DIR="${VENV_DIR:-${ROOT_DIR}/.venv}"

if [ -z "${PYTHON_BIN:-}" ]; then
	if [ -x "${VENV_DIR}/bin/python" ]; then
		PYTHON_BIN="${VENV_DIR}/bin/python"
	else
		PYTHON_BIN="$(command -v python3 || true)"
	fi
fi

if [ -z "${PYTHON_BIN}" ]; then
	echo "Unable to locate python3. Set PYTHON_BIN or create a virtual environment." >&2
	exit 1
fi

NETWORK_NAME="${DOCKER_NETWORK_NAME:-meshtastic-net}"

if command -v docker >/dev/null 2>&1; then
	DOCKER_AVAILABLE=1
else
	echo "Docker not found; skipping container startup." >&2
	DOCKER_AVAILABLE=0
fi

if [ "${DOCKER_AVAILABLE}" -eq 1 ] && ! docker network ls --format '{{.Name}}' | grep -q "^${NETWORK_NAME}$"; then
	docker network create "${NETWORK_NAME}"
fi

ensure_container_network() {
	local container_name="$1"

	if ! docker ps --format '{{.Names}}' | grep -q "^${container_name}$"; then
		return
	fi

	if ! docker inspect -f '{{json .NetworkSettings.Networks}}' "${container_name}" | grep -q "\"${NETWORK_NAME}\":"; then
		docker network connect "${NETWORK_NAME}" "${container_name}" || true
	fi
}

if [ -f "${ROOT_DIR}/requirements.txt" ]; then
	"${PYTHON_BIN}" -m pip install --upgrade -r "${ROOT_DIR}/requirements.txt"
fi

AGENT_CONFIG="${ROOT_DIR}/config/default.toml"

# Run Ollama container if it is not already up
if [ "${DOCKER_AVAILABLE}" -eq 1 ]; then
	if ! docker ps --format '{{.Names}}' | grep -q '^ollama$'; then
		if docker ps -a --format '{{.Names}}' | grep -q '^ollama$'; then
			docker start ollama >/dev/null
		else
			docker run -d -v ollama:/root/.ollama -p 11434:11434 --network "${NETWORK_NAME}" --restart unless-stopped --name ollama ollama/ollama
		fi
	fi
	ensure_container_network "ollama"
fi

# Run OpenWebUI container if it is not already up
if [ "${DOCKER_AVAILABLE}" -eq 1 ]; then
	if ! docker ps --format '{{.Names}}' | grep -q '^open-webui$'; then
		if docker ps -a --format '{{.Names}}' | grep -q '^open-webui$'; then
			docker start open-webui >/dev/null
		else
			docker run -d -p 3000:8080 -v open-webui:/app/backend/data --network "${NETWORK_NAME}" --restart unless-stopped --name open-webui ghcr.io/open-webui/open-webui:main
		fi
	fi
	ensure_container_network "open-webui"
fi

# Launch the AI agent in the background, ensure cleanup on exit
AGENT_PID=0
start_agent() {
	echo "Starting ai-agent.py..."
	"${PYTHON_BIN}" "${ROOT_DIR}/ai-agent.py" --config "${AGENT_CONFIG}" &
	AGENT_PID=$!
}

stop_agent() {
	if [ "${AGENT_PID}" -ne 0 ]; then
		kill "${AGENT_PID}" 2>/dev/null || true
		wait "${AGENT_PID}" 2>/dev/null || true
	fi
}

start_agent
trap stop_agent EXIT INT TERM

# Launch the Meshtastic bridge for local testing
exec "${PYTHON_BIN}" "${ROOT_DIR}/meshtastic-bridge.py" --config "${AGENT_CONFIG}"