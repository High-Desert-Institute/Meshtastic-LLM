#!/bin/bash

# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh
rm get-docker.sh


# Install Python + dependencies
apt install python3 python3-pip python3-venv
pip install -r requirements.txt

