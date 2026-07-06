# One-time local dev setup (Windows): enable docker-compose.dev.yml via .env
# Run from repo root or bitrix_export_web/: .\scripts\setup-local-env.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$envFile = Join-Path $Root ".env"
$exampleFile = Join-Path $Root ".env.example"

if (-not (Test-Path $envFile)) {
    if (-not (Test-Path $exampleFile)) {
        Write-Error ".env.example not found in $Root"
    }
    Copy-Item $exampleFile $envFile
    Write-Host "[OK] Created .env from .env.example"
}

$lines = Get-Content $envFile -Encoding UTF8
$keys = @{
    "COMPOSE_PATH_SEPARATOR" = ":"
    "COMPOSE_FILE"           = "docker-compose.yml:docker-compose.dev.yml"
    "WEB_PUBLISH_PORT"       = "8000"
}

function Set-EnvKey {
    param([string[]]$Content, [string]$Key, [string]$Value)
    $pattern = "^\s*#?\s*$([regex]::Escape($Key))="
    $newLine = "${Key}=${Value}"
    $found = $false
    $result = @()
    foreach ($line in $Content) {
        if ($line -match $pattern) {
            $result += $newLine
            $found = $true
        } else {
            $result += $line
        }
    }
    if (-not $found) {
        $result += $newLine
    }
    return ,$result
}

foreach ($entry in $keys.GetEnumerator()) {
    $lines = Set-EnvKey -Content $lines -Key $entry.Key -Value $entry.Value
}

$lines | Set-Content $envFile -Encoding UTF8

Write-Host ""
Write-Host "[OK] Local dev .env configured:"
Write-Host "  COMPOSE_PATH_SEPARATOR=:"
Write-Host "  COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml"
Write-Host "  WEB_PUBLISH_PORT=8000"
Write-Host ""
Write-Host "Start stack:  docker compose up --build -d"
Write-Host "Health check: http://localhost:8000/health"
Write-Host "Prod (Linux): docker compose -f docker-compose.yml up --build -d  (port 80)"
