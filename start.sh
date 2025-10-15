#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "${ROOT_DIR}/requirements.txt" ]; then
	python3 -m pip install --upgrade -r "${ROOT_DIR}/requirements.txt"
fi

# Run Ollama container if it is not already up
if ! docker ps --format '{{.Names}}' | grep -q '^ollama$'; then
	docker run -d -v ollama:/root/.ollama -p 11434:11434 --name ollama ollama/ollama
fi

# Run OpenWebUI container if it is not already up
if ! docker ps --format '{{.Names}}' | grep -q '^open-webui$'; then
	docker run -d -p 3000:8080 -v open-webui:/app/backend/data --name open-webui ghcr.io/open-webui/open-webui:main
fi

# Launch the Meshtastic bridge for local testing
python -m pip install --upgrade -r requirements.txt
exec python3 "${ROOT_DIR}/meshtastic-bridge.py" --config "${ROOT_DIR}/config/default.toml"