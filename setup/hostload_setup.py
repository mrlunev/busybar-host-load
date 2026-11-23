# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
Host Load installer: sets up this computer and a BUSY Bar in one go.

What `install` does:

1. Installs Glances, with a Python of its own, into the Host Load folder,
   limited to the CPU and memory plugins, and starts it in web server mode now
   and at every login. Both starts run the same command through the same
   launcher, start-glances.py, so what works during the install works at login.
2. Opens port 61208 in the firewall where that needs doing.
3. Finds the BUSY Bar: over USB, on the network via mDNS, or by address.
4. Installs the Host Load app on the bar, writes this computer's address into
   the app's settings and launches it.
5. Checks the bar's screen to confirm the graphs are coming in.

`uninstall` reverses all of it: the app, Glances and its data folder, the login
item, and the firewall changes the installer made (on Windows its rules, which
replace earlier Block rules for that Python; on macOS the exception for the
Python running Glances, unless the user had added it before).

Run through the `install.sh` / `install.ps1` launchers, which bring `uv`. Only
the Python standard library is used, so the script also runs on its own:

    uv run setup/hostload_setup.py install [--bar ADDRESS] [--package FILE]
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import plistlib
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_ID = "app.mrlunev.hostload"
APP_NAME = "Host Load"
REPO = "mrlunev/busybar-host-load"
PACKAGE_URL = f"https://github.com/{REPO}/releases/latest/download/{APP_ID}.tgz"

# Glances 4 serves the /api/4 REST API the app reads. The installed set is fixed:
# Glances itself by version, its dependencies (uvicorn, fastapi, psutil...) by
# taking what PyPI had at DEPENDENCIES_AS_OF, so a new release of any of them
# cannot change what runs on people's computers. To move on, raise both and test
# the installer on macOS, Windows and Linux.
GLANCES_SPEC = "glances[web]==4.5.9"
DEPENDENCIES_AS_OF = "2026-11-22T00:00:00Z"
GLANCES_PORT = 61208
GLANCES_ARGS = [
    "-w",
    "--disable-webui",
    "-p", str(GLANCES_PORT),
    # Only what the bar shows is exposed to the network, nothing about processes or disks.
    "--disable-plugin", "all",
    "--enable-plugin", "cpu,mem",
    # No calls to PyPI and no cache file outside the Host Load folder.
    "--disable-check-update",
    # Fresh values down to the bar's shortest step, 0.5 s: plugins refresh every 0.5 s
    # instead of 2, and the API answers from them without its own 1 s cache.
    "--time", "0.5",
    "--cached-time", "0",
]
# Glances runs on a Python of its own, which uv downloads into the Host Load
# folder: a Python already on the computer can be upgraded, moved or removed by
# Homebrew, the Microsoft Store, a distribution upgrade or the user, and Glances
# would then quietly stop starting at login. A Python on the computer is used
# only when that download fails (no wheels for the platform, NixOS).
GLANCES_PYTHON = "3.12"
SYSTEM_PYTHON_RANGE = ">=3.10,<3.15"

# The bar is always at this address when connected over USB.
USB_ADDRESS = "10.0.4.20"
# Firmware 1.3.1 reports API 27.14.0; the app needs at least this.
MIN_API = (27, 12, 0)

SERVICE_NAME = "busybar-host-load"
LAUNCHD_LABEL = "com.github.mrlunev.busybar-host-load"
WINDOWS_RUN_VALUE = "BUSY Bar Host Load"
WINDOWS_FIREWALL_RULE = "BUSY Bar Host Load (Glances)"

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")


# --------------------------------------------------------------------------- output


class SetupError(Exception):
    """A problem the user can fix; printed without a traceback."""


def enable_colors() -> bool:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if not IS_WINDOWS:
        return True
    # cmd and PowerShell on Windows 10 and later show ANSI colours once the console is asked to.
    try:
        import ctypes

        kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


COLORS = enable_colors()


def paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLORS else text


def bold(text: str) -> str:
    return paint("1", text)


def dim(text: str) -> str:
    return paint("2", text)


def green(text: str) -> str:
    return paint("32", text)


def yellow(text: str) -> str:
    return paint("33", text)


def cyan(text: str) -> str:
    return paint("36", text)


RULE = "-" * 64


def banner(title: str, subtitle: str) -> None:
    print()
    print(cyan("  " + RULE))
    print("    " + bold(title))
    print("    " + dim(subtitle))
    print(cyan("  " + RULE))


def step(n: int, total: int, text: str) -> None:
    print(f"\n{cyan(f'[{n}/{total}]')} {bold(text)}")


def ok(text: str) -> None:
    print(f"  {green('ok')}  {text}")


def note(text: str) -> None:
    print(f"      {text}")


def warn(text: str) -> None:
    print(f"  {yellow('!!')}  {text}")


class working:
    """
    Shows what is going on during a slow operation: a spinner, the text and a
    stopwatch, on one line that is cleared when the operation ends. Without a
    terminal (output piped to a file) it prints the text once instead.
    """

    FRAMES = "|/-\\"

    def __init__(self, text: str) -> None:
        self.text = text
        self.started = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def seconds(self) -> int:
        return int(time.time() - self.started)

    def __enter__(self) -> "working":
        self.started = time.time()
        if sys.stdout.isatty():
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            print(f"  ..  {self.text}...", flush=True)
        return self

    def _spin(self) -> None:
        frame = 0
        while True:
            clock = f"{self.seconds // 60}:{self.seconds % 60:02d}"
            sys.stdout.write(f"\r  {cyan(self.FRAMES[frame % 4])}   {self.text}...  {dim(clock)} ")
            sys.stdout.flush()
            frame += 1
            if self._stop.wait(0.15):
                break

    def __exit__(self, *_: object) -> bool:
        if self._thread:
            self._stop.set()
            self._thread.join()
            sys.stdout.write("\r" + " " * (len(self.text) + 24) + "\r")
            sys.stdout.flush()
        return False


def interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def ask(question: str, default: str = "") -> str:
    if not interactive():
        return default
    suffix = f" [{default}]" if default else ""
    answer = input(f"  {cyan('??')}  {question}{suffix}: ").strip()
    return answer or default


def confirm(question: str, default: bool = True) -> bool:
    answer = ask(f"{question} (y/n)", "y" if default else "n").lower()
    return answer.startswith("y")


# --------------------------------------------------------------------------- paths and state


def data_dir() -> Path:
    """Where Glances, its Python, its logs and the install record live."""
    home = Path.home()
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    elif IS_MAC:
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return base / SERVICE_NAME


def venv_dir() -> Path:
    return data_dir() / "glances"


def launcher_path() -> Path:
    return data_dir() / "start-glances.py"


def log_path() -> Path:
    return data_dir() / "logs" / "output.log"


def state_path() -> Path:
    return data_dir() / "install.json"


def load_state() -> dict:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def find_uv() -> str:
    candidates = [os.environ.get("HOSTLOAD_UV"), os.environ.get("UV"), shutil.which("uv")]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise SetupError("uv was not found. Run the installer through install.sh or install.ps1.")


