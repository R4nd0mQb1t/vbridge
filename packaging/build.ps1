# Builds the indicator DLL, the service executable and the installer.
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
# Output: dist\VBridge-Setup-<version>.exe
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$version = (Get-Content "$root\service\bridge\version.py" | Select-String 'VERSION = "([^"]+)"').Matches[0].Groups[1].Value
Write-Host "== V Bridge $version"

Write-Host "== indicator"
dotnet build "$root\indicator\VBridge.csproj" -c Release --nologo -v q
if ($LASTEXITCODE -ne 0) { throw "indicator build failed" }

Write-Host "== service (PyInstaller)"
if (Test-Path "$root\dist\VBridgeService") { Remove-Item -Recurse -Force "$root\dist\VBridgeService" }
python -m PyInstaller "$root\packaging\vbridge.spec" --noconfirm --distpath "$root\dist" --workpath "$root\build"
if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }

Write-Host "== installer (Inno Setup)"
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "C:\Program Files (x86)\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "ISCC.exe not found: install Inno Setup 6 (winget install JRSoftware.InnoSetup)" }
& $iscc "$root\packaging\installer.iss" "/DMyAppVersion=$version" /Q
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }

$out = "$root\dist\VBridge-Setup-$version.exe"
Write-Host "== done: $out ($([math]::Round((Get-Item $out).Length / 1MB, 1)) MB)"
