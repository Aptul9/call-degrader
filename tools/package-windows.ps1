# Packs the PyInstaller folder build (dist/call-degrader) into CallDegrader-Windows-x64.zip, one top-level call-degrader
# folder inside, and writes next to it CallDegrader-Windows-x64.json {version, sha256, bytes, commit, file}, the file the
# services products page reads, plus SHA256SUMS.txt.
[CmdletBinding()]
param(
    [string]$Build = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\call-degrader'),
    [string]$OutputDirectory = (Join-Path (Split-Path $PSScriptRoot -Parent) '.release\windows')
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$name = 'CallDegrader-Windows-x64'

if (-not (Test-Path (Join-Path $Build 'call-degrader.exe'))) { throw "no call-degrader.exe in $Build; run pyinstaller call-degrader.spec first" }
$version = [regex]::Match((Get-Content (Join-Path $root 'src\__init__.py') -Raw), '(?m)^__version__ = "(\d+\.\d+\.\d+)"').Groups[1].Value
if (-not $version) { throw '__version__ not found in src/__init__.py' }
$commit = git -C $root rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw 'Source commit unavailable.' }

New-Item -ItemType Directory -Force $OutputDirectory | Out-Null
$archive = Join-Path $OutputDirectory "$name.zip"
if (Test-Path $archive) { throw "Archive already exists; use a new output directory: $archive" }

# the folder as the user gets it: the build, the licence and the readme beside the exe
$stage = Join-Path $OutputDirectory ('stage-' + [guid]::NewGuid().ToString('N'))
$top = Join-Path $stage 'call-degrader'
Copy-Item $Build $top -Recurse
foreach ($file in 'README.md', 'LICENSE', 'THIRD-PARTY.md') { Copy-Item (Join-Path $root $file) $top }

Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($top, $archive, [IO.Compression.CompressionLevel]::Optimal, $true)
Remove-Item $stage -Recurse -Force

$hash = (Get-FileHash $archive).Hash.ToLowerInvariant()
$bytes = (Get-Item $archive).Length
[IO.File]::WriteAllText((Join-Path $OutputDirectory 'SHA256SUMS.txt'), "$hash  $name.zip`n", [Text.Encoding]::ASCII)
$meta = [ordered]@{ version = $version; sha256 = $hash; bytes = $bytes; commit = $commit; file = "$name.zip" }
[IO.File]::WriteAllText((Join-Path $OutputDirectory "$name.json"), ($meta | ConvertTo-Json -Compress) + "`n", [Text.UTF8Encoding]::new($false))
$meta | ConvertTo-Json -Compress
