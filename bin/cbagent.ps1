$ErrorActionPreference = "Stop"

$scriptPath = $PSCommandPath
if (-not $scriptPath) {
  $scriptPath = $MyInvocation.MyCommand.Path
}

$scriptDir = Split-Path -Parent $scriptPath
$agentRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
$workspace = (Get-Location).ProviderPath
$venvRoot = $null
$siblingVenv = Join-Path (Split-Path -Parent $agentRoot) "venv"
$localVenv = Join-Path $agentRoot "venv"
if (Test-Path (Join-Path $siblingVenv "Scripts/python.exe")) {
  $venvRoot = (Resolve-Path $siblingVenv).Path
} elseif (Test-Path (Join-Path $localVenv "Scripts/python.exe")) {
  $venvRoot = (Resolve-Path $localVenv).Path
}

$env:CBAGENT_APP_ROOT = $agentRoot
$env:CBAGENT_WORKSPACE = $workspace
if ($venvRoot) {
  $env:VIRTUAL_ENV = $venvRoot
  $env:Path = (Join-Path $venvRoot "Scripts") + ";" + $env:Path
}

Set-Location (Join-Path $agentRoot "ui-otui")
& bun "src/entry.tsx" @args
exit $LASTEXITCODE
