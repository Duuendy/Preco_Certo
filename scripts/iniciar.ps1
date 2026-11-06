$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Test-Path -LiteralPath '.env')) {
    $randomBytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($randomBytes)
    $password = [Convert]::ToHexString($randomBytes).ToLowerInvariant()
    Set-Content -LiteralPath '.env' -Value "DB_PASSWORD=$password" -Encoding utf8
}
docker compose up -d --build --wait
if ($LASTEXITCODE -ne 0) { throw 'Não foi possível iniciar. Confira o Docker Desktop e execute novamente.' }
Write-Host 'Preço Certo disponível em http://localhost:8088'
