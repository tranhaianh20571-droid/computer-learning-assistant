param(
    [string]$AdminUser = 'postgres',
    [string]$PgBin = (Join-Path $env:ProgramFiles 'PostgreSQL/17/bin')
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$pythonPath = Join-Path $projectRoot 'server/.venv/Scripts/python.exe'
$psqlPath = Join-Path $PgBin 'psql.exe'
if (-not (Test-Path -LiteralPath $pythonPath) -or -not (Test-Path -LiteralPath $psqlPath)) {
    throw 'Project Python or PostgreSQL client is unavailable.'
}
$metadataScript = @'
import json,sys
from pathlib import Path
from sqlalchemy.engine import make_url
sys.path.insert(0,str(Path(sys.argv[1])/"server/backend"))
from app.logging_audit.db import get_database_url
url=make_url(get_database_url())
if url.get_backend_name()!="postgresql":
    raise SystemExit("PostgreSQL is required.")
print(json.dumps({"host":url.host,"port":url.port or 5432,"database":url.database}))
'@
$metadataJson = & $pythonPath -c $metadataScript $projectRoot
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve the application database.' }
$target = $metadataJson | ConvertFrom-Json
Write-Host ('Enable vector in {0}:{1}/{2} as {3}.' -f $target.host, $target.port, $target.database, $AdminUser)
Write-Host 'The PostgreSQL client will request the database administrator password locally.'
& $psqlPath -X -W -h $target.host -p ([string]$target.port) -U $AdminUser -d $target.database `
    -v ON_ERROR_STOP=1 -c 'CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public; SELECT extversion FROM pg_extension WHERE extname=''vector'';'
if ($LASTEXITCODE -ne 0) { throw 'Extension activation failed; no password was stored.' }
