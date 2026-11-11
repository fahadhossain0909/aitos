# Temporary synthetic installation ONLY. No running paper/live instance is touched.
param([switch]$RequireSigned)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$setups = @(Get-ChildItem (Join-Path $root 'artifacts/windows/*.exe'))
if ($setups.Count -ne 1) { throw 'Exactly one installer required' }
$thumb=$env:AITOS_SIGNING_CERT_SHA1
if ($RequireSigned -and $thumb -notmatch '^[A-Fa-f0-9]{40}$') { throw 'Expected signer required' }
function Verify-Signer([string]$path) {
  $s=Get-AuthenticodeSignature $path
  if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Thumbprint -ne $thumb) { throw 'Unexpected or invalid signature' }
}
function Run-Checked([string]$file,[string[]]$arguments) {
  $p=Start-Process $file -ArgumentList $arguments -PassThru
  if (!$p.WaitForExit(120000)) { Stop-Process -Id $p.Id -Force; throw 'Owned test process timed out' }
  if ($p.ExitCode -ne 0) { throw ('Installed test failed: ' + (Split-Path $file -Leaf) + ' ' + ($arguments -join ' ')) }
}
if (!$env:RUNNER_TEMP -or !(Test-Path $env:RUNNER_TEMP -PathType Container)) { throw 'CI temporary directory required' }
$destination=Join-Path $env:RUNNER_TEMP ('AITOS-test-'+[guid]::NewGuid().ToString('N'))
if (Test-Path $destination) { throw 'Fresh synthetic install directory required' }
if ($RequireSigned) { Verify-Signer $setups[0].FullName }
$installed=$false
try {
  Run-Checked $setups[0].FullName @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',('/DIR="'+$destination+'"'))
  $installed=$true
  foreach ($name in @('AITOS-Console.exe','unins000.exe')) {
    $file=Join-Path $destination $name
    if (!(Test-Path $file -PathType Leaf)) { throw 'Installed binary missing' }
    if ($RequireSigned) { Verify-Signer $file }
  }
  Run-Checked (Join-Path $destination 'AITOS-Console.exe') @('--self-test')
  Run-Checked (Join-Path $destination 'AITOS-Console.exe') @('--gateway-self-test')
  $evidence=@{
    schema_version=1; status='passed'; source_commit=$env:GITHUB_SHA
    verification_uses_synthetic_data=$true; signature_checked=[bool]$RequireSigned
    installer_sha256=(Get-FileHash $setups[0].FullName -Algorithm SHA256).Hash
    live_authorized=$false; production_certified=$false
  }
} finally {
  $uninstaller=Join-Path $destination 'unins000.exe'
  if ($installed -and (Test-Path $uninstaller -PathType Leaf)) {
    if ($RequireSigned) { Verify-Signer $uninstaller }
    Run-Checked $uninstaller @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART')
    if (Test-Path (Join-Path $destination 'AITOS-Console.exe')) { throw 'Uninstall retained executable' }
  }
}
# Written only after all checks, including uninstall, complete successfully.
$evidence | ConvertTo-Json | Set-Content (Join-Path $root 'artifacts/windows/install-evidence.json')
