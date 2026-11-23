#!/usr/bin/env bash
# Host Load installer for macOS and Linux.
#
# From a downloaded copy:   ./install.sh            (or double-click Install-macOS.command)
# Without downloading:      curl -fsSL https://raw.githubusercontent.com/mrlunev/busybar-host-load/main/install.sh | bash
# To remove everything:     ./install.sh --uninstall
#
# All it does itself is make sure uv (https://docs.astral.sh/uv/) is available; uv
# then runs setup/hostload_setup.py with a Python it finds or fetches, so nothing
# else has to be installed beforehand.
set -euo pipefail

RAW_BASE="https://raw.githubusercontent.com/mrlunev/busybar-host-load/main"
# setup/hostload_setup.py exits with this code when uv should go too.
REMOVE_UV_EXIT=10
# Oldest uv with everything the installer uses (remote scripts, --exclude-newer,
# a separate folder for downloaded Pythons).
MIN_UV="0.5.0"

action="install"
args=()
for arg in "$@"; do
  case "$arg" in
    --uninstall|uninstall) action="uninstall" ;;
    *) args+=("$arg") ;;
  esac
done

say() { printf '  %s\n' "$*"; }

find_uv() {
  if command -v uv >/dev/null 2>&1; then command -v uv; return 0; fi
  # Where uv's own installer puts it: UV_INSTALL_DIR, XDG_BIN_HOME, ~/.local/bin, ~/.cargo/bin.
  for candidate in "${UV_INSTALL_DIR:-/nonexistent}/uv" "${XDG_BIN_HOME:-/nonexistent}/uv" \
                   "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -x "$candidate" ]; then echo "$candidate"; return 0; fi
  done
  return 1
}

uv_too_old() {
  version="$("$1" --version 2>/dev/null | awk '{print $2}')"
  [ -z "$version" ] && return 0
  [ "$(printf '%s\n%s\n' "$MIN_UV" "$version" | sort -t. -k1,1n -k2,2n -k3,3n | head -n1)" != "$MIN_UV" ]
}

remove_uv() {
  say "Removing uv..."
  "$uv_bin" cache clean >/dev/null 2>&1 || true
  for dir in "$("$uv_bin" python dir 2>/dev/null || true)" "$("$uv_bin" tool dir 2>/dev/null || true)" \
             "$("$uv_bin" cache dir 2>/dev/null || true)"; do
    # Only uv's own folders, never anything that does not say so.
    case "$dir" in *uv*) rm -rf "$dir" ;; esac
  done
  rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/uv/uv-receipt.json"
  bin_dir="$(dirname "$uv_bin")"
  rm -f "$bin_dir/uv" "$bin_dir/uvx"
  say "uv removed."
}

uv_bin="$(find_uv || true)"
if [ -z "$uv_bin" ]; then
  if [ "$action" = "uninstall" ]; then
    say "uv is not installed, so neither is Host Load's Glances. Nothing to remove on this computer."
    exit 0
  fi
  say "Installing uv, a small tool that runs this installer (https://docs.astral.sh/uv/)..."
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
  uv_bin="$(find_uv)"
  say "uv is ready."
  export HOSTLOAD_UV_INSTALLED=1
fi
if uv_too_old "$uv_bin"; then
  say "The uv on this computer is older than $MIN_UV; updating it..."
  "$uv_bin" self update >/dev/null 2>&1 || true
  if uv_too_old "$uv_bin"; then
    say "Could not update uv at $uv_bin. Update it the way it was installed (for example"
    say "'brew upgrade uv' or 'pipx upgrade uv'), then run this installer again."
    exit 1
  fi
fi
export HOSTLOAD_UV="$uv_bin"
# The installer itself runs on a Python already on this computer when there is one;
# uv downloads one only if not. (Glances gets a Python of its own either way.)
export UV_PYTHON_PREFERENCE=system
# On a Mac without the command line developer tools, /usr/bin/python3 is a stub
# that opens an "install the tools" dialog when run: use a downloaded Python.
if [ "$(uname -s)" = "Darwin" ] && ! xcode-select -p >/dev/null 2>&1; then
  export UV_PYTHON_PREFERENCE=only-managed
fi

if ! "$uv_bin" python find '>=3.9' >/dev/null 2>&1 || [ "$UV_PYTHON_PREFERENCE" = "only-managed" ]; then
  say "No Python on this computer yet; downloading one for the installer..."
  "$uv_bin" python install 3.12
fi

# Run as a file, use the copy next to it; piped from curl, fetch it, whatever
# folder the terminal is in (an old checkout there must not run instead).
script="$RAW_BASE/setup/hostload_setup.py"
source_file="${BASH_SOURCE[0]:-}"
if [ -n "$source_file" ] && [ -f "$source_file" ]; then
  here="$(cd "$(dirname "$source_file")" && pwd)"
  if [ -f "$here/setup/hostload_setup.py" ]; then script="$here/setup/hostload_setup.py"; fi
fi

# Piped from curl, stdin is this script itself; questions must read the terminal.
# Without one (an agent, CI, cron) /dev/tty exists but cannot be opened, and the
# installer runs on its defaults.
input=/dev/stdin
if [ ! -t 0 ]; then
  input=/dev/null
  if (exec </dev/tty) 2>/dev/null; then input=/dev/tty; fi
fi

set +e
"$uv_bin" run --quiet --no-project "$script" "$action" ${args[@]+"${args[@]}"} <"$input"
code=$?
set -e

if [ "$code" -eq "$REMOVE_UV_EXIT" ]; then
  remove_uv
  code=0
fi
exit "$code"
