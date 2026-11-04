# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
Host Load installer: sets up this computer and a BUSY Bar in one go.

What `install` does:

1. Installs Glances into its own virtual environment, limited to the CPU and
   memory plugins, and starts it in web server mode at every login.
2. Opens port 61208 in the firewall where that needs doing.
3. Finds the BUSY Bar: over USB, on the network via mDNS, or by address.
4. Installs the Host Load app on the bar, writes this computer's address into
   the app's settings and launches it.
5. Checks the bar's screen to confirm the graphs are coming in.

`uninstall` reverses all of it. Nothing outside the app's own data folder, the
login item and (on Windows) one firewall rule is touched.

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
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_ID = "app.mrlunev.hostload"
APP_NAME = "Host Load"
REPO = "mrlunev/busybar-host-load"
PACKAGE_URL = f"https://github.com/{REPO}/releases/latest/download/{APP_ID}.tgz"

# Glances 4 serves the /api/4 REST API the app reads.
GLANCES_SPEC = "glances[web]>=4.5,<5"
GLANCES_PORT = 61208
GLANCES_ARGS = [
    "-w",
    "--disable-webui",
    "-p", str(GLANCES_PORT),
    # Only what the bar shows is exposed to the network, nothing about processes or disks.
    "--disable-plugin", "all",
    "--enable-plugin", "cpu,mem",
]
PYTHON_FOR_GLANCES = "3.12"

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


def step(n: int, total: int, text: str) -> None:
    print(f"\n[{n}/{total}] {text}")


def ok(text: str) -> None:
    print(f"      ok  {text}")


def note(text: str) -> None:
    print(f"          {text}")


def warn(text: str) -> None:
    print(f"      !!  {text}")


def interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def ask(question: str, default: str = "") -> str:
    if not interactive():
        return default
    suffix = f" [{default}]" if default else ""
    answer = input(f"      ?   {question}{suffix}: ").strip()
    return answer or default


def confirm(question: str, default: bool = True) -> bool:
    answer = ask(f"{question} (y/n)", "y" if default else "n").lower()
    return answer.startswith("y")


# --------------------------------------------------------------------------- paths and state


def data_dir() -> Path:
    """Where the Glances environment, logs and the install record live."""
    home = Path.home()
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    elif IS_MAC:
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    return base / SERVICE_NAME


def venv_dir() -> Path:
    return data_dir() / "glances"


def glances_command() -> list[str]:
    """The command that runs Glances from its environment, without a console window on Windows."""
    if IS_WINDOWS:
        scripts = venv_dir() / "Scripts"
        pythonw = scripts / "pythonw.exe"
        if pythonw.exists():
            return [str(pythonw), "-m", "glances", *GLANCES_ARGS]
        return [str(scripts / "glances.exe"), *GLANCES_ARGS]
    return [str(venv_dir() / "bin" / "glances"), *GLANCES_ARGS]


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


def run(command: list[str], check: bool = True, quiet: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE if quiet else None,
        stderr=subprocess.STDOUT if quiet else None,
        text=True,
    )
    if check and result.returncode != 0:
        output = (result.stdout or "").strip()
        raise SetupError(f"'{Path(command[0]).name} {' '.join(command[1:3])}' failed:\n{output}")
    return result


# --------------------------------------------------------------------------- Glances


def glances_answers(host: str = "127.0.0.1", timeout: float = 2) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{GLANCES_PORT}/api/4/cpu/total", timeout=timeout) as r:
            return "total" in json.loads(r.read().decode())
    except (OSError, ValueError):
        return False


def port_taken() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", GLANCES_PORT)) == 0


def install_glances(uv: str) -> None:
    venv = venv_dir()
    if not venv.exists():
        note(f"Creating a Python {PYTHON_FOR_GLANCES} environment (uv downloads Python if needed)")
        run([uv, "venv", "--quiet", "--python", PYTHON_FOR_GLANCES, str(venv)])
    python = venv / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
    note(f"Installing {GLANCES_SPEC}")
    run([uv, "pip", "install", "--quiet", "--upgrade", "--python", str(python), GLANCES_SPEC])
    version = run([str(python), "-m", "glances", "--version"], check=False).stdout or ""
    ok(version.strip().splitlines()[0] if version.strip() else "Glances installed")


