$ErrorActionPreference = "Stop"

$rootDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

if (Test-Path (Join-Path $rootDir 'requirements.txt')) {
    & python -m pip install --upgrade -r (Join-Path $rootDir 'requirements.txt')
}

$ollama = docker ps --format "{{.Names}}" | Where-Object { $_ -eq 'ollama' }
if (-not $ollama) {
    & docker run -d -v ollama:/root/.ollama -p 11434:11434 --name ollama ollama/ollama | Out-Host
}

$openWebUi = docker ps --format "{{.Names}}" | Where-Object { $_ -eq 'open-webui' }
if (-not $openWebUi) {
    & docker run -d -p 3000:8080 -v open-webui:/app/backend/data --name open-webui ghcr.io/open-webui/open-webui:main | Out-Host
}

& python (Join-Path $rootDir 'meshtastic-bridge.py') --config (Join-Path $rootDir 'config\default.toml')
