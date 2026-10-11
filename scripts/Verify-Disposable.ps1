[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$KochwikiRepository = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $repositoryRoot '.venv/Scripts/python.exe' }
$Python = (Get-Command $Python -ErrorAction Stop).Source
$composeFile = Join-Path $repositoryRoot 'deployments/docker-compose-test.yml'
$exampleEnv = Join-Path $repositoryRoot 'deployments/.env.example'
$providerArguments = @('compose', '--project-name', 'identity-provider-test', '--env-file', $exampleEnv, '-f', $composeFile)
$kochwikiArguments = @()
$backendStarted = $false
$providerStarted = $false

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Verification command failed with exit code $LASTEXITCODE." }
}

function Wait-Http {
    param([string]$Url)
    $deadline = [DateTime]::UtcNow.AddSeconds(180)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -eq 200) { return }
        } catch {}
        Start-Sleep -Seconds 1
    }
    throw 'A disposable service did not become ready within 180 seconds.'
}

# Fixed project names and Compose paths restrict teardown to test resources.
if ($env:COMPOSE_PROJECT_NAME -or $env:COMPOSE_FILE) {
    throw 'Unset COMPOSE_PROJECT_NAME and COMPOSE_FILE before running disposable verification.'
}
if ($KochwikiRepository) {
    $KochwikiRepository = (Resolve-Path -LiteralPath $KochwikiRepository).Path
    $backendCompose = Join-Path $KochwikiRepository 'deployment/docker-compose-auth-test.yml'
    if (-not (Test-Path -LiteralPath $backendCompose)) { throw 'KochWiki auth test Compose file is missing.' }
    $kochwikiArguments = @('compose', '--project-name', 'kochwiki-auth-test', '--env-file',
        (Join-Path $KochwikiRepository 'deployment/.env.example'), '-f', $backendCompose)
}
try {
    $existing = & docker @providerArguments ps -a -q
    if ($LASTEXITCODE -ne 0 -or $existing) { throw 'Disposable provider project already exists; inspect it before rerunning.' }
    if ($KochwikiRepository) {
        $existingBackend = & docker @kochwikiArguments ps -a -q
        if ($LASTEXITCODE -ne 0 -or $existingBackend) { throw 'Disposable KochWiki project already exists; inspect it before rerunning.' }
    }
    $volumes = & docker volume ls --format '{{.Name}}'
    if ($LASTEXITCODE -ne 0 -or $volumes -contains 'identity-provider-test_test-postgres-data') {
        throw 'Disposable provider database already exists; inspect and clean up the test project before rerunning.'
    }
    $providerStarted = $true
    Invoke-Checked docker ($providerArguments + @('up', '-d'))
    Wait-Http 'http://localhost:8016/realms/master/.well-known/openid-configuration'
    & (Join-Path $PSScriptRoot 'Apply-Configuration.ps1') -ComposeFile $composeFile -EnvFile $exampleEnv
    if ($LASTEXITCODE -ne 0) { throw 'Disposable configuration apply failed.' }
    foreach ($test in @('verify_local.py', 'verify_oidc.py', 'verify_persistence.py')) {
        $testArguments = @('-u', (Join-Path $repositoryRoot "tests/$test"), '--base-url',
            'http://localhost:8016', '--compose-file', $composeFile)
        if ($test -eq 'verify_persistence.py') { $testArguments += '--allow-recreate' }
        else { $testArguments += '--allow-user-creation' }
        Invoke-Checked $Python $testArguments
    }
    if ($KochwikiRepository) {
        $backendStarted = $true
        Invoke-Checked docker ($kochwikiArguments + @('up', '-d', '--build'))
        Wait-Http 'http://localhost:8012/api/meta/version'
        Invoke-Checked $Python @('-u', (Join-Path $KochwikiRepository 'backend/tests/verify_oidc_login.py'),
            '--base-url', 'http://localhost:8012', '--provider-url', 'http://localhost:8016',
            '--allow-user-creation')
    }
    Write-Host 'PASS: disposable verification completed'
} finally {
    $cleanupFailed = $false
    if ($backendStarted) {
        & docker @kochwikiArguments down --volumes
        if ($LASTEXITCODE -ne 0) { $cleanupFailed = $true }
    }
    if ($providerStarted) {
        & docker @providerArguments down --volumes
        if ($LASTEXITCODE -ne 0) { $cleanupFailed = $true }
    }
    if ($cleanupFailed) { throw 'Disposable cleanup failed; inspect the test projects. Persistent local projects were not targeted.' }
}