def child_env(**extra: str) -> dict:
    """The environment for programs the installer runs: UTF-8 output, whatever the locale."""
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    env.update(extra)
    return env


def run(command: list[str], check: bool = True, quiet: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL if quiet else None,
        stdout=subprocess.PIPE if quiet else None,
        stderr=subprocess.STDOUT if quiet else None,
        # Paths can hold any letters; a locale code page must not turn them into an exception.
        encoding="utf-8",
        errors="replace",
        env=env or child_env(),
    )
    if check and result.returncode != 0:
        output = (result.stdout or "").strip()
        raise SetupError(f"'{Path(command[0]).name} {' '.join(command[1:3])}' failed:\n{output}")
    return result


# Requests to the bar and to Glances go straight to them: a proxy set in the system
# settings or in http_proxy cannot reach 10.0.4.20 or a local network address.
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# --------------------------------------------------------------------------- Glances


def glances_answers(host: str = "127.0.0.1", timeout: float = 2) -> bool:
    try:
        with DIRECT.open(f"http://{host}:{GLANCES_PORT}/api/4/cpu/total", timeout=timeout) as r:
            return "total" in json.loads(r.read().decode())
    except (OSError, ValueError):
        return False


def port_taken() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", GLANCES_PORT)) == 0


def venv_python(venv: Path | None = None, windowless: bool = False) -> Path:
    venv = venv or venv_dir()
    if IS_WINDOWS:
        return venv / "Scripts" / ("pythonw.exe" if windowless else "python.exe")
    return venv / "bin" / "python"


def uv_env() -> dict:
    """uv keeps the Python it downloads for Glances, and its cache, in the Host Load
    folder: nothing outside it changes, and uninstalling removes all of it."""
    return child_env(
        UV_PYTHON_INSTALL_DIR=str(data_dir() / "python"),
        UV_CACHE_DIR=str(data_dir() / "cache"),
        UV_PYTHON_DOWNLOADS="automatic",
    )


def create_venv(uv: str, venv: Path, managed: bool) -> bool:
    shutil.rmtree(venv, ignore_errors=True)
    if managed:
        flags = ["--python-preference", "only-managed", "--python", GLANCES_PYTHON]
    else:
        flags = ["--python-preference", "only-system", "--python", SYSTEM_PYTHON_RANGE]
    return run([uv, "venv", "--quiet", *flags, str(venv)], check=False, env=uv_env()).returncode == 0


def install_packages(uv: str, venv: Path) -> subprocess.CompletedProcess:
    return run(
        [uv, "pip", "install", "--quiet", "--python", str(venv_python(venv)),
         "--exclude-newer", DEPENDENCIES_AS_OF, GLANCES_SPEC],
        check=False,
        env=uv_env(),
    )


def describe_python(venv: Path) -> str:
    probe = "import platform, sys; print(platform.python_version()); print(sys.base_prefix)"
    lines = (run([str(venv_python(venv)), "-c", probe], check=False).stdout or "").split("\n")
    version, home = lines[0].strip(), (lines[1].strip() if len(lines) > 1 else "")
    if os.path.normcase(os.path.realpath(home)).startswith(os.path.normcase(os.path.realpath(data_dir()))):
        return f"Python {version} of its own"
    return f"Python {version} already on this computer"


def build_glances(uv: str) -> Path:
    """
    Builds a fresh Glances environment next to the current one and returns it. The
    running Glances keeps serving the bar meanwhile; the new one replaces it only
    once it is complete, so a failed download leaves the old one working.
    """
    new = data_dir() / "glances.new"
    data_dir().mkdir(parents=True, exist_ok=True)
    with working(f"Getting Python {GLANCES_PYTHON} for Glances"):
        managed = create_venv(uv, new, managed=True)
    with working("Installing Glances (about a minute the first time)"):
        result = install_packages(uv, new) if managed else None
    if result is None or result.returncode != 0:
        warn(f"Could not set up Python {GLANCES_PYTHON} for Glances; trying a Python already on this computer")
        with working("Installing Glances on the Python of this computer"):
            if not create_venv(uv, new, managed=False):
                raise SetupError(
                    f"No Python for Glances: uv could not download Python {GLANCES_PYTHON}, and there is no "
                    "Python 3.10-3.14 on this computer. Check the internet connection and run the installer again."
                )
            result = install_packages(uv, new)
        if result.returncode != 0:
            raise SetupError(f"Glances did not install:\n{(result.stdout or '').strip()}")
    ok(f"Using {describe_python(new)}")
    # From the package metadata: running Glances for its version would write its log
    # into the home folder, where nothing of ours belongs.
    probe = "import importlib.metadata as m; print(m.version('glances'))"
    version = (run([str(venv_python(new)), "-I", "-c", probe], check=False).stdout or "").strip()
    ok(f"Glances {version} installed" if version else "Glances installed")
    return new


def replace_dir(new: Path, target: Path) -> None:
    """Puts `new` in place of `target`. Windows releases a stopped program's files a moment later."""
    old = target.with_name(target.name + ".old")
    for attempt in range(15):
        try:
            shutil.rmtree(old, ignore_errors=True)
            if target.exists():
                target.rename(old)
            new.rename(target)
            shutil.rmtree(old, ignore_errors=True)
            return
        except OSError:
            if attempt == 14:
                raise SetupError(f"Could not replace {target}; restart the computer and run the installer again.") from None
            time.sleep(1)


# The launcher every start of Glances goes through. Written into the Host Load folder.
LAUNCHER = '''\
"""
Starts Glances for the BUSY Bar Host Load app. Written by the Host Load installer:
the installer and every login start Glances through this file, with one command.

    python -I start-glances.py run         Glances in this process
    python -I start-glances.py supervise   Glances in a child process, restarted if it stops
"""
import os
import runpy
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(HERE, "logs")
ARGS = {args!r} + ["-C", os.path.join(HERE, "glances.conf")]
LOG_LIMIT = 1000000


def open_log():
    os.makedirs(LOGS, exist_ok=True)
    path = os.path.join(LOGS, "output.log")
    try:
        if os.path.getsize(path) > LOG_LIMIT:
            os.replace(path, path + ".1")
    except OSError:
        pass
    return open(path, "a", encoding="utf-8", errors="replace", buffering=1)


def run():
    log = open_log()
    # Started at a Windows login, pythonw has no stdout or stderr at all (None), and
    # uvicorn fails to set up its logging without them. Started by the installer it
    # has some, at login or under launchd and systemd others. Always the same file.
    sys.stdout = sys.stderr = log
    sys.stdin = open(os.devnull, encoding="utf-8")
    # Glances writes its own log under XDG_CACHE_HOME on every system; keep it here.
    os.environ["XDG_CACHE_HOME"] = LOGS
    os.environ.pop("LOG_CFG", None)
    os.chdir(HERE)
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "starting Glances", sys.version.split()[0], flush=True)
    sys.argv = ["glances"] + ARGS
    # Glances keeps the CPU percentage for 2 s whatever the API cache says; the bar can
    # sample every 0.5 s. Not a setting in Glances, so the cache is shortened here, and
    # a Glances without it still starts.
    try:
        import glances.cpu_percent
        glances.cpu_percent.cpu_percent.cached_timer_cpu = 0.5
    except Exception as error:
        print("CPU cache left at its default:", error, flush=True)
    runpy.run_module("glances", run_name="__main__", alter_sys=True)


def supervise():
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    while True:
        started = time.time()
        subprocess.call(
            [sys.executable, "-I", os.path.abspath(__file__), "run"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=HERE, creationflags=flags,
        )
        # Back at once after a long run; slowly while it keeps failing (port taken).
        time.sleep(5 if time.time() - started > 60 else 30)


if __name__ == "__main__":
    supervise() if sys.argv[1:] == ["supervise"] else run()
'''

