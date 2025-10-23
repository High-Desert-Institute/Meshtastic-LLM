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
    Write-Host "Starting ai-agent.py loop..."
    $agentJob = Start-Job -Name "meshtastic-ai-agent" -ScriptBlock {
        param($pythonPath, $scriptPath, $configPath, $workingDir)
        $ErrorActionPreference = "Stop"
        Set-Location $workingDir
        while ($true) {
            & $pythonPath $scriptPath --config $configPath
            Write-Host "ai-agent.py exited; restarting in 5 seconds..."
            Start-Sleep -Seconds 5
        }
    } -ArgumentList $pythonCmd, $agentScript, $agentConfig, $rootDir

    while ($true) {
        & $pythonCmd $bridgeScript --config $agentConfig
        Write-Host "meshtastic-bridge.py exited; restarting in 5 seconds..."
        Start-Sleep -Seconds 5
    }
}
finally {
    if ($agentJob -ne $null) {
        if ($agentJob.State -eq 'Running') {
            Write-Host "Stopping ai-agent job..."
            Stop-Job -Job $agentJob -ErrorAction SilentlyContinue | Out-Null
        }
        if ($agentJob.State -ne 'Completed') {
            Wait-Job -Job $agentJob -ErrorAction SilentlyContinue | Out-Null
        }
        Receive-Job $agentJob -ErrorAction SilentlyContinue | Out-Null
        Remove-Job $agentJob
    }
}
