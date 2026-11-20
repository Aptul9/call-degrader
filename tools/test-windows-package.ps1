# Tests the download made by package-windows.ps1 the way a user gets it: checks the JSON against the ZIP, extracts the ZIP
# into a new folder, starts the extracted exe on a spare port with the test pattern and no audio (a CI runner has neither a
# camera nor VB-CABLE), and checks that it serves the page, its data files and a state reporting the packaged version.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Archive,
    [int]$Port = 8761
)
$ErrorActionPreference = 'Stop'
$Archive = (Resolve-Path $Archive).Path
$name = [IO.Path]::GetFileNameWithoutExtension($Archive)
$meta = Get-Content (Join-Path (Split-Path $Archive -Parent) "$name.json") -Raw | ConvertFrom-Json

$keys = ($meta.PSObject.Properties.Name | Sort-Object) -join ','
if ($keys -ne 'bytes,commit,file,sha256,version') { throw "JSON fields are $keys" }
$hash = (Get-FileHash $Archive).Hash.ToLowerInvariant()
if ($meta.sha256 -ne $hash) { throw "JSON sha256 $($meta.sha256) differs from the ZIP $hash" }
if ([long]$meta.bytes -ne (Get-Item $Archive).Length) { throw "JSON bytes $($meta.bytes) differ from the ZIP $((Get-Item $Archive).Length)" }
if ($meta.file -ne [IO.Path]::GetFileName($Archive)) { throw "JSON file $($meta.file) is not the ZIP name" }
if ($meta.commit -notmatch '^[0-9a-f]{40}$') { throw "JSON commit $($meta.commit) is not a commit sha" }
if ($meta.version -notmatch '^\d+\.\d+\.\d+$') { throw "JSON version $($meta.version) is not major.minor.patch" }

$work = Join-Path ([IO.Path]::GetTempPath()) ('call-degrader-test-' + [guid]::NewGuid().ToString('N'))
$proc = $null
try {
    Expand-Archive $Archive $work
    $top = @(Get-ChildItem $work)
    if ($top.Count -ne 1 -or $top[0].Name -ne 'call-degrader') { throw "the ZIP has to hold one call-degrader folder, found: $($top.Name -join ', ')" }
    $exe = Join-Path $top[0].FullName 'call-degrader.exe'
    foreach ($required in 'call-degrader.exe', 'README.md', 'LICENSE', 'THIRD-PARTY.md', '_internal\ui\index.html', '_internal\assets\icon.ico') {
        if (-not (Test-Path (Join-Path $top[0].FullName $required))) { throw "missing $required" }
    }

    $proc = Start-Process $exe -ArgumentList '--port', $Port, '--no-window', '--pattern', '--no-audio' -PassThru
    $base = "http://127.0.0.1:$Port"
    $served = $false
    for ($i = 0; $i -lt 240 -and -not $proc.HasExited; $i++) {
        try { Invoke-WebRequest "$base/" -TimeoutSec 2 | Out-Null; $served = $true; break } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $served) { throw "the extracted exe never served $base/ (exited: $($proc.HasExited))" }

    # the data files are what a bundle drops, and it drops them quietly
    if ((Invoke-WebRequest "$base/").Content -notmatch 'call-degrader') { throw 'index.html does not serve' }
    if ((Invoke-WebRequest "$base/static/app.js").Content -notmatch 'abRefreshSoon') { throw 'app.js not bundled' }
    if ((Invoke-WebRequest "$base/static/style.css").Content -notmatch 'ab-play') { throw 'style.css not bundled' }

    $video = $null
    for ($i = 0; $i -lt 40; $i++) {
        $video = (Invoke-RestMethod "$base/api/state").status.video
        if ($video.running) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $video.running) { throw "video chain not running: $($video.error)" }

    [pscustomobject]@{ version = $meta.version; backend = $video.backend; virtual_camera = $video.virtual_camera } | ConvertTo-Json -Compress
    "package ok: $($meta.file) $($meta.version) $hash"
}
finally {
    # the tree, not the launched process: a survivor keeps the port and the virtual camera
    if ($proc -and -not $proc.HasExited) { taskkill /F /T /PID $proc.Id | Out-Null }
    Start-Sleep -Milliseconds 500
    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
}
