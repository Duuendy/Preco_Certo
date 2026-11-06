$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupDir = Join-Path (Get-Location) "backups/$stamp"
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
# Pause the application so records and photos form one consistent backup.
docker compose stop worker app
if ($LASTEXITCODE -ne 0) { throw 'Não foi possível pausar a aplicação.' }
try {
    docker compose exec -T db pg_dump -U preco -d preco -Fc -f /tmp/preco-certo.dump
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao copiar o banco.' }
    docker compose cp db:/tmp/preco-certo.dump "$backupDir/database.dump"
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao baixar a cópia do banco.' }
    docker compose cp app:/data/photos "$backupDir/photos"
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao copiar as fotos.' }
} finally {
    docker compose start app worker
}
Write-Host "Backup criado em $backupDir. Copie essa pasta para outro disco ou serviço de armazenamento."
