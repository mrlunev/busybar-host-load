# Host Load installer for Windows.
#
# From a downloaded copy:   double-click Install-Windows.bat
# Without downloading:      irm https://raw.githubusercontent.com/mrlunev/busybar-host-load/main/install.ps1 | iex
# To remove everything:     double-click setup\Uninstall-Windows.bat
#
# All it does itself is make sure uv (https://docs.astral.sh/uv/) is available; uv
# then runs setup\hostload_setup.py with a Python it manages, so nothing else has
# to be installed beforehand.
param([switch]$Uninstall)

function Invoke-HostLoadSetup([bool]$Uninstall, [string]$Here) {
    $ErrorActionPreference = 'Stop'
    $rawBase = 'https://raw.githubusercontent.com/mrlunev/busybar-host-load/main'

    function Find-Uv {
        $cmd = Get-Command uv -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
        foreach ($candidate in @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe")) {
            if (Test-Path $candidate) { return $candidate }
        }
        return $null
    }

    $uv = Find-Uv
    if (-not $uv) {
        if ($Uninstall) {
            Write-Host "uv is not installed, so neither is Host Load's Glances. Nothing to remove on this computer."
            return 0
        }
        Write-Host 'Installing uv, the tool that sets up Python for Host Load (https://docs.astral.sh/uv/)...'
        $env:UV_NO_MODIFY_PATH = '1'
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" | Out-Null
        $uv = Find-Uv
        if (-not $uv) { throw 'uv could not be installed.' }
        $env:HOSTLOAD_UV_INSTALLED = '1'
    }
    $env:HOSTLOAD_UV = $uv
    # Run on a Python already on this computer when there is one; uv downloads one only if not.
    $env:UV_PYTHON_PREFERENCE = 'system'

    # Use the copy next to this file when there is one, otherwise fetch it.
    $script = $null
    if ($Here) { $script = Join-Path $Here 'setup\hostload_setup.py' }
    if (-not $script -or -not (Test-Path $script)) { $script = "$rawBase/setup/hostload_setup.py" }

    $action = if ($Uninstall) { 'uninstall' } else { 'install' }
    & $uv run --quiet --no-project $script $action
    return $LASTEXITCODE
}

$code = Invoke-HostLoadSetup -Uninstall:$Uninstall.IsPresent -Here $PSScriptRoot
# Run as a file, report the result; pasted into a console, leave the window open.
if ($PSCommandPath) { exit $code }