# Read with -C instead of any glances.conf of the user's, which could enable more
# plugins, turn update checks back on or, in another encoding, stop Glances at start.
GLANCES_CONF = "[global]\ncheck_update=false\n"


def write_launcher() -> None:
    (data_dir() / "glances.log").unlink(missing_ok=True)  # where 1.0.x logged; unused since
    launcher_path().write_text(LAUNCHER.format(args=GLANCES_ARGS), encoding="utf-8")
    (data_dir() / "glances.conf").write_text(GLANCES_CONF, encoding="utf-8")
    log_path().parent.mkdir(parents=True, exist_ok=True)


def glances_command(mode: str) -> list[str]:
    """
    The one command that starts Glances, at install and at login. `-I` keeps the
    user's PYTHONHOME, PYTHONPATH and user site-packages out of it. On Windows
    pythonw opens no console window.
    """
    return [str(venv_python(windowless=True)), "-I", str(launcher_path()), mode]


def our_processes() -> list[int]:
    """
    Processes of our Glances: those whose program is in the Host Load folder (the
    environment's Python and, since 1.0.2, the Python under it), and on Windows the
    children of those, because a venv's python.exe only relays to the base one.
    Matching the program rather than the command line spares an editor that has
    our log open.
    """
    folder = os.path.normcase(str(data_dir())) + os.sep
    # Never this process or the one that started it (a venv relay on Windows).
    spared = {os.getpid(), os.getppid()}
    if IS_WINDOWS:
        folder_ps = str(data_dir()).replace("'", "''")
        script = (
            f"$dir = '{folder_ps}\\'\n"
            "$all = @(Get-CimInstance Win32_Process)\n"
            f"$spared = @({','.join(str(p) for p in spared)}, $PID)\n"
            "$ours = @($all | Where-Object { $spared -notcontains $_.ProcessId -and $_.ExecutablePath -and "
            "$_.ExecutablePath.StartsWith($dir, [StringComparison]::OrdinalIgnoreCase) })\n"
            "$ids = @($ours | ForEach-Object { $_.ProcessId })\n"
            "$kids = @($all | Where-Object { $ids -contains $_.ParentProcessId -and $spared -notcontains $_.ProcessId })\n"
            "($ours + $kids) | ForEach-Object { $_.ProcessId } | Sort-Object -Unique\n"
        )
        output = powershell(script).stdout or ""
        return [int(line) for line in output.split() if line.strip().isdigit()]
    output = run(["ps", "-axo", "pid=,command="], check=False).stdout or ""
    pids = []
    for line in output.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and int(pid) not in spared and os.path.normcase(command.strip()).startswith(folder):
            pids.append(int(pid))
    return pids


def kill_our_processes() -> None:
    pids = our_processes()
    if not pids:
        return
    if IS_WINDOWS:
        ids = ",".join(str(p) for p in pids)
        powershell(f"Stop-Process -Id {ids} -Force -ErrorAction SilentlyContinue")
    else:
        import signal

        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    for _ in range(10):
        if not our_processes():
            return
        time.sleep(0.5)


def stop_glances_service() -> None:
    """Stops our Glances, whichever way and by whichever version it was started."""
    if IS_MAC:
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], check=False)
    elif IS_LINUX and shutil.which("systemctl"):
        run(["systemctl", "--user", "stop", f"{SERVICE_NAME}.service"], check=False)
    kill_our_processes()


def launchd_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def systemd_unit() -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config / "systemd" / "user" / f"{SERVICE_NAME}.service"


def autostart_entry() -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config / "autostart" / f"{SERVICE_NAME}.desktop"


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def start_glances_service(state: dict) -> str:
    """
    Registers Glances to start at login and starts it now, the way the login will:
    launchd and systemd start the job they just registered; on Windows the line
    written to the Run key is read back and run as it is.
    """
    write_launcher()

    if IS_MAC:
        plist = launchd_plist()
        plist.parent.mkdir(parents=True, exist_ok=True)
        with plist.open("wb") as f:
            plistlib.dump(
                {
                    "Label": LAUNCHD_LABEL,
                    "ProgramArguments": glances_command("run"),
                    "RunAtLoad": True,
                    "KeepAlive": True,
                    # Only what fails before the launcher takes over its output lands here.
                    "StandardOutPath": str(log_path()),
                    "StandardErrorPath": str(log_path()),
                },
                f,
            )
        domain = f"gui/{os.getuid()}"
        for attempt in range(5):
            # Right after a bootout launchd may still be tearing the old job down.
            result = run(["launchctl", "bootstrap", domain, str(plist)], check=False)
            if result.returncode == 0:
                break
            run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"], check=False)
            time.sleep(1 + attempt)
        else:
            raise SetupError(f"launchd did not take the login item:\n{(result.stdout or '').strip()}")
        return "launchd"

    if IS_LINUX and systemd_user_available():
        unit = systemd_unit()
        unit.parent.mkdir(parents=True, exist_ok=True)
        exec_start = " ".join(quote_systemd(part) for part in glances_command("run"))
        unit.write_text(
            "[Unit]\n"
            "Description=Glances for the BUSY Bar Host Load app\n\n"
            "[Service]\n"
            f"ExecStart={exec_start}\n"
            "Restart=always\n"
            "RestartSec=5\n\n"
            "[Install]\n"
            "WantedBy=default.target\n",
            encoding="utf-8",
        )
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", f"{SERVICE_NAME}.service"])
        run(["systemctl", "--user", "restart", f"{SERVICE_NAME}.service"])
        enable_linger(state)
        return "systemd"

    if IS_WINDOWS:
        import winreg  # only on Windows

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
            winreg.SetValueEx(key, WINDOWS_RUN_VALUE, 0, winreg.REG_SZ, subprocess.list2cmdline(glances_command("supervise")))
            line, _ = winreg.QueryValueEx(key, WINDOWS_RUN_VALUE)
        spawn_detached(line)
        return "registry"

    # Linux without a systemd user session: the desktop's autostart, and a start now.
    entry = autostart_entry()
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Glances for BUSY Bar Host Load\n"
        f"Exec={' '.join(quote_desktop(part) for part in glances_command('supervise'))}\n"
        "NoDisplay=true\n"
        "X-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )
    spawn_detached(glances_command("supervise"))
    return "autostart"


