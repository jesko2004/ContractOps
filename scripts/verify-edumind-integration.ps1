$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$required = @(
  'package.json',
  'docker-compose.yml',
  'app',
  'packages',
  'services/model-gateway/Cargo.toml',
  'services/model-gateway/crates/switchyard-server/Cargo.toml',
  'deploy/model-gateway/routes.local.toml',
  'README-EDUMIND.md',
  'architecture/EDUMIND-SCOPE.md'
)

$missing = @($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $root $_)) })
if ($missing.Count -gt 0) {
  throw "EduMind integration is incomplete. Missing: $($missing -join ', ')"
}

$package = Get-Content -Raw -LiteralPath (Join-Path $root 'package.json') | ConvertFrom-Json
if ($package.name -ne 'edumind') {
  throw "Expected package name 'edumind', found '$($package.name)'."
}

$compose = Get-Content -Raw -LiteralPath (Join-Path $root 'docker-compose.yml')
if ($compose -notmatch '(?m)^  model-gateway:$') {
  throw 'docker-compose.yml does not contain the model-gateway service.'
}

Write-Output 'EduMind integration structure: OK'
