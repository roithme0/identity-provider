[CmdletBinding()]
param(
    [string]$ComposeFile = 'deployments/docker-compose-local.yml',
    [string]$Service = 'keycloak',
    [string]$EnvFile = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$resolvedComposeFile = if ([System.IO.Path]::IsPathRooted($ComposeFile)) {
    $ComposeFile
} else {
    Join-Path $repositoryRoot $ComposeFile
}

$deploymentEnvFile = if ($EnvFile) {
    if ([System.IO.Path]::IsPathRooted($EnvFile)) { $EnvFile } else { Join-Path $repositoryRoot $EnvFile }
} else {
    Join-Path (Split-Path -Parent $resolvedComposeFile) '.env'
}
if (-not (Test-Path -LiteralPath $deploymentEnvFile)) {
    throw "Environment file not found: $deploymentEnvFile"
}
$composeArguments = @('-f', $resolvedComposeFile, '--env-file', $deploymentEnvFile)

function Invoke-Compose {
    param([Parameter(Mandatory)][string[]]$Arguments)
    & docker compose @composeArguments @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose failed with exit code $LASTEXITCODE. Configuration may be partially applied; correct the error and rerun."
    }
}

function Get-ValidatedUrls {
    param([string]$Value, [string]$Label, [bool]$AllowLocalHttp)
    $urls = @($Value.Trim() -split '\s+' | Where-Object { $_ } | Select-Object -Unique)
    if ($urls.Count -eq 0) { throw "$Label requires at least one URL." }
    foreach ($url in $urls) {
        $parsed = $null
        if (-not [Uri]::TryCreate($url, [UriKind]::Absolute, [ref]$parsed)) {
            throw "$Label must contain absolute URLs."
        }
        $localHttp = $AllowLocalHttp -and $parsed.Scheme -eq 'http' -and $parsed.Host -in @('localhost', '127.0.0.1')
        if (($parsed.Scheme -ne 'https' -and -not $localHttp) -or $parsed.UserInfo -or
            $parsed.Query -or $parsed.Fragment -or $url.Contains('*')) {
            throw "$Label requires exact HTTPS URLs, or local HTTP URLs in the local deployment, without credentials, queries, fragments or wildcards."
        }
    }
    return $urls
}

$configurationText = & docker compose @composeArguments config --format json 2>$null
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve Compose configuration; check the selected environment file.' }
$configuration = ($configurationText -join [char]10) | ConvertFrom-Json
$settings = $configuration.'x-kochwiki'
$localCallback = $settings.PSObject.Properties['test-callback-url']
$allowLocalHttp = $null -ne $localCallback
$secret = [string]$settings.'client-secret'
if ($secret -eq 'xxxx' -or $secret.Length -lt 32 -or $secret -match '\s') {
    throw 'KOCHWIKI_CLIENT_SECRET must be a random secret of at least 32 characters without whitespace; replace the example placeholder.'
}
$redirects = @(Get-ValidatedUrls -Value $settings.'redirect-uris' -Label 'KOCHWIKI_REDIRECT_URIS' -AllowLocalHttp $allowLocalHttp)
$logoutRedirects = @(Get-ValidatedUrls -Value $settings.'logout-redirect-uris' -Label 'KOCHWIKI_LOGOUT_REDIRECT_URIS' -AllowLocalHttp $allowLocalHttp)
if ($localCallback) {
    $testUrls = @(Get-ValidatedUrls -Value $localCallback.Value -Label 'KOCHWIKI_TEST_CALLBACK_URL' -AllowLocalHttp $true)
    if ($testUrls.Count -ne 1 -or ([Uri]$testUrls[0]).Host -notin @('localhost', '127.0.0.1')) {
        throw 'KOCHWIKI_TEST_CALLBACK_URL must be one loopback URL.'
    }
    $redirects = @($redirects + $testUrls | Select-Object -Unique)
}
$client = Get-Content -Raw -LiteralPath (Join-Path $repositoryRoot 'config/kochwiki/kochwiki-client.json') | ConvertFrom-Json
$client | Add-Member -NotePropertyName secret -NotePropertyValue $secret
$client | Add-Member -NotePropertyName redirectUris -NotePropertyValue $redirects
$client.attributes | Add-Member -NotePropertyName 'post.logout.redirect.uris' -NotePropertyValue ($logoutRedirects -join '##')
$clientJson = $client | ConvertTo-Json -Depth 12 -Compress

Invoke-Compose -Arguments @('exec', '-T', $Service, 'mkdir', '-p', '/tmp/identity-provider-config')
Invoke-Compose -Arguments @('cp', (Join-Path $repositoryRoot 'config/.'), "${Service}:/tmp/identity-provider-config")
Invoke-Compose -Arguments @('cp', (Join-Path $PSScriptRoot 'apply-config.sh'), "${Service}:/tmp/identity-provider-apply.sh")
$clientJson | & docker compose @composeArguments exec -T $Service bash /tmp/identity-provider-apply.sh /tmp/identity-provider-config
if ($LASTEXITCODE -ne 0) {
    throw 'Configuration apply failed. Configuration may be partially applied; correct the error and rerun.'
}
