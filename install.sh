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
  for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -x "$candidate" ]; then echo "$candidate"; return 0; fi
  done
  return 1
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
export HOSTLOAD_UV="$uv_bin"
# Run on a Python already on this computer when there is one; uv downloads one only if not.
export UV_PYTHON_PREFERENCE=system

if ! "$uv_bin" python find '>=3.9' >/dev/null 2>&1; then
  say "No Python on this computer yet; downloading one for the installer..."
  "$uv_bin" python install 3.12
fi

# Use the copy next to this file when there is one, otherwise fetch it.
here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
script="$here/setup/hostload_setup.py"
if [ -z "$here" ] || [ ! -f "$script" ]; then
  script="$RAW_BASE/setup/hostload_setup.py"
fi

# Piped from curl, stdin is this script itself; questions must read the terminal.
input=/dev/stdin
if [ ! -t 0 ] && [ -r /dev/tty ]; then input=/dev/tty; fi

set +e
"$uv_bin" run --quiet --no-project "$script" "$action" ${args[@]+"${args[@]}"} <"$input"
code=$?
set -e

if [ "$code" -eq "$REMOVE_UV_EXIT" ]; then
  remove_uv
  code=0
fi
exit "$code"
