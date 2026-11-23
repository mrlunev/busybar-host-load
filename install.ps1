# Host Load installer for Windows.
#
# From a downloaded copy:   double-click Install-Windows.bat
# Without downloading:      irm https://raw.githubusercontent.com/mrlunev/busybar-host-load/main/install.ps1 | iex
# To remove everything:     double-click setup\Uninstall-Windows.bat
#
# All it does itself is make sure uv (https://docs.astral.sh/uv/) is available; uv
# then runs setup\hostload_setup.py with a Python it finds or fetches, so nothing
# else has to be installed beforehand.
param(
    [switch]$Uninstall,
    # Passed on to setup\hostload_setup.py: --bar ADDRESS, --package FILE, --no-firewall.
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$SetupArgs
)

# Not called in an assignment: PowerShell would then capture everything the
# installer prints instead of showing it, and the installer would not see a
# console. The exit code travels in $script:HostLoadExitCode instead.
function Invoke-HostLoadSetup([bool]$Uninstall, [string]$Here, [string[]]$SetupArgs) {
    $ErrorActionPreference = 'Stop'
    $rawBase = 'https://raw.githubusercontent.com/mrlunev/busybar-host-load/main'
    # setup\hostload_setup.py exits with this code when uv should go too.
    $removeUvExit = 10
    # Oldest uv with everything the installer uses (remote scripts, --exclude-newer,
    # a separate folder for downloaded Pythons).
    $minUv = [version]'0.5.0'
    # By full path: a powershell.exe planted in the current folder must not run instead.
    $powershellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

    function Say([string]$text) { Write-Host "  $text" }

    # Windows PowerShell 5.1 turns each line a native program writes to a
    # redirected stderr into an error record, and 'Stop' above makes the first
    # one fatal. uv reports ordinary things there ("No interpreter found",
    # "Clearing cache"), so its quiet calls run with 'Continue'.
    function Invoke-Native([scriptblock]$block) {
        $ErrorActionPreference = 'Continue'
        & $block 2>$null
    }

    function Find-Uv {
        $cmd = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cmd) { return $cmd.Source }
        # Where uv's own installer puts it: UV_INSTALL_DIR, XDG_BIN_HOME, ~\.local\bin, ~\.cargo\bin.
        $candidates = @()
        if ($env:UV_INSTALL_DIR) { $candidates += Join-Path $env:UV_INSTALL_DIR 'uv.exe' }
        if ($env:XDG_BIN_HOME) { $candidates += Join-Path $env:XDG_BIN_HOME 'uv.exe' }
        $candidates += "$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe"
        foreach ($candidate in $candidates) {
            if (Test-Path $candidate) { return $candidate }
        }
        return $null
    }

    function Test-UvTooOld([string]$uv) {
        $text = Invoke-Native { & $uv --version }
        if ("$text" -match 'uv (\d+\.\d+\.\d+)') { return ([version]$Matches[1]) -lt $minUv }
        return $true
    }

    function Remove-Uv([string]$uv) {
        Say 'Removing uv...'
        $dirs = @((Invoke-Native { & $uv python dir }), (Invoke-Native { & $uv tool dir }), (Invoke-Native { & $uv cache dir }))
        Invoke-Native { & $uv cache clean } | Out-Null
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
        & $powershellExe -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" | Out-Null
        $uv = Find-Uv
        if (-not $uv) { throw 'uv could not be installed.' }
        Say 'uv is ready.'
        $env:HOSTLOAD_UV_INSTALLED = '1'
    }
    if (Test-UvTooOld $uv) {
        Say "The uv on this computer is older than $minUv; updating it..."
        Invoke-Native { & $uv self update } | Out-Null
        if (Test-UvTooOld $uv) {
            Say "Could not update uv at $uv. Update it the way it was installed (for example"
            Say "'winget upgrade astral-sh.uv' or 'scoop update uv'), then run this installer again."
            $script:HostLoadExitCode = 1
            return
        }
    }
    $env:HOSTLOAD_UV = $uv
    # The installer itself runs on a Python already on this computer when there is one;
    # uv downloads one only if not. (Glances gets a Python of its own either way.)
    $env:UV_PYTHON_PREFERENCE = 'system'

    Invoke-Native { & $uv python find '>=3.9' } | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Say 'No Python on this computer yet; downloading one for the installer...'
        & $uv python install 3.12
    }

    # Use the copy next to this file when there is one, otherwise fetch it.
    $script = $null
    if ($Here) { $script = Join-Path $Here 'setup\hostload_setup.py' }
    if (-not $script -or -not (Test-Path $script)) { $script = "$rawBase/setup/hostload_setup.py" }

    $action = if ($Uninstall) { 'uninstall' } else { 'install' }
    & $uv run --quiet --no-project $script $action @SetupArgs
    $code = $LASTEXITCODE
    if ($code -eq $removeUvExit) {
        Remove-Uv $uv
        $code = 0
    }
    $script:HostLoadExitCode = $code
}

$script:HostLoadExitCode = 1
# Pasted into a console, the variables set here would stay in that console and
# steer the next uv command typed there; they are put back afterwards.
$savedEnv = @{}
foreach ($name in 'HOSTLOAD_UV', 'HOSTLOAD_UV_INSTALLED', 'UV_PYTHON_PREFERENCE', 'UV_NO_MODIFY_PATH') {
    $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    Invoke-HostLoadSetup -Uninstall:$Uninstall.IsPresent -Here $PSScriptRoot -SetupArgs $SetupArgs
} finally {
    foreach ($name in $savedEnv.Keys) { [Environment]::SetEnvironmentVariable($name, $savedEnv[$name], 'Process') }
}
# Run as a file, report the result; pasted into a console, leave the window open.
if ($PSCommandPath) { exit $script:HostLoadExitCode }
