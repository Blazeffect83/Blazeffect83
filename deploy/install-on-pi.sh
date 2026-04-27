#!/usr/bin/env bash
# VOLTFORGE — one-shot Raspberry Pi installer.
#
# What it does:
#   1. Sanity-checks the OS, arch, and free disk
#   2. Installs Node.js 20 (NodeSource) if missing/old
#   3. Installs build deps for better-sqlite3 (build-essential, python3, sqlite3)
#   4. Clones the repo to ~/Blazeffect83 (or pulls if already there)
#   5. Checks out the working branch
#   6. npm ci + npm run build
#   7. Creates /var/lib/voltforge with correct ownership
#   8. Installs the systemd unit, enables it, starts it
#   9. Drops the `voltforge` operator CLI in /usr/local/bin
#  10. Prints the URL(s)
#
# Re-running it is safe — it's idempotent.
#
# Usage:
#   bash <(curl -fsSL https://raw.githubusercontent.com/Blazeffect83/Blazeffect83/claude/gym-app-dark-aesthetic-RuGh1/deploy/install-on-pi.sh)

set -euo pipefail

REPO_URL="${VOLTFORGE_REPO:-https://github.com/Blazeffect83/Blazeffect83.git}"
BRANCH="${VOLTFORGE_BRANCH:-claude/gym-app-dark-aesthetic-RuGh1}"
APP_DIR="$HOME/Blazeffect83"
WEB_DIR="$APP_DIR/web"
DEPLOY_DIR="$APP_DIR/deploy"
DATA_DIR="/var/lib/voltforge"
SERVICE_PATH="/etc/systemd/system/voltforge.service"
CLI_PATH="/usr/local/bin/voltforge"
USER_NAME="$(id -un)"

step() { printf "\n\033[1;35m==>\033[0m %s\n" "$*"; }
ok()   { printf "    \033[1;32mok\033[0m %s\n" "$*"; }
warn() { printf "    \033[1;33m!!\033[0m %s\n" "$*"; }
fail() { printf "\n\033[1;31mxx\033[0m %s\n" "$*"; exit 1; }

[[ "$EUID" -ne 0 ]] || fail "Run this as your normal user (e.g. 'pi'), not as root. It will sudo when needed."
command -v sudo >/dev/null 2>&1 || fail "sudo not found — please install it first."

step "Checking environment"
arch="$(uname -m)"
case "$arch" in
  aarch64|arm64|x86_64) ok "arch: $arch" ;;
  armv7l) warn "32-bit arch — better-sqlite3 may need extra build time" ;;
  *)      fail "unsupported arch: $arch" ;;
esac

if [[ -f /etc/os-release ]]; then
  . /etc/os-release
  ok "os: ${PRETTY_NAME:-$ID}"
fi

free_kb="$(df -k "$HOME" | tail -1 | awk '{print $4}')"
free_mb=$(( free_kb / 1024 ))
[[ "$free_mb" -ge 700 ]] || fail "Not enough free disk in \$HOME (need ~700MB, have ${free_mb}MB)"
ok "disk free: ${free_mb}MB"

step "Installing system packages"
sudo apt-get update -y
sudo apt-get install -y curl git ca-certificates build-essential python3 sqlite3
ok "apt packages ready"

step "Ensuring Node.js 20+"
need_node=1
if command -v node >/dev/null 2>&1; then
  v="$(node -v | sed 's/v//;s/\..*//')"
  if [[ "$v" -ge 18 ]]; then
    need_node=0
    ok "node $(node -v) already installed"
  else
    warn "node $(node -v) is too old, upgrading"
  fi
fi
if [[ "$need_node" -eq 1 ]]; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y nodejs
  ok "node $(node -v) installed"
fi

NODE_BIN="$(command -v node)"
NPM_BIN="$(command -v npm)"
[[ -x "$NODE_BIN" ]] || fail "node binary missing"
[[ -x "$NPM_BIN" ]]  || fail "npm binary missing"

step "Cloning / updating repo"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch origin "$BRANCH"
  git -C "$APP_DIR" checkout "$BRANCH"
  git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
  ok "pulled $BRANCH"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  ok "cloned to $APP_DIR"
fi

step "Installing app deps"
(cd "$WEB_DIR" && npm ci --no-audit --no-fund)
ok "node_modules ready"

step "Building"
(cd "$WEB_DIR" && npm run build)
ok "build complete"

step "Preparing data directory"
sudo mkdir -p "$DATA_DIR"
sudo chown -R "$USER_NAME":"$USER_NAME" "$DATA_DIR"
sudo chmod 750 "$DATA_DIR"
ok "$DATA_DIR ready (owned by $USER_NAME)"

step "Installing systemd unit"
tmp_unit="$(mktemp)"
sed \
  -e "s|__USER__|$USER_NAME|g" \
  -e "s|__APP_DIR__|$WEB_DIR|g" \
  -e "s|__DATA_DIR__|$DATA_DIR|g" \
  -e "s|__NODE__|$NODE_BIN|g" \
  -e "s|__NPM__|$NPM_BIN|g" \
  "$DEPLOY_DIR/voltforge.service" > "$tmp_unit"
sudo install -m 0644 "$tmp_unit" "$SERVICE_PATH"
rm -f "$tmp_unit"
sudo systemctl daemon-reload
sudo systemctl enable voltforge.service
sudo systemctl restart voltforge.service
ok "voltforge.service installed + enabled + started"

step "Installing voltforge CLI"
sudo install -m 0755 "$DEPLOY_DIR/voltforge" "$CLI_PATH"
ok "$CLI_PATH installed"

step "Tailscale serve auto-config"
if command -v tailscale >/dev/null 2>&1 && tailscale ip -4 >/dev/null 2>&1; then
  voltforge serve >/dev/null 2>&1 \
    && ok "tailscale serve proxy applied + persisted across reboots" \
    || warn "tailscale serve config skipped — run 'voltforge serve' later"
else
  warn "tailscale not active yet — run 'sudo tailscale up' then 'voltforge serve'"
fi

step "Waiting for VOLTFORGE to come up"
for i in {1..30}; do
  if curl -fsS "http://127.0.0.1:3000" -o /dev/null 2>/dev/null; then
    ok "responding on port 3000"
    break
  fi
  sleep 1
  [[ "$i" -eq 30 ]] && warn "didn't respond in 30s — check 'voltforge logs'"
done

echo
echo "=========================================================="
echo "  VOLTFORGE is up."
echo
voltforge url || true
echo "  Operator CLI:"
echo "    voltforge doctor    diagnose what's broken"
echo "    voltforge fix       attempt every self-heal in sequence"
echo "    voltforge update    pull + build + restart"
echo "    voltforge backup    snapshot the SQLite db"
echo
echo "  Optional — for HTTPS from anywhere via Tailscale:"
echo "      curl -fsSL https://tailscale.com/install.sh | sh"
echo "      sudo tailscale up"
echo "      voltforge serve"
echo "=========================================================="
