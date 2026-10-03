#!/usr/bin/env bash
# Installs (or updates) the delivery coordinator on this laptop, then runs `delivery setup`
# to write your config. While the repository is private, run it with your GitHub CLI login:
#
#   bash <(gh api repos/Ryanfo/ai-sdlc-coordinator/contents/install.sh -H "Accept: application/vnd.github.raw")
#
# or from a clone: ./install.sh
#
# Run it again at any time to update. Settings (environment variables):
#   DELIVERY_HOME    where the code lives (default ~/.delivery-platform)
#   DELIVERY_REPO    GitHub repository to install from (default Ryanfo/ai-sdlc-coordinator)
#   DELIVERY_CONFIG  your config file (default ~/delivery.local.toml)
set -euo pipefail

repo="${DELIVERY_REPO:-Ryanfo/ai-sdlc-coordinator}"
home="${DELIVERY_HOME:-$HOME/.delivery-platform}"
config="${DELIVERY_CONFIG:-$HOME/delivery.local.toml}"

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
fail() {
  printf '\ninstall: %s\n' "$*" >&2
  exit 1
}
have() { command -v "$1" >/dev/null 2>&1; }

# Questions are read from the terminal even when this script itself arrives on stdin.
tty=/dev/stdin
[ -t 0 ] || tty=/dev/tty

yes_no() { # yes_no "Question?" -> success for yes; Enter means yes
  local reply=""
  printf '%s (Y/n) ' "$1"
  read -r reply <"$tty" || reply=n
  case "$reply" in [nN]*) return 1 ;; *) return 0 ;; esac
}

case "$(uname -s)" in
  Darwin) os=mac ;;
  Linux) os=linux ;;
  *) fail "this runs on macOS or Linux (on Windows, use WSL2)." ;;
esac

# Running ./install.sh from a clone installs that clone instead of making another.
script="${BASH_SOURCE[0]:-}"
if [ -z "${DELIVERY_HOME:-}" ] && [ -f "$script" ]; then
  here="$(cd "$(dirname "$script")" && pwd)"
  if grep -q '^name = "delivery-platform"' "$here/pyproject.toml" 2>/dev/null; then
    home="$here"
  fi
fi

step "Checking the tools it needs"

have git || fail "Git is missing. On macOS run: xcode-select --install"

brew_install() { # brew_install <command> <formula>: offer Homebrew for a missing tool
  have "$1" && return 0
  if [ "$os" = mac ] && have brew && yes_no "$1 is not installed. Install it with Homebrew (brew install $2)?"; then
    brew install "$2"
  fi
}

brew_install gh gh
have gh || fail "GitHub CLI (gh) is missing: https://github.com/cli/cli#installation"
if ! gh auth status >/dev/null 2>&1; then
  say "Sign in to GitHub (the coordinator opens pull requests as you):"
  gh auth login <"$tty"
fi

brew_install uv uv
if ! have uv && yes_no "uv (Python tooling) is not installed. Install it with its official installer?"; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
have uv || fail "uv is missing: https://docs.astral.sh/uv/getting-started/installation/"

if ! have claude && yes_no "Claude Code is not installed. Install it with Anthropic's installer?"; then
  curl -fsSL https://claude.ai/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
fi
have claude || say "Claude Code is not installed yet; setup will remind you."

if [ "$os" = linux ] && { ! have bwrap || ! have socat; }; then
  say "Claude's sandbox on Linux also needs bubblewrap and socat (for example: sudo apt install bubblewrap socat)."
fi

step "Getting the code"
if [ "$home" = "${here:-}" ]; then
  say "Using this clone: $home"
elif [ -d "$home/.git" ]; then
  say "Updating $home"
  git -C "$home" pull --ff-only --quiet || say "Could not update $home (local changes?); using it as it is."
else
  gh repo clone "$repo" "$home" -- --quiet
fi

step "Installing the delivery and coordinator commands"
# --editable runs the code in $home directly, so updates need only a git pull (or this script).
uv tool install --editable "$home" --force --quiet
bin="$(uv tool dir --bin)"
case ":$PATH:" in
  *":$bin:"*) ;;
  *)
    if yes_no "Add $bin to your PATH so the commands work in new terminals (uv tool update-shell)?"; then
      uv tool update-shell
    fi
    export PATH="$bin:$PATH"
    ;;
esac
say "Installed: $("$bin/delivery" --version)"

step "Your config"
if [ -f "$config" ]; then
  say "$config already exists, so it is left as it is."
  say "To change your answers: delivery setup"
  say "To start the coordinator: coordinator"
else
  "$bin/delivery" setup --config "$config" <"$tty"
fi
