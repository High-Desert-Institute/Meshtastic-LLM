#!/bin/bash

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "This installer must be run as root." >&2
    exit 1
fi

read -r -p "Install Docker? [y/N]: " INSTALL_DOCKER
if [ "$INSTALL_DOCKER" = "y" ] || [ "$INSTALL_DOCKER" = "Y" ] || [ "$INSTALL_DOCKER" = "yes" ] || [ "$INSTALL_DOCKER" = "YES" ]; then
    curl -fsSL https://get.docker.com -o get-docker.sh
    sh get-docker.sh
    rm get-docker.sh
else
    echo "Skipping Docker installation."
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-.venv}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Error: $PYTHON_BIN is required but not installed." >&2
    echo "Install Python 3 (e.g. 'sudo apt install python3 python3-venv') and rerun this script." >&2
    exit 1
fi

if [ ! -d "$VENV_DIR" ]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/pip" install -r requirements.txt

