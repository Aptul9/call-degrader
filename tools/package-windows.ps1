# Copies the PyInstaller onefile build (dist/call-degrader.exe) to CallDegrader-Windows-x64.exe and writes next to it
# CallDegrader-Windows-x64.json {version, sha256, bytes, commit, file}, the file the services products page reads, plus
# SHA256SUMS.txt. One exe and no zip: a folder unzipped from a download carries the Mark of the Web on every file, and
# .NET then refuses to load the pythonnet bridge pywebview needs for its window.
[CmdletBinding()]
param(
    [string]$Build = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\call-degrader.exe'),
    [string]$OutputDirectory = (Join-Path (Split-Path $PSScriptRoot -Parent) '.release\windows')
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$name = 'CallDegrader-Windows-x64'

if (-not (Test-Path $Build)) { throw "no $Build; run pyinstaller call-degrader.spec first" }
$version = [regex]::Match((Get-Content (Join-Path $root 'src\__init__.py') -Raw), '(?m)^__version__ = "(\d+\.\d+\.\d+)"').Groups[1].Value
if (-not $version) { throw '__version__ not found in src/__init__.py' }
$commit = git -C $root rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw 'Source commit unavailable.' }

New-Item -ItemType Directory -Force $OutputDirectory | Out-Null
$target = Join-Path $OutputDirectory "$name.exe"
if (Test-Path $target) { throw "Package already exists; use a new output directory: $target" }
Copy-Item $Build $target

$hash = (Get-FileHash $target).Hash.ToLowerInvariant()
$bytes = (Get-Item $target).Length
[IO.File]::WriteAllText((Join-Path $OutputDirectory 'SHA256SUMS.txt'), "$hash  $name.exe`n", [Text.Encoding]::ASCII)
$meta = [ordered]@{ version = $version; sha256 = $hash; bytes = $bytes; commit = $commit; file = "$name.exe" }
[IO.File]::WriteAllText((Join-Path $OutputDirectory "$name.json"), ($meta | ConvertTo-Json -Compress) + "`n", [Text.UTF8Encoding]::new($false))
$meta | ConvertTo-Json -Compress
