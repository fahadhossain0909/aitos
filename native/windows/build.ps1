$ErrorActionPreference = 'Stop'
function Assert-Exit { if ($LASTEXITCODE -ne 0) { throw 'Build command failed' } }
Push-Location (Join-Path $PSScriptRoot '../..')
try {
  npm --prefix desktop ci; Assert-Exit
  npm --prefix desktop run build; Assert-Exit
  python -m pip install -r native/windows/requirements.txt; Assert-Exit
  python -m PyInstaller --noconfirm --clean --windowed --onedir --name AITOS-Console --paths . --add-data 'desktop/dist;console_dist' native/windows/launcher.py; Assert-Exit
  & 'C:/Program Files (x86)/Inno Setup 6/ISCC.exe' native/windows/installer.iss; Assert-Exit
} finally { Pop-Location }