def stop_glances_service() -> None:
    """Stops our Glances, whichever way it was started. Errors are fine: it may not be running."""
    if IS_MAC:
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], check=False)
    elif IS_LINUX:
        if shutil.which("systemctl"):
            run(["systemctl", "--user", "stop", f"{SERVICE_NAME}.service"], check=False)
        run(["pkill", "-f", str(venv_dir())], check=False)
    elif IS_WINDOWS:
        venv = str(venv_dir()).replace("'", "''")
        run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
                f"'*{venv}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}",
            ],
            check=False,
        )


def start_glances_service() -> str:
    """Registers Glances to start at login and starts it now. Returns how it was registered."""
    command = glances_command()
    log = data_dir() / "glances.log"

    if IS_MAC:
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        with plist.open("wb") as f:
            plistlib.dump(
                {
                    "Label": LAUNCHD_LABEL,
                    "ProgramArguments": command,
                    "RunAtLoad": True,
                    "KeepAlive": True,
                    "StandardOutPath": str(log),
                    "StandardErrorPath": str(log),
                },
                f,
            )
        run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
        return "launchd"

    if IS_LINUX and systemd_user_available():
        unit = Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        exec_start = " ".join(quote_systemd(part) for part in command)
        unit.write_text(
            "[Unit]\n"
            "Description=Glances for the BUSY Bar Host Load app\n"
            "After=network-online.target\n\n"
            "[Service]\n"
            f"ExecStart={exec_start}\n"
            "Restart=on-failure\n"
            "RestartSec=5\n\n"
            "[Install]\n"
            "WantedBy=default.target\n",
            encoding="utf-8",
        )
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.service"])
        return "systemd"

    if IS_WINDOWS:
        import winreg  # only on Windows

        line = subprocess.list2cmdline(command)
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        with key:
            winreg.SetValueEx(key, WINDOWS_RUN_VALUE, 0, winreg.REG_SZ, line)
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        subprocess.Popen(command, creationflags=flags, close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "registry"

    # Linux without a systemd user session: run it now, without autostart.
    with log.open("ab") as out:
        subprocess.Popen(command, stdout=out, stderr=out, start_new_session=True)
    return "manual"


def remove_glances_service() -> None:
    stop_glances_service()
    if IS_MAC:
        (Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist").unlink(missing_ok=True)
    elif IS_LINUX:
        unit = Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"
        if unit.exists():
            run(["systemctl", "--user", "disable", f"{SERVICE_NAME}.service"], check=False)
            unit.unlink()
            run(["systemctl", "--user", "daemon-reload"], check=False)
    elif IS_WINDOWS:
        import winreg

        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_SET_VALUE,
            )
            with key:
                winreg.DeleteValue(key, WINDOWS_RUN_VALUE)
        except OSError:
            pass


def systemd_user_available() -> bool:
    if not shutil.which("systemctl"):
        return False
    result = run(["systemctl", "--user", "is-system-running"], check=False)
    return (result.stdout or "").strip() not in ("", "offline")


def quote_systemd(part: str) -> str:
    return f'"{part}"' if " " in part else part


def wait_for_glances(seconds: int = 45) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if glances_answers():
            return True
        time.sleep(1)
    return False


# --------------------------------------------------------------------------- firewall


def open_firewall(state: dict) -> None:
    if IS_WINDOWS:
        exists = run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={WINDOWS_FIREWALL_RULE}"],
            check=False,
        )
        if exists.returncode == 0:
            ok("Firewall rule is already in place")
            return
        note("Windows asks for permission to let the bar reach Glances on this computer.")
        note("Allowing it adds one rule: TCP port 61208, from your local network only.")
        args = (
            "advfirewall firewall add rule "
            f'name="{WINDOWS_FIREWALL_RULE}" dir=in action=allow protocol=TCP '
            f"localport={GLANCES_PORT} remoteip=localsubnet profile=any"
        )
        elevated = run(
            [
                "powershell", "-NoProfile", "-Command",
                f"Start-Process netsh -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList '{args}'",
            ],
            check=False,
        )
        if elevated.returncode == 0 and run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={WINDOWS_FIREWALL_RULE}"],
            check=False,
        ).returncode == 0:
            state["firewall_rule"] = WINDOWS_FIREWALL_RULE
            ok("Firewall rule added")
        else:
            warn("The firewall rule was not added. If the bar shows NO GLANCES, run the installer again")
            warn("and allow the permission prompt, or allow TCP port 61208 in Windows Defender Firewall.")
        return

    if IS_MAC:
        tool = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        status = run([tool, "--getglobalstate"], check=False).stdout or ""
        if "enabled" not in status.lower():
            ok("macOS firewall is off, nothing to open")
            return
        python = os.path.realpath(venv_dir() / "bin" / "python")
        note("The macOS firewall is on. Allowing Glances to accept connections needs your password.")
        script = f"{tool} --add '{python}' && {tool} --unblockapp '{python}'"
        allowed = run(
            ["osascript", "-e", f'do shell script "{script}" with administrator privileges'],
            check=False,
        )
        if allowed.returncode == 0:
            ok("Glances is allowed through the macOS firewall")
        else:
            warn("Glances was not added to the firewall. If the bar shows NO GLANCES, open")
            warn("System Settings > Network > Firewall > Options and allow incoming connections for python.")
        return

    if shutil.which("ufw"):
        note("If ufw is enabled, allow the bar to reach Glances with:")
        note(f"  sudo ufw allow from <your-network>/24 to any port {GLANCES_PORT} proto tcp")
    else:
        ok("No firewall changes needed")


