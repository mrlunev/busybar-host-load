# Host Load installer for Windows.
#
# From a downloaded copy:   double-click Install-Windows.bat
# Without downloading:      irm https://raw.githubusercontent.com/mrlunev/busybar-host-load/main/install.ps1 | iex
# To remove everything:     double-click setup\Uninstall-Windows.bat
#
# All it does itself is make sure uv (https://docs.astral.sh/uv/) is available; uv
# then runs setup\hostload_setup.py with a Python it finds or fetches, so nothing
# else has to be installed beforehand.
param([switch]$Uninstall)

# Not called in an assignment: PowerShell would then capture everything the
# installer prints instead of showing it, and the installer would not see a
# console. The exit code travels in $script:HostLoadExitCode instead.
function Invoke-HostLoadSetup([bool]$Uninstall, [string]$Here) {
    $ErrorActionPreference = 'Stop'
    $rawBase = 'https://raw.githubusercontent.com/mrlunev/busybar-host-load/main'
    # setup\hostload_setup.py exits with this code when uv should go too.
    $removeUvExit = 10

    function Say([string]$text) { Write-Host "  $text" }

    function Find-Uv {
        $cmd = Get-Command uv -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
        foreach ($candidate in @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe")) {
            if (Test-Path $candidate) { return $candidate }
        }
        return $null
    }

    function Remove-Uv([string]$uv) {
        Say 'Removing uv...'
        $dirs = @((& $uv python dir 2>$null), (& $uv tool dir 2>$null), (& $uv cache dir 2>$null))
        & $uv cache clean *> $null
        # Only uv's own folders, never anything that does not say so.
        foreach ($dir in $dirs) {
            if ($dir -and $dir -match 'uv') { Remove-Item -Recurse -Force $dir -ErrorAction SilentlyContinue }
        }
        foreach ($receipt in @("$env:LOCALAPPDATA\uv\uv-receipt.json", "$env:APPDATA\uv\uv-receipt.json")) {
            Remove-Item -Force $receipt -ErrorAction SilentlyContinue
        }
        foreach ($dir in @("$env:LOCALAPPDATA\uv", "$env:APPDATA\uv")) {
            if ((Test-Path $dir) -and -not (Get-ChildItem $dir -Force)) { Remove-Item -Force $dir }
        }
        $bin = Split-Path $uv
        foreach ($exe in 'uv.exe', 'uvx.exe', 'uvw.exe') {
            Remove-Item -Force (Join-Path $bin $exe) -ErrorAction SilentlyContinue
        }
        if ((Test-Path $bin) -and -not (Get-ChildItem $bin -Force)) { Remove-Item -Force $bin }
        Say 'uv removed.'
    }

    $uv = Find-Uv
    if (-not $uv) {
        if ($Uninstall) {
            Say "uv is not installed, so neither is Host Load's Glances. Nothing to remove on this computer."
            $script:HostLoadExitCode = 0
            return
        }
        Say 'Installing uv, a small tool that runs this installer (https://docs.astral.sh/uv/)...'
        $env:UV_NO_MODIFY_PATH = '1'
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" | Out-Null
        $uv = Find-Uv
        if (-not $uv) { throw 'uv could not be installed.' }
        Say 'uv is ready.'
        $env:HOSTLOAD_UV_INSTALLED = '1'
    }
    $env:HOSTLOAD_UV = $uv
    # Run on a Python already on this computer when there is one; uv downloads one only if not.
    $env:UV_PYTHON_PREFERENCE = 'system'

    & $uv python find '>=3.9' *> $null
    if ($LASTEXITCODE -ne 0) {
        Say 'No Python on this computer yet; downloading one for the installer...'
        & $uv python install 3.12
    }

    # Use the copy next to this file when there is one, otherwise fetch it.
    $script = $null
    if ($Here) { $script = Join-Path $Here 'setup\hostload_setup.py' }
    if (-not $script -or -not (Test-Path $script)) { $script = "$rawBase/setup/hostload_setup.py" }

    $action = if ($Uninstall) { 'uninstall' } else { 'install' }
    & $uv run --quiet --no-project $script $action
    $code = $LASTEXITCODE
    if ($code -eq $removeUvExit) {
        Remove-Uv $uv
        $code = 0
    }
    $script:HostLoadExitCode = $code
}

$script:HostLoadExitCode = 1
Invoke-HostLoadSetup -Uninstall:$Uninstall.IsPresent -Here $PSScriptRoot
# Run as a file, report the result; pasted into a console, leave the window open.
if ($PSCommandPath) { exit $script:HostLoadExitCode }
