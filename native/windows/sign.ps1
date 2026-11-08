# Run ONLY on the protected Windows signing runner with an installed cert/HSM.
$ErrorActionPreference = 'Stop'
$thumb = $env:AITOS_SIGNING_CERT_SHA1
if ($thumb -notmatch '^[A-Fa-f0-9]{40}$') { throw 'Expected signer thumbprint required' }
$sign = (Get-Command signtool.exe -ErrorAction Stop).Source
function Sign-And-Verify([string]$file) {
  & $sign sign /sha1 $thumb /s My /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $file
  if ($LASTEXITCODE -ne 0) { throw 'Signing failed' }
  $result=Get-AuthenticodeSignature $file
  if ($result.Status -ne 'Valid' -or $result.SignerCertificate.Thumbprint -ne $thumb) { throw 'Unexpected or invalid signature' }
}
# Sign the executable BEFORE compiling/signing its installer.
Sign-And-Verify 'dist/AITOS-Console/AITOS-Console.exe'
$command = '"' + $sign + '" sign /sha1 ' + $thumb + ' /s My /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $f'
& 'C:/Program Files (x86)/Inno Setup 6/ISCC.exe' /DSignedRelease=1 "/Saitos=$command" native/windows/installer.iss
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed' }
$installers=@(Get-ChildItem artifacts/windows/*.exe)
if ($installers.Count -ne 1) { throw 'Exactly one installer expected' }
Sign-And-Verify $installers[0].FullName
Get-FileHash $installers[0].FullName -Algorithm SHA256 | Format-List | Out-File artifacts/windows/SHA256.txt