def spawn_detached(command: str | list[str]) -> None:
    """Starts a program that outlives the installer and the terminal it runs in."""
    if not IS_WINDOWS:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, cwd=str(data_dir()))
        return
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
             | subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
    breakaway = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB: survive a job that ends with the installer
    for extra in (breakaway, 0):
        try:
            subprocess.Popen(command, creationflags=flags | extra, close_fds=True, cwd=str(data_dir()),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            if not extra:
                raise


def remove_glances_service(state: dict) -> None:
    stop_glances_service()
    if IS_MAC:
        launchd_plist().unlink(missing_ok=True)
    elif IS_LINUX:
        unit = systemd_unit()
        if unit.exists():
            run(["systemctl", "--user", "disable", f"{SERVICE_NAME}.service"], check=False)
            unit.unlink()
            run(["systemctl", "--user", "daemon-reload"], check=False)
        autostart_entry().unlink(missing_ok=True)
        if state.get("enabled_linger"):
            run(["loginctl", "--no-ask-password", "disable-linger"], check=False)
    elif IS_WINDOWS:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, WINDOWS_RUN_VALUE)
        except OSError:
            pass


def systemd_user_available() -> bool:
    """True when this login has a systemd user manager to register Glances with."""
    if not shutil.which("systemctl"):
        return False
    result = subprocess.run(["systemctl", "--user", "show-environment"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def enable_linger(state: dict) -> None:
    """
    systemd starts user services at login. Lingering starts them at boot, so a
    computer nobody logs in to (a home server) still serves the bar after a
    restart. Done only where the system allows it without a password; undone on
    uninstall if it was done here.
    """
    if not shutil.which("loginctl"):
        return
    user = getpass.getuser()
    shown = run(["loginctl", "show-user", user, "--property=Linger"], check=False).stdout or ""
    if "Linger=yes" in shown:
        return
    if run(["loginctl", "--no-ask-password", "enable-linger", user], check=False).returncode == 0:
        state["enabled_linger"] = True
    else:
        note("Glances starts when you log in. To have it start at boot too, run: sudo loginctl enable-linger " + user)


def quote_systemd(part: str) -> str:
    """One ExecStart word: quoted, with systemd's specifiers and variables escaped."""
    escaped = part.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def quote_desktop(part: str) -> str:
    """
    One Exec word of a .desktop file (Desktop Entry Specification, 'The Exec key'):
    quoted, with the quoting escapes, then the escapes every string value gets.
    """
    escaped = part
    for char in ("\\", '"', "`", "$"):
        escaped = escaped.replace(char, "\\" + char)
    escaped = escaped.replace("%", "%%").replace("\\", "\\\\")
    return f'"{escaped}"'


def wait_for_glances(seconds: int = 45) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if glances_answers():
            return True
        time.sleep(1)
    return False


def wait_port_free(seconds: int = 10) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not port_taken():
            return True
        time.sleep(0.5)
    return False


# --------------------------------------------------------------------------- firewall

# By full path: a powershell.exe or .cmd planted in the current folder or early on
# PATH must not run instead of Windows' own.
POWERSHELL = str(Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def encode_ps(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def powershell(script: str, elevated: bool = False) -> subprocess.CompletedProcess:
    """
    Runs a PowerShell script, as administrator after the Windows permission prompt if
    asked. Passed as -EncodedCommand: no quoting to get wrong, no file in TEMP, and no
    execution policy applies to it (a policy that forbids unsigned scripts would
    stop a -File).
    """
    command = [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand"]
    # Without this, the first use of a module writes a progress record (CLIXML) into
    # the output that is read back.
    script = "$ProgressPreference = 'SilentlyContinue'\n" + script
    if not elevated:
        return run([*command, encode_ps(script)], check=False)
    exe = POWERSHELL.replace("'", "''")
    launcher = (
        f"try {{ $p = Start-Process -FilePath '{exe}' -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
        f"-ArgumentList '-NoProfile','-NonInteractive','-EncodedCommand','{encode_ps(script)}'; exit $p.ExitCode }} "
        "catch { exit 1 }"
    )
    return run([*command, encode_ps(launcher)], check=False)


def interpreter_programs(venv: Path) -> list[str]:
    """
    The executables Windows sees listening. A venv's pythonw.exe only relays to the
    base interpreter, so the firewall has to know the base one, by its real path:
    uv reaches its Python through a junction, and Windows matches rules against the
    path the program was really loaded from.
    """
    probe = "import os, sys; print(os.path.dirname(sys._base_executable))"
    base = (run([str(venv_python(venv)), "-c", probe], check=False).stdout or "").strip()
    programs: list[str] = []
    if base:
        for folder in (base, os.path.realpath(base)):
            for exe in ("pythonw.exe", "python.exe"):
                path = str(Path(folder) / exe)
                if Path(path).exists() and path.lower() not in (p.lower() for p in programs):
                    programs.append(path)
    return programs


def ps_list(items: list[str]) -> str:
    return "@(" + ", ".join("'" + item.replace("'", "''") + "'" for item in items) + ")"


FIREWALL_RULE_KEYS = (
    r"SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\FirewallRules",
    r"SOFTWARE\Policies\Microsoft\WindowsFirewall\FirewallRules",  # set by group policy
)


def windows_firewall_rules() -> list[dict]:
    """
    Inbound and outbound rules as Windows stores them ("v2.30|Action=Allow|Dir=In|...").
    Read from the registry because, without administrator rights, Get-NetFirewallRule
    returns nothing and Get-NetFirewallApplicationFilter fails with "Access is denied",
    while the registry can be read by any user.
    """
    import winreg

    rules = []
    for path in FIREWALL_RULE_KEYS:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
        except OSError:
            continue
        with key:
            index = 0
            while True:
                try:
                    _, value, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if isinstance(value, str):
                    fields = dict(part.split("=", 1) for part in value.split("|") if "=" in part)
                    rules.append(fields)
    return rules


def windows_rules_in_place(programs: list[str]) -> bool:
    """Whether our rules already allow these programs and nothing blocks them. Needs no administrator."""
    if not programs:
        return False
    try:
        rules = windows_firewall_rules()
    except OSError:
        return False

    def active(rule: dict, action: str) -> bool:
        return rule.get("Action") == action and rule.get("Active", "").upper() == "TRUE" and rule.get("Dir") == "In"

    def app(rule: dict) -> str:
        return os.path.normcase(os.path.expandvars(rule.get("App", "")))

    port_rule = any(active(r, "Allow") and r.get("Name") == WINDOWS_FIREWALL_RULE and "App" not in r
                    and str(GLANCES_PORT) in r.get("LPort", "").split(",") for r in rules)
    ours = {app(r) for r in rules if active(r, "Allow") and r.get("Name", "").startswith(WINDOWS_FIREWALL_RULE)}
    blocked = {app(r) for r in rules if active(r, "Block")}
    wanted = {os.path.normcase(p) for p in programs}
    return port_rule and wanted <= ours and not (wanted & blocked)


def windows_rules_exist() -> bool:
    """Whether any rule named by an installer of Host Load is there (1.0.x did not always record them)."""
    try:
        return any(r.get("Name", "").startswith(WINDOWS_FIREWALL_RULE) for r in windows_firewall_rules())
    except OSError:
        return False


def windows_firewall_script(programs: list[str]) -> str:
    rule = WINDOWS_FIREWALL_RULE.replace("'", "''")
    return f"""$ErrorActionPreference = 'Stop'
$rule = '{rule}'
Get-NetFirewallRule -DisplayName "$rule*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $rule -Direction Inbound -Action Allow -Protocol TCP `
    -LocalPort {GLANCES_PORT} -RemoteAddress LocalSubnet -Profile Any | Out-Null
$programs = {ps_list(programs)}
# Windows asks 'Allow access?' the first time a program without a rule listens. Dismissing
# it, or accepting it with only private networks ticked, leaves Block rules for public
# networks (the bar's USB link is one), and a Block rule beats every Allow rule. So the
# interpreter gets its own Allow rule before Glances first starts, and Block rules left
# on it by an earlier prompt go.
foreach ($program in $programs) {{
    New-NetFirewallRule -DisplayName "$rule - $(Split-Path $program -Leaf)" -Direction Inbound `
        -Action Allow -Program $program -Protocol TCP -LocalPort {GLANCES_PORT} `
        -RemoteAddress LocalSubnet -Profile Any | Out-Null
    Get-NetFirewallApplicationFilter |
        Where-Object {{ [Environment]::ExpandEnvironmentVariables($_.Program) -eq $program }} |
        Get-NetFirewallRule |
        Where-Object {{ $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' }} |
        Remove-NetFirewallRule
}}
"""


def open_firewall(state: dict, venv: Path) -> None:
    if IS_WINDOWS:
        programs = interpreter_programs(venv)
        if windows_rules_in_place(programs):
            state["firewall_rule"] = WINDOWS_FIREWALL_RULE
            ok("Firewall rules are in place from an earlier install")
            return
        note("Windows asks for permission to let the bar reach Glances on this computer. Allowing it")
        note(f"adds rules for TCP port {GLANCES_PORT}, from your local network only, for the Python running")
        note("Glances, and removes earlier Block rules for that Python, which would override them.")
        with working("Waiting for you to answer the Windows permission prompt"):
            changed = powershell(windows_firewall_script(programs), elevated=True).returncode == 0
        if changed and windows_rules_in_place(programs):
            state["firewall_rule"] = WINDOWS_FIREWALL_RULE
            ok("Firewall rules added")
        else:
            warn("The firewall was not changed. Over USB, and on networks Windows treats as public, the")
            warn("bar then cannot reach Glances. Run the installer again and allow the prompt, or ask")
            warn("an administrator of this PC to.")
        return

    if IS_MAC:
        tool = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        status = run([tool, "--getglobalstate"], check=False).stdout or ""
        if "enabled" not in status.lower():
            ok("macOS firewall is off, nothing to open")
            return
        python = os.path.realpath(venv_python(venv))
        listed = python in (run([tool, "--listapps"], check=False).stdout or "")
        apps = mac_firewall_apps(state)
        if listed and python in apps and "permitted" in (run([tool, "--getappblocked", python], check=False).stdout or ""):
            ok("Glances is allowed through the macOS firewall from an earlier install")
            return
        note("The macOS firewall is on. Allowing Glances to accept connections needs your password.")
        script = f"{tool} --add {shlex.quote(python)} && {tool} --unblockapp {shlex.quote(python)}"
        with working("Waiting for your password in the macOS dialog"):
            allowed = run_as_admin_mac(script)
        if allowed.returncode == 0:
            # An exception the user already had stays theirs: uninstall removes only those added here.
            if not listed and python not in apps:
                state["mac_firewall_apps"] = apps + [python]
            ok("Glances is allowed through the macOS firewall")
        else:
            warn("Glances was not added to the firewall. If the bar shows NO GLANCES, open")
            warn("System Settings > Network > Firewall > Options and allow incoming connections for python.")
        return

    if shutil.which("ufw"):
        note("If ufw is enabled, allow the bar to reach Glances with:")
        note(f"  sudo ufw allow from <your-network>/24 to any port {GLANCES_PORT} proto tcp")
    elif shutil.which("firewall-cmd"):
        note("If firewalld is running, allow the bar to reach Glances with:")
        note(f"  sudo firewall-cmd --permanent --add-port={GLANCES_PORT}/tcp && sudo firewall-cmd --reload")
    else:
        ok("No firewall changes needed")


def mac_firewall_apps(state: dict) -> list[str]:
    """Firewall exceptions the installer added; 1.0.x recorded one under another key."""
    apps = list(state.get("mac_firewall_apps") or [])
    if state.get("mac_firewall_app") and state["mac_firewall_app"] not in apps:
        apps.append(state["mac_firewall_app"])
    return apps


def run_as_admin_mac(script: str) -> subprocess.CompletedProcess:
    """Runs a shell command as root after macOS asks for the password. The command
    sits inside an AppleScript string, so its backslashes and quotes are escaped."""
    quoted = script.replace("\\", "\\\\").replace('"', '\\"')
    return run(["osascript", "-e", f'do shell script "{quoted}" with administrator privileges'], check=False)


def close_firewall(state: dict) -> None:
    apps = mac_firewall_apps(state)
    if IS_MAC and apps:
        tool = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        note("macOS asks for your password to remove the firewall exception the installer added.")
        with working("Waiting for your password in the macOS dialog"):
            removed = run_as_admin_mac(" ; ".join(f"{tool} --remove {shlex.quote(app)}" for app in apps))
        if removed.returncode == 0:
            ok("Firewall exception removed")
        else:
            warn("The firewall exception was not removed. Remove python in System Settings > Network >")
            warn("Firewall > Options if you like.")
        return
    if IS_WINDOWS and (state.get("firewall_rule") or windows_rules_exist()):
        rule = (state.get("firewall_rule") or WINDOWS_FIREWALL_RULE).replace("'", "''")
        note("Windows asks for permission to remove the firewall rules the installer added.")
        with working("Waiting for you to answer the Windows permission prompt"):
            removed = powershell(f"Get-NetFirewallRule -DisplayName '{rule}*' | Remove-NetFirewallRule", elevated=True)
        if removed.returncode == 0:
            ok("Removed the firewall rules")
        else:
            warn(f"The firewall rules \"{WINDOWS_FIREWALL_RULE}\" stay: the permission prompt was declined.")


# --------------------------------------------------------------------------- the bar


class Bar:
    """Talks to a BUSY Bar over its HTTP API."""

    def __init__(self, address: str, name: str = "") -> None:
        self.address = address
        self.name = name
        self.token: str | None = None

    @property
    def over_usb(self) -> bool:
        return self.address == USB_ADDRESS

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
        timeout: float = 15,
    ) -> bytes:
        for attempt in range(2):
            req = urllib.request.Request(f"http://{self.address}{path}", data=body, method=method)
            if body is not None:
                req.add_header("Content-Type", content_type)
            if self.token:
                req.add_header("X-API-Token", self.token)
            try:
                with DIRECT.open(req, timeout=timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                if error.code in (401, 403) and attempt == 0 and self.token is None:
                    self.token = self.ask_password()
                    continue
                detail = error.read().decode("utf-8", "replace").strip()
                raise BarError(error.code, detail) from None
            except OSError as error:
                raise SetupError(f"The bar at {self.address} did not answer ({error}).") from None
        raise SetupError("The bar refused the password.")

    def ask_password(self) -> str:
        print()
        note("The bar asks for its HTTP API password (Network > HTTP API in its web interface).")
        if not interactive():
            raise SetupError("The bar is password protected. Run the installer in a terminal to enter it.")
        password = getpass.getpass("      ?   Password: ")
        if not password:
            raise SetupError("No password entered.")
        return password

    def json(self, method: str, path: str, payload: object | None = None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else None
        raw = self.request(method, path, body)
        return json.loads(raw.decode()) if raw else {}


class BarError(SetupError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"The bar answered HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


def probe(address: str, timeout: float = 2) -> bool:
    try:
        with DIRECT.open(f"http://{address}/api/version", timeout=timeout) as r:
            return "api_semver" in json.loads(r.read().decode())
    except urllib.error.HTTPError as error:
        # A password-protected bar still answers, just not with the version.
        return error.code in (401, 403)
    except (OSError, ValueError):
        return False


def discover_bars(timeout: float = 3) -> list[tuple[str, str]]:
    """
    Finds bars on the local network over mDNS. Every bar advertises an `_http._tcp`
    service named `busybar-<serial>` with its display name in the TXT record. A
    query from an ordinary port gets direct (unicast) answers, so nothing has to
    bind port 5353 and this works next to the system's own mDNS responder.
    """
    query = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + encode_name("_http._tcp.local") + struct.pack(">HH", 12, 1)
    found: dict[str, tuple[str, str]] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.settimeout(0.3)
        start = time.time()
        sent = 0
        # mDNS is lossy and the first query often goes unanswered, so it is repeated.
        while time.time() < start + timeout:
            if sent < 3 and time.time() >= start + sent * 0.8:
                try:
                    sock.sendto(query, ("224.0.0.251", 5353))
                except OSError:
                    return []
                sent += 1
            try:
                packet, sender = sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                service, name = parse_bar_answer(packet)
            except (IndexError, struct.error, UnicodeDecodeError):
                continue
            if service:
                found[service] = (name or service, sender[0])
    return sorted(found.values())


def encode_name(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".") if p) + b"\0"


def read_name(packet: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    end = None
    for _ in range(64):
        length = packet[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:
            if end is None:
                end = offset + 2
            offset = struct.unpack(">H", packet[offset:offset + 2])[0] & 0x3FFF
            continue
        labels.append(packet[offset + 1:offset + 1 + length].decode("utf-8", "replace"))
        offset += 1 + length
    return ".".join(labels), end if end is not None else offset


def parse_bar_answer(packet: bytes) -> tuple[str, str]:
    """Returns (service instance, display name) if the answer is from a BUSY Bar."""
    counts = struct.unpack(">HHHH", packet[4:12])
    offset = 12
    for _ in range(counts[0]):
        _, offset = read_name(packet, offset)
        offset += 4
    service, display = "", ""
    for _ in range(sum(counts[1:])):
        name, offset = read_name(packet, offset)
        rtype, _, _, length = struct.unpack(">HHIH", packet[offset:offset + 10])
        offset += 10
        data = packet[offset:offset + length]
        if rtype == 12:  # PTR
            target, _ = read_name(packet, offset)
            if target.startswith("busybar-"):
                service = target.split(".")[0]
        elif rtype == 16 and name.startswith("busybar-"):  # TXT
            i = 0
            while i < len(data):
                entry = data[i + 1:i + 1 + data[i]].decode("utf-8", "replace")
                if entry.startswith("name="):
                    display = entry[5:]
                i += 1 + data[i]
        offset += length
    return service, display


def find_bar(requested: str | None, remembered: str | None) -> Bar:
    if requested:
        address = clean_address(requested)
        if not probe(address, timeout=5):
            raise SetupError(f"No BUSY Bar answers at {address}.")
        ok(f"Using the bar at {address}")
        return Bar(address)

    if probe(USB_ADDRESS):
        ok("Found a bar connected over USB")
        return Bar(USB_ADDRESS, "USB")

    with working("No bar on USB. Looking for bars on the network"):
        bars = [Bar(ip, name) for name, ip in discover_bars()]
    if remembered and remembered != USB_ADDRESS and all(b.address != remembered for b in bars):
        if probe(remembered):
            bars.append(Bar(remembered, "used last time"))

    if len(bars) == 1:
        ok(f"Found {bars[0].name} at {bars[0].address}")
        return bars[0]

    if len(bars) > 1:
        note("Found several bars:")
        for i, bar in enumerate(bars, 1):
            note(f"  {i}. {bar.name} ({bar.address})")
        choice = ask("Which one? Enter its number", "1")
        if choice.isdigit() and 1 <= int(choice) <= len(bars):
            return bars[int(choice) - 1]
        raise SetupError("No bar chosen.")

    note("No bar found automatically. Connect it with a USB cable, or enter its IP address")
    note("(your router's list of devices shows it).")
    address = clean_address(ask("Bar address (e.g. 192.168.1.42)"))
    if not address:
        raise SetupError("No bar to install on.")
    if not probe(address, timeout=5):
        raise SetupError(
            f"No BUSY Bar answers at {address}. Check the address, and that HTTP API access over Wi-Fi "
            "is on in the bar's web interface (Network > HTTP API)."
        )
    return Bar(address)


def clean_address(text: str) -> str:
    text = text.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.split("/", 1)[0]


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(".")[:3] if part.isdigit())


def check_bar(bar: Bar) -> str:
    """Checks the firmware, and returns the bar's Wi-Fi address, or "" when it has none."""
    api = bar.json("GET", "/api/version").get("api_semver", "0.0.0")
    firmware = bar.json("GET", "/api/status/firmware").get("version", "?")
    if version_tuple(api) < MIN_API:
        raise SetupError(
            f"The bar runs firmware {firmware} (API {api}). Host Load needs firmware 1.3.1 or newer: "
            "update it in the Firmware tab of the bar's web interface, then run the installer again."
        )
    name = bar.json("GET", "/api/name").get("name", "")
    if name:
        bar.name = name
    ok(f"{bar.name or 'BUSY Bar'}: firmware {firmware}, API {api}")

    wifi = bar.json("GET", "/api/wifi/status")
    address = (wifi.get("ip_config") or {}).get("address", "")
    if wifi.get("state") == "connected" and address:
        ok(f"Bar is on Wi-Fi \"{wifi.get('ssid', '?')}\" as {address}")
        return address
    if bar.over_usb:
        note("The bar is not on Wi-Fi; it can still read Glances over the USB cable.")
        return ""
    # It answered over the network, so it is on it, whatever the status says while
    # Wi-Fi reconnects (state "reconnecting", no ip_config).
    return bar.address


def address_towards(ip: str) -> str:
    """
    The address this computer uses to reach `ip`, which is the one a device there can
    use to reach back. Asking the routing table is more reliable than listing
    interfaces: it skips VPNs, virtual adapters and links that lead elsewhere.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((ip, 9))
        return sock.getsockname()[0]


def candidate_addresses(bar: Bar, bar_wifi: str) -> list[tuple[str, str]]:
    """
    Addresses the bar might reach this computer on, best first, with the way there.
    The network comes first when both are on the same one, because it keeps working
    when the cable is unplugged. A bar plugged in by USB can always reach the
    computer over the cable, whatever networks the two are on.
    """
    lan = address_towards(bar_wifi) if bar_wifi else ""
    same_network = bool(lan) and lan.split(".")[:3] == bar_wifi.split(".")[:3]
    usb = address_towards(USB_ADDRESS) if bar.over_usb else ""

    candidates = []
    if same_network:
        candidates.append((lan, "network"))
    elif lan:
        note(f"This computer ({lan}) and the bar ({bar_wifi}) are on different networks.")
    if usb:
        candidates.append((usb, "USB"))
    if lan and not same_network:
        candidates.append((lan, "network"))
    if not candidates:
        raise SetupError(
            "The bar cannot reach this computer: connect it with a USB cable, or put both on the same network."
        )
    return candidates


# --------------------------------------------------------------------------- the app


def find_package(requested: str | None) -> Path:
    if requested:
        path = Path(requested).expanduser()
        if not path.exists():
            raise SetupError(f"Package not found: {requested}")
        return path

    # Next to this script (a release bundle), or a local build in a checkout.
    here = Path(__file__).resolve().parent
    for candidate in (here / f"{APP_ID}.tgz", here.parent / "dist" / f"{APP_ID}.tgz"):
        if candidate.exists():
            note(f"Using the package at {candidate.relative_to(here.parent)}")
            return candidate

    target = Path(tempfile.mkdtemp()) / f"{APP_ID}.tgz"
    with working(f"Downloading {APP_NAME} from GitHub"):
        download(PACKAGE_URL, target)
    return target


def download(url: str, target: Path) -> None:
    """
    curl first: it uses the system's certificates, while a python.org Python on
    macOS has none until its 'Install Certificates' step is run, and every HTTPS
    request from it fails. Python's own client is the fallback.
    """
    curl = shutil.which("curl")
    if curl and run([curl, "-fsSL", "--retry", "2", "--max-time", "120", "-o", str(target), url], check=False).returncode == 0:
        return
    try:
        with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as out:
            shutil.copyfileobj(response, out)
    except OSError as error:
        raise SetupError(f"Could not download {url} ({error}).") from None


def install_app(bar: Bar, package: Path) -> None:
    try:
        with working(f"Uploading {APP_NAME} to the bar"):
            raw = bar.request("POST", "/api/apps/stage", package.read_bytes(), "application/octet-stream", timeout=60)
        staged = json.loads(raw.decode())
    except BarError as error:
        if error.status == 409:
            raise SetupError("The bar's firmware is too old for this app. Update it to 1.3.1 or newer.") from None
        raise
    key = staged.get("install_key")
    if key is None:
        raise SetupError(f"The bar did not accept the package: {staged}")
    with working(f"Installing {APP_NAME} on the bar"):
        bar.request("POST", f"/api/apps/install?install_key={key}", timeout=60)
    previous = (staged.get("installed") or {}).get("version")
    version = (staged.get("staged") or {}).get("version", "?")
    ok(f"{APP_NAME} {version} installed" + (f" (replaced {previous})" if previous else ""))


def configure_app(bar: Bar, host: str) -> None:
    doc = bar.json("GET", f"/api/apps/settings?app_id={APP_ID}")
    values = doc.setdefault("values", {})
    values["host"] = host
    bar.json("PUT", f"/api/apps/settings?app_id={APP_ID}", doc)
    ok(f"Computer address set to {host}")


def launch_app(bar: Bar) -> bool:
    try:
        bar.request("POST", "/api/apps/quit")
    except SetupError:
        pass  # nothing was running
    try:
        bar.request("POST", f"/api/apps/launch?app_id={APP_ID}")
    except BarError as error:
        if error.status == 409:
            warn(f"Another app has the screen. Open {APP_NAME} from the bar's Apps menu.")
            return False
        raise
    ok(f"{APP_NAME} launched")
    return True


def graphs_on_screen(bar: Bar) -> bool:
    """
    Reads the bar's front screen: 72x16 pixels, 3 bytes each in BGR order. While the
    graphs run, the gap row under the CPU label is the dark red of the CPU half; the
    NO GLANCES message is on black.
    """
    raw = base64.b64decode(bar.request("GET", "/api/screen?display=0"))
    x, y = 17, 5
    blue, green, red = raw[(y * 72 + x) * 3:(y * 72 + x) * 3 + 3]
    return red > 25 and red > 3 * max(green, blue)


def verify(bar: Bar, seconds: int = 25) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(3)
        try:
            if graphs_on_screen(bar):
                return True
        except (SetupError, ValueError):
            return False
    return False


# --------------------------------------------------------------------------- commands


def install(args: argparse.Namespace) -> None:
    total = 6
    state = load_state()
    if os.environ.get("HOSTLOAD_UV_INSTALLED") == "1":
        state["installed_uv"] = True
        # Right away: if a later step fails, the next run finds uv already there and
        # would take it for the user's own, so uninstall would never offer to remove it.
        save_state(state)

    step(1, total, "Glances on this computer")
    # Ours if an earlier run installed it, or if nothing answers yet. A Glances the
    # user runs themselves stays theirs on every later run, not just the first.
    ours = state.get("glances") == "ours" or not glances_answers()
    new_env: Path | None = None
    if not ours:
        ok(f"Glances is already running on port {GLANCES_PORT}, using it")
        state["glances"] = "external"
    else:
        if state.get("glances") != "ours" and port_taken():
            raise SetupError(port_taken_message())
        # Built beside the running one, which keeps serving the bar until the swap.
        new_env = build_glances(find_uv())
    save_state(state)

    step(2, total, "Firewall")
    if not ours:
        ok("Using your own Glances, firewall left as it is")
    elif args.no_firewall:
        ok("Firewall left as it is (--no-firewall)")
    else:
        # Before Glances first listens, so Windows has a rule for it and asks nothing.
        open_firewall(state, new_env)
        save_state(state)

    step(3, total, "Starting Glances")
    if not ours:
        ok("Your own Glances keeps running as it is")
    else:
        with working("Starting Glances"):
            stop_glances_service()
            if not wait_port_free():
                raise SetupError(port_taken_message())
            replace_dir(new_env, venv_dir())
            state["service"] = start_glances_service(state)
            state["glances"] = "ours"
            save_state(state)
            started = wait_for_glances()
        if not started:
            raise SetupError(f"Glances did not start. The end of {log_path()}:\n{log_tail()}")
        if state["service"] == "autostart":
            ok(f"Glances is running on port {GLANCES_PORT}")
            note("There is no systemd user session here, so it starts again when you log in to the")
            note(f"desktop. Without a desktop, start it with: {shlex.join(glances_command('supervise'))}")
        elif state.get("enabled_linger") or state["service"] != "systemd":
            ok(f"Glances is running on port {GLANCES_PORT} and starts " + ("at boot" if state["service"] == "systemd" else "at login"))
        else:
            ok(f"Glances is running on port {GLANCES_PORT} and starts at login")

    step(4, total, "BUSY Bar")
    bar = find_bar(args.bar, state.get("bar"))
    bar_wifi = check_bar(bar)
    candidates = candidate_addresses(bar, bar_wifi)
    state["bar"] = bar_wifi if bar.over_usb and bar_wifi else bar.address
    save_state(state)

    step(5, total, f"{APP_NAME} app")
    install_app(bar, find_package(args.package))

    step(6, total, "Connecting the bar to this computer")
    connected: tuple[str, str] | None = None
    for host, way in candidates:
        configure_app(bar, host)
        if not launch_app(bar):
            break
        route = "over the USB cable" if way == "USB" else "over the network"
        with working(f"Waiting for the graphs on the bar's screen ({route})"):
            shown = verify(bar)
        if shown:
            connected = (host, way)
            break
        warn(f"The bar cannot reach Glances at {host}.")

    if connected:
        ok("The graphs are live")
        if connected[1] == "USB":
            note("The bar reads this computer over the USB cable, so keep it plugged in. To use Wi-Fi")
            note("instead, put this computer on the bar's Wi-Fi network and run the installer again.")
    else:
        if len(candidates) > 1:
            configure_app(bar, candidates[0][0])
        warn("The bar does not show the graphs yet. Usual causes:")
        warn(f"- a firewall on this computer blocks port {GLANCES_PORT} (see step 2);")
        warn("- the bar and this computer are on different networks: connect the bar by USB,")
        warn("  or put both on the same Wi-Fi.")
        warn("The bar keeps retrying; check its screen in a minute.")

    uninstaller = "setup\\Uninstall-Windows.bat" if IS_WINDOWS else "setup/Uninstall-macOS.command" if IS_MAC else "./install.sh --uninstall"
    print()
    print(cyan("  " + RULE))
    if connected:
        print("    " + bold(f"Done! {bar.name or 'Your BUSY Bar'} now shows this computer's CPU and RAM."))
    else:
        print("    " + bold(f"{APP_NAME} is installed, but the bar does not show the graphs yet."))
    print()
    print(f"    {dim('Settings ')}  the bar's web interface > Apps > Host Load")
    print(f"    {dim('IP change')}  run this installer again")
    print(f"    {dim('Uninstall')}  {uninstaller}, or the installer with --uninstall")
    print(cyan("  " + RULE))


def port_owner() -> str:
    """The program listening on the Glances port, as "name (PID n)", or "" if unknown."""
    try:
        if IS_WINDOWS:
            script = (f"$c = Get-NetTCPConnection -LocalPort {GLANCES_PORT} -State Listen -ErrorAction SilentlyContinue | "
                      "Select-Object -First 1; if ($c) { $p = Get-Process -Id $c.OwningProcess; "
                      "\"$($p.ProcessName) (PID $($p.Id))\" }")
            return (powershell(script).stdout or "").strip()
        if shutil.which("lsof"):
            pid = (run(["lsof", "-nP", f"-iTCP:{GLANCES_PORT}", "-sTCP:LISTEN", "-t"], check=False).stdout or "").split()
            if pid:
                name = (run(["ps", "-o", "comm=", "-p", pid[0]], check=False).stdout or "").strip()
                return f"{os.path.basename(name) or 'a program'} (PID {pid[0]})"
    except (OSError, ValueError):
        pass
    return ""


def port_taken_message() -> str:
    owner = port_owner()
    return (f"Port {GLANCES_PORT} is used by {owner or 'another program'}"
            f"{'' if owner else ' (an older Glances?)'}. Stop it and run the installer again.")


def log_tail(lines: int = 15) -> str:
    try:
        text = log_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no log)"
    return "\n".join("      " + line for line in text.splitlines()[-lines:])


def force_remove(function, path, _info) -> None:
    """rmtree's error handler: Windows refuses to delete read-only files until they are writable."""
    try:
        os.chmod(path, 0o700)
        function(path)
    except OSError:
        pass


# The launcher removes uv after this script exits when it returns this code: uv
# cannot delete itself while it is running the script.
REMOVE_UV_EXIT = 10


def uninstall(args: argparse.Namespace) -> int:
    state = load_state()

    step(1, 3, f"{APP_NAME} on the bar")
    remembered = args.bar or (USB_ADDRESS if probe(USB_ADDRESS) else state.get("bar"))
    if remembered and probe(remembered):
        bar = Bar(remembered)
        try:
            bar.request("DELETE", f"/api/apps?app_id={APP_ID}")
            ok(f"Removed {APP_NAME} from the bar at {remembered}")
        except BarError as error:
            if error.status == 404:
                ok("The app is not installed on the bar")
            else:
                warn(str(error))
    else:
        warn("No bar found. Remove the app in the bar's web interface: Apps > Host Load > Delete app.")

    step(2, 3, "Glances")
    if state.get("glances") == "external":
        ok("Your own Glances was used; left untouched")
    else:
        with working("Stopping Glances"):
            remove_glances_service(state)
        ok("Stopped Glances and removed it from login items")
        close_firewall(state)

    step(3, 3, "Files")
    # Windows releases the files of a process a moment after it is stopped.
    for _ in range(10):
        shutil.rmtree(data_dir(), onerror=force_remove)
        if not data_dir().exists():
            break
        time.sleep(1)
    if data_dir().exists():
        warn(f"Could not remove everything in {data_dir()}; delete it by hand after a restart.")
    else:
        ok(f"Removed {data_dir()}")
    remove_uv = bool(state.get("installed_uv")) and confirm(
        "uv was installed along with Host Load. Remove it too?", default=True
    )

    print()
    print(cyan("  " + RULE))
    print("    " + bold(f"{APP_NAME} is uninstalled."))
    print(cyan("  " + RULE))
    return REMOVE_UV_EXIT if remove_uv else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=f"Install or remove {APP_NAME} for the BUSY Bar.")
    parser.add_argument("action", choices=["install", "uninstall"], nargs="?", default="install")
    parser.add_argument("--bar", help="the bar's address; found automatically when left out")
    parser.add_argument("--package", help="app package (.tgz) to install instead of the latest release")
    parser.add_argument("--no-firewall", action="store_true",
                        help="leave the firewall as it is, for computers whose firewall is managed elsewhere")
    args = parser.parse_args()
    # Printing a path in letters the console's code page lacks must not stop the installer.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    if args.action == "install":
        banner(f"{APP_NAME} for BUSY Bar", "Live CPU and RAM graphs of this computer on your BUSY Bar")
    else:
        banner(f"{APP_NAME} for BUSY Bar", "Removing everything the installer added")
    try:
        if args.action == "install":
            install(args)
        else:
            return uninstall(args)
    except SetupError as error:
        print()
        warn(str(error))
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