def close_firewall(state: dict) -> None:
    if IS_WINDOWS and state.get("firewall_rule"):
        run(
            [
                "powershell", "-NoProfile", "-Command",
                "Start-Process netsh -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList "
                f"'advfirewall firewall delete rule name=\"{state['firewall_rule']}\"'",
            ],
            check=False,
        )


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
                with urllib.request.urlopen(req, timeout=timeout) as response:
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
        with urllib.request.urlopen(f"http://{address}/api/version", timeout=timeout) as r:
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

    note("No bar on USB. Looking for bars on the network...")
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
    """Checks firmware and Wi-Fi, and returns the bar's address on the Wi-Fi network."""
    api = bar.json("GET", "/api/version").get("api_semver", "0.0.0")
    firmware = bar.json("GET", "/api/status/firmware").get("version", "?")
    if version_tuple(api) < MIN_API:
        raise SetupError(
            f"The bar runs firmware {firmware} (API {api}). Host Load needs firmware 1.3.1 or newer: "
            "update it in the Firmware tab of the bar's web interface, then run the installer again."
        )
    ok(f"Firmware {firmware}, API {api}")

    wifi = bar.json("GET", "/api/wifi/status")
    address = (wifi.get("ip_config") or {}).get("address", "")
    if wifi.get("state") != "connected" or not address:
        raise SetupError(
            "The bar is not connected to Wi-Fi. It reads Glances over Wi-Fi, so connect it to the same "
            "network as this computer (Network tab of its web interface), then run the installer again."
        )
    ok(f"Bar is on Wi-Fi \"{wifi.get('ssid', '?')}\" as {address}")
    return address


