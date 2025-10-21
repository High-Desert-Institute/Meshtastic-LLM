#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NETWORK_NAME="${DOCKER_NETWORK_NAME:-meshtastic-net}"

if ! docker network ls --format '{{.Name}}' | grep -q "^${NETWORK_NAME}$"; then
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
	python3 -m pip install --upgrade -r "${ROOT_DIR}/requirements.txt"
fi

# Run Ollama container if it is not already up
if ! docker ps --format '{{.Names}}' | grep -q '^ollama$'; then
	docker run -d -v ollama:/root/.ollama -p 11434:11434 --network "${NETWORK_NAME}" --name ollama ollama/ollama
fi
ensure_container_network "ollama"

# Run OpenWebUI container if it is not already up
if ! docker ps --format '{{.Names}}' | grep -q '^open-webui$'; then
	docker run -d -p 3000:8080 -v open-webui:/app/backend/data --network "${NETWORK_NAME}" --name open-webui ghcr.io/open-webui/open-webui:main
fi
ensure_container_network "open-webui"

# Launch the Meshtastic bridge for local testing
python -m pip install --upgrade -r requirements.txt
exec python3 "${ROOT_DIR}/meshtastic-bridge.py" --config "${ROOT_DIR}/config/default.toml"