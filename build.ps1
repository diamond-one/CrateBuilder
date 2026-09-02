$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$buildPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $buildPython)) { $buildPython = 'python' }
& $buildPython -m PyInstaller --noconfirm --clean --onefile --windowed --name CrateBuilder --collect-all customtkinter --collect-all sounddevice --add-data 'cratebuilder/assets;cratebuilder/assets' --add-data 'licenses;licenses' --exclude-module matplotlib --exclude-module pandas --exclude-module librosa --exclude-module torch --exclude-module IPython --exclude-module pytest run.py
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
Write-Output 'Built dist\CrateBuilder.exe'