def computer_address(bar_wifi_address: str) -> str:
    """
    The address this computer uses to reach the bar's network, which is the one the
    bar can use to reach back. Asking the routing table is more reliable than
    listing interfaces: it skips VPNs, Docker bridges and the USB link to the bar.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((bar_wifi_address, 9))
        mine = sock.getsockname()[0]
    if mine.split(".")[:3] != bar_wifi_address.split(".")[:3]:
        warn(f"This computer ({mine}) and the bar ({bar_wifi_address}) look like they are on different networks.")
        warn("If the bar shows NO GLANCES, put both on the same Wi-Fi (guest networks often isolate devices).")
    return mine


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

    note("Downloading the latest release from GitHub")
    target = Path(tempfile.mkdtemp()) / f"{APP_ID}.tgz"
    try:
        urllib.request.urlretrieve(PACKAGE_URL, target)
    except OSError as error:
        raise SetupError(f"Could not download {PACKAGE_URL} ({error}).") from None
    return target


def install_app(bar: Bar, package: Path) -> None:
    try:
        raw = bar.request("POST", "/api/apps/stage", package.read_bytes(), "application/octet-stream", timeout=60)
        staged = json.loads(raw.decode())
    except BarError as error:
        if error.status == 409:
            raise SetupError("The bar's firmware is too old for this app. Update it to 1.3.1 or newer.") from None
        raise
    key = staged.get("install_key")
    if key is None:
        raise SetupError(f"The bar did not accept the package: {staged}")
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
    total = 5
    state = load_state()
    if os.environ.get("HOSTLOAD_UV_INSTALLED") == "1":
        state["installed_uv"] = True

    step(1, total, "Glances on this computer")
    if glances_answers() and not state.get("glances"):
        ok(f"Glances is already running on port {GLANCES_PORT}, using it")
        state["glances"] = "external"
    else:
        if state.get("glances") == "ours":
            stop_glances_service()
        elif port_taken():
            raise SetupError(
                f"Port {GLANCES_PORT} is used by another program (an older Glances?). "
                "Stop it and run the installer again."
            )
        install_glances(find_uv())
        state["service"] = start_glances_service()
        state["glances"] = "ours"
        save_state(state)
        if not wait_for_glances():
            raise SetupError(f"Glances did not start. See {data_dir() / 'glances.log'}")
        if state["service"] == "manual":
            ok(f"Glances is running on port {GLANCES_PORT} (no systemd user session: it will not start at login)")
        else:
            ok(f"Glances is running on port {GLANCES_PORT} and starts at login")

    step(2, total, "Firewall")
    if state.get("glances") == "ours":
        open_firewall(state)
    else:
        ok("Using your own Glances, firewall left as it is")
    save_state(state)

    step(3, total, "BUSY Bar")
    bar = find_bar(args.bar, state.get("bar"))
    bar_wifi = check_bar(bar)
    host = computer_address(bar_wifi)
    state["bar"] = bar.address if not bar.over_usb else bar_wifi
    save_state(state)

    step(4, total, f"{APP_NAME} app")
    install_app(bar, find_package(args.package))
    configure_app(bar, host)

    step(5, total, "Checking the bar's screen")
    if launch_app(bar) and verify(bar):
        ok("The graphs are live")
        print(f"\nDone. {APP_NAME} shows this computer's CPU and RAM on the bar.")
    else:
        warn("The bar does not show the graphs yet. Usual causes:")
        warn("- a firewall on this computer blocks port 61208 (see step 2);")
        warn("- the bar and this computer are on different networks.")
        warn("The bar keeps retrying; check its screen in a minute.")

    print(
        "\nSettings live in the bar's web interface: Apps > Host Load."
        "\nIf this computer's IP address changes, run the installer again."
        "\nTo remove everything, run the uninstaller (see README)."
    )


def uninstall(args: argparse.Namespace) -> None:
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
        remove_glances_service()
        close_firewall(state)
        ok("Stopped Glances and removed it from login items")

    step(3, 3, "Files")
    uv_installed = state.get("installed_uv")
    shutil.rmtree(data_dir(), ignore_errors=True)
    ok(f"Removed {data_dir()}")
    if uv_installed:
        note("uv was installed along with Host Load. Keep it, or remove it as described at")
        note("https://docs.astral.sh/uv/getting-started/installation/#uninstallation")

    print(f"\n{APP_NAME} is uninstalled.")


def main() -> int:
    parser = argparse.ArgumentParser(description=f"Install or remove {APP_NAME} for the BUSY Bar.")
    parser.add_argument("action", choices=["install", "uninstall"], nargs="?", default="install")
    parser.add_argument("--bar", help="the bar's address; found automatically when left out")
    parser.add_argument("--package", help="app package (.tgz) to install instead of the latest release")
    args = parser.parse_args()

    print(f"BUSY Bar {APP_NAME} {args.action}er")
    try:
        if args.action == "install":
            install(args)
        else:
            uninstall(args)
    except SetupError as error:
        print(f"\n      !!  {error}")
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
