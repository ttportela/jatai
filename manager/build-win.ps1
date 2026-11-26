# Gera o .exe do Jataí Manager e o instalador (Windows).
# Pré-requisitos: Python 3.11+, pip install pyinstaller, Inno Setup 6 (ISCC).
# Uso (PowerShell, a partir da raiz do repo):
#   .venv\Scripts\pyinstaller --noconfirm --clean --windowed --name "jatai_manager" --icon manager\jatai_manager.ico --add-data "manager\logo_64.png;." manager\jatai_manager.py
#   & "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" manager\installer.iss
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

New-Item -ItemType Directory -Force dist-windows | Out-Null

.venv\Scripts\pyinstaller --noconfirm --clean `
    --windowed --name "jatai_manager" `
    --icon manager\jatai_manager.ico `
    --add-data "manager\logo_64.png;." `
    manager\jatai_manager.py

Copy-Item "dist\jatai_manager.exe" "dist-windows\jatai_manager.exe" -Force

$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { $iscc = "$env:ProgramFiles\Inno Setup 6\ISCC.exe" }
if (Test-Path $iscc) {
    & $iscc manager\installer.iss
    Write-Host "Instalador gerado em dist-windows\"
} else {
    Write-Warning "Inno Setup não encontrado — .exe pronto em dist-windows\; instale o Inno Setup 6 para gerar o instalador."
}
