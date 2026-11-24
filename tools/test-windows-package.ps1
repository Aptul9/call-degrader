# Tests the download made by package-windows.ps1 the way a user gets it: checks the JSON against the exe, copies the exe
# into a new folder with the Mark of the Web a browser puts on it, and starts it twice. Windowed first, because that is the
# path a blocked .NET assembly broke while every headless check passed; then on the test pattern with no audio (a CI runner
# has neither a camera nor VB-CABLE), checking the page, its data files and a state reporting the packaged version. Last,
# it ends only the launched process, the bootloader, and checks the application inside goes with it.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Exe,
    [int]$Port = 8761
)
$ErrorActionPreference = 'Stop'
$Exe = (Resolve-Path $Exe).Path
$name = [IO.Path]::GetFileNameWithoutExtension($Exe)
$meta = Get-Content (Join-Path (Split-Path $Exe -Parent) "$name.json") -Raw | ConvertFrom-Json

$keys = ($meta.PSObject.Properties.Name | Sort-Object) -join ','
if ($keys -ne 'bytes,commit,file,sha256,version') { throw "JSON fields are $keys" }
$hash = (Get-FileHash $Exe).Hash.ToLowerInvariant()
if ($meta.sha256 -ne $hash) { throw "JSON sha256 $($meta.sha256) differs from the exe $hash" }
if ([long]$meta.bytes -ne (Get-Item $Exe).Length) { throw "JSON bytes $($meta.bytes) differ from the exe $((Get-Item $Exe).Length)" }
if ($meta.file -ne [IO.Path]::GetFileName($Exe)) { throw "JSON file $($meta.file) is not the exe name" }
if ($meta.commit -notmatch '^[0-9a-f]{40}$') { throw "JSON commit $($meta.commit) is not a commit sha" }
if ($meta.version -notmatch '^\d+\.\d+\.\d+$') { throw "JSON version $($meta.version) is not major.minor.patch" }

function Wait-Served([string]$base, $proc, [int]$tries = 240) {
    for ($i = 0; $i -lt $tries -and -not $proc.HasExited; $i++) {
        try { Invoke-WebRequest "$base/" -TimeoutSec 2 | Out-Null; return $true } catch { Start-Sleep -Milliseconds 500 }
    }
    return $false
}

# CreateProcess, not ShellExecute: Start-Process on a file with the Mark of the Web gets the attachment manager's "Open
# File - Security Warning" on Windows Server, a modal no CI runner ever answers. The mark still does what is under test,
# since what .NET checks is the zone of the assembly it loads, not how the process was started.
function Start-App([string]$exe, [string[]]$arguments) {
    $info = [Diagnostics.ProcessStartInfo]::new($exe)
    $info.UseShellExecute = $false
    foreach ($a in $arguments) { $info.ArgumentList.Add($a) }
    $p = [Diagnostics.Process]::Start($info)
    $script:started += $p.Id
    $p
}

function Get-Children([int]$id) {
    @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$id" | ForEach-Object { [int]$_.ProcessId })
}

function Stop-Tree($proc) {
    if ($proc -and -not $proc.HasExited) { taskkill /F /T /PID $proc.Id | Out-Null }
    # the virtual camera is single-producer and the driver lets go of it a moment after the process
    Start-Sleep -Milliseconds 1500
}

$work = Join-Path ([IO.Path]::GetTempPath()) ('call-degrader-test-' + [guid]::NewGuid().ToString('N'))
$proc = $null
$started = @()
$children = @()
try {
    New-Item -ItemType Directory $work | Out-Null
    $copy = Join-Path $work ([IO.Path]::GetFileName($Exe))
    Copy-Item $Exe $copy
    Set-Content -LiteralPath $copy -Stream Zone.Identifier -Value "[ZoneTransfer]`r`nZoneId=3"

    # windowed: the window comes from pywebview over pythonnet, which is what the Mark of the Web broke
    $proc = Start-App $copy @('--port', $Port, '--pattern', '--no-audio')
    $base = "http://127.0.0.1:$Port"
    if (-not (Wait-Served $base $proc)) { throw "windowed: never served $base/ (exited: $($proc.HasExited))" }
    "windowed: serving"
    Start-Sleep -Seconds 10
    if ($proc.HasExited) { throw "windowed: exited $($proc.ExitCode) after serving; see %LOCALAPPDATA%\call-degrader\call-degrader.log" }
    "windowed: still up 10 s later"
    $children += Get-Children $proc.Id
    Stop-Tree $proc

    $Port++
    $proc = Start-App $copy @('--port', $Port, '--no-window', '--pattern', '--no-audio')
    $base = "http://127.0.0.1:$Port"
    if (-not (Wait-Served $base $proc)) { throw "the downloaded exe never served $base/ (exited: $($proc.HasExited))" }
    "--no-window: serving"

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
    "--no-window: video chain running"

    # the launched process is the bootloader; ending it alone has to end the app inside too
    $children += Get-Children $proc.Id
    Stop-Process -Id $proc.Id -Force
    $gone = $false
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        try { Invoke-WebRequest "$base/" -TimeoutSec 1 | Out-Null } catch { $gone = $true; break }
    }
    if (-not $gone) { throw 'the app went on serving after its bootloader was ended' }

    [pscustomobject]@{ version = $meta.version; backend = $video.backend; virtual_camera = $video.virtual_camera } | ConvertTo-Json -Compress
    "package ok: $($meta.file) $($meta.version) $hash"
}
finally {
    Stop-Tree $proc
    # by pid: on a runner %TEMP% is an 8.3 short path and a process reports its long one, so matching on the path misses
    foreach ($id in $started + $children) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
}
