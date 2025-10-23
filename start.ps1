$ErrorActionPreference = "Stop"

$rootDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

if (Test-Path (Join-Path $rootDir 'requirements.txt')) {
    & python -m pip install --upgrade -r (Join-Path $rootDir 'requirements.txt')
}

$pythonCmd = 'python'
$agentScript = Join-Path $rootDir 'ai-agent.py'
$agentConfig = Join-Path $rootDir 'config\default.toml'
$bridgeScript = Join-Path $rootDir 'meshtastic-bridge.py'

$dockerAvailable = $false
try {
    $null = Get-Command -Name docker -ErrorAction Stop
    $dockerAvailable = $true
} catch {
    Write-Warning "Docker not found; skipping container startup."
}

if ($dockerAvailable) {
    $ollama = docker ps --format "{{.Names}}" | Where-Object { $_ -eq 'ollama' }
    if (-not $ollama) {
        & docker run -d -v ollama:/root/.ollama -p 11434:11434 --name ollama ollama/ollama | Out-Host
    }

    $openWebUi = docker ps --format "{{.Names}}" | Where-Object { $_ -eq 'open-webui' }
    if (-not $openWebUi) {
        & docker run -d -p 3000:8080 -v open-webui:/app/backend/data --name open-webui ghcr.io/open-webui/open-webui:main | Out-Host
    }
}

$agentJob = $null
try {
    Write-Host "Starting ai-agent.py..."
    $agentJob = Start-Job -Name "meshtastic-ai-agent" -ScriptBlock {
        param($pythonPath, $scriptPath, $configPath, $workingDir)
        Set-Location $workingDir
        & $pythonPath $scriptPath --config $configPath
    } -ArgumentList $pythonCmd, $agentScript, $agentConfig, $rootDir

    & $pythonCmd $bridgeScript --config $agentConfig
}
finally {
    if ($agentJob -ne $null) {
        if ($agentJob.State -eq 'Running') {
            Write-Host "Stopping ai-agent job..."
            Stop-Job $agentJob -Force | Out-Null
        }
        Receive-Job $agentJob | Out-Null
        Remove-Job $agentJob
    }
}
