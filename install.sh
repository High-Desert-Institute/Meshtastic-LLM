#!/bin/bash

# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh
rm get-docker.sh


# Install Python dependencies
pip install meshtastic ollama
pip install -r requirements.txt

