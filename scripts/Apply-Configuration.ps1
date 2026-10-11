[CmdletBinding()]
param(
    [string]$ComposeFile = 'docker-compose-local.yml',
    [string]$Service = 'keycloak'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$resolvedComposeFile = if ([System.IO.Path]::IsPathRooted($ComposeFile)) {
    $ComposeFile
} else {
    Join-Path $repositoryRoot $ComposeFile
}

function Invoke-Compose {
    param([Parameter(Mandatory)][string[]]$Arguments)
    & docker compose -f $resolvedComposeFile @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose failed with exit code $LASTEXITCODE. Configuration may be partially applied; correct the error and rerun."
    }
}

Invoke-Compose -Arguments @('exec', '-T', $Service, 'mkdir', '-p', '/tmp/identity-provider-config')
Invoke-Compose -Arguments @('cp', (Join-Path $repositoryRoot 'config/.'), "${Service}:/tmp/identity-provider-config")
Invoke-Compose -Arguments @('cp', (Join-Path $PSScriptRoot 'apply-config.sh'), "${Service}:/tmp/identity-provider-apply.sh")
Invoke-Compose -Arguments @('exec', '-T', $Service, 'bash', '/tmp/identity-provider-apply.sh', '/tmp/identity-provider-config')
