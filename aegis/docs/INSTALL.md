# Installation guide (Raspberry Pi 5 + Ubuntu Server ARM64 + external SSD)

Every manual step is listed below. Steps marked 🔐 change the system and need `sudo`.

## 0. What you need

* Raspberry Pi 5 (4 GB works; 8 GB recommended if you want a local model), official 27 W USB-C supply, active cooler.
* An SSD, either NVMe on an M.2 HAT or USB 3, holding **Ubuntu Server 24.04 LTS (64-bit)**.
* Network access during installation (Ubuntu packages and PyPI).
* An iPad with Safari, plus the free Tailscale app for remote access.

## 1. Prepare the Pi (one-time)

1. Flash *Ubuntu Server 24.04 LTS (64-bit)* to the SSD with Raspberry Pi Imager. In the Imager settings, set the hostname (for example `aegis-pi`), a user and SSH public-key login.
2. NVMe boot: update the bootloader (`sudo rpi-eeprom-update -a`) and make sure `BOOT_ORDER` tries NVMe first (`sudo rpi-eeprom-config --edit`). Then reboot.
3. SSH in once, then update: 🔐 `sudo apt update && sudo apt full-upgrade -y && sudo reboot`.
4. Optional swap, which helps if you run a local model: 🔐 `sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab`

## 2. Read-only environment audit

Before changing anything, check what is already on the machine:

```bash
git clone <repo-url> ~/aegis-src && cd ~/aegis-src/aegis
python3 -m venv /tmp/aegis-audit && /tmp/aegis-audit/bin/pip install -q .
/tmp/aegis-audit/bin/aegis env-report          # OS, CPU, RAM, SSD, mounts, temperature, ports, Docker, runtimes, sandbox
```

This command makes no changes. It reports whether bubblewrap works and whether AppArmor restricts user namespaces.

## 3. Install

🔐 Run `sudo ./scripts/install.sh`. To put data on a separate SSD mount, use `--data-dir /mnt/ssd/aegis`.

The installer asks for confirmation before each of these:

| Step | Change |
|---|---|
| packages | `apt-get install python3-venv bubblewrap` (only if missing) |
| user | creates the system user `aegis` (no login shell) |
| code | copies the app to `/opt/aegis` and creates a virtualenv `/opt/aegis/.venv` (root-owned, read-only to `aegis`) |
| config | creates `/etc/aegis/aegis.env` (mode 640, `root:aegis`); never overwrites an existing file |
| data | creates the data directory (mode 750, `aegis:aegis`) and applies the database migrations |
| sandbox | probes bubblewrap as `aegis`. On Ubuntu 24.04 AppArmor usually blocks unprivileged user namespaces, so it **offers** to install `/etc/apparmor.d/aegis-bwrap` (see SECURITY.md for the trade-off) |
| password | `aegis set-password` (min. 10 chars, scrypt hash stored in the env file) |
| service | installs `aegis.service`, enables it at boot and starts it |

It never formats storage, edits boot settings, touches SSH configuration or opens firewall ports.

Afterwards, check:

```bash
systemctl status aegis
curl -s http://127.0.0.1:8600/health          # {"ok": true}
sudo -u aegis AEGIS_ENV_FILE=/etc/aegis/aegis.env /opt/aegis/.venv/bin/aegis status
```

## 4. Choose a model provider

Edit 🔐 `sudoedit /etc/aegis/aegis.env`, then run 🔐 `sudo systemctl restart aegis`.

**Cloud (best plan quality):**

```
MODEL_PROVIDER=anthropic            # or openai
MODEL_NAME=<current model id from the provider's model list>
ANTHROPIC_API_KEY=<key>             # or OPENAI_API_KEY
DAILY_SPEND_LIMIT_USD=2.00
MODEL_PRICES=<model-id>=<input $/Mtok>:<output $/Mtok>   # exact cost accounting; otherwise a conservative estimate
```

**Local only (free, private, slower):**

```bash
curl -fsSL https://ollama.com/install.sh | sh     # third-party installer — review it first
ollama pull <small model, e.g. a 1–3B 4-bit instruct model>
```

```
MODEL_PROVIDER=local
LOCAL_MODEL_BASE_URL=http://127.0.0.1:11434/v1
MODEL_NAME=<name shown by `ollama list`>
LOCAL_ONLY=true
```

A Pi 5 runs only small quantised models at usable speed, and planning quality drops with them. A good split is a cloud model for planning (`MODEL_NAME`) and a cheaper one for extraction (`MODEL_NAME_LIGHT`).

**No model:** `MODEL_PROVIDER=none` still lets you run research objectives that contain explicit URLs, plus scheduled topic research.

## 5. Load research topics

```bash
sudo -u aegis AEGIS_ENV_FILE=/etc/aegis/aegis.env /opt/aegis/.venv/bin/aegis load-topics /opt/aegis/config/topics.example.json
```

You can also add topics on the iPad under *Research → Add topic*.

Optional web search: run a self-hosted SearXNG instance with its JSON format enabled, then set `SEARXNG_URL=http://127.0.0.1:8888`.

## 6. Reach the dashboard from the iPad

The dashboard listens on **127.0.0.1 only**. Do not port-forward it on your router. Use Tailscale (a WireGuard VPN):

1. 🔐 On the Pi: `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`
2. On the iPad: install Tailscale from the App Store and sign in to the same account.
3. 🔐 On the Pi, publish the dashboard over HTTPS **inside your tailnet only**:
   `sudo tailscale serve --bg --https=443 http://127.0.0.1:8600`
4. In `/etc/aegis/aegis.env`, set `COOKIE_SECURE=true` and `ALLOWED_HOSTS=aegis-pi.<tailnet>.ts.net,127.0.0.1,localhost`, then run `sudo systemctl restart aegis`.
5. In Safari, open `https://aegis-pi.<tailnet>.ts.net`, sign in, then choose *Share → Add to Home Screen*.

On the home network only, without a VPN, you can use an SSH tunnel from an iPad SSH app (for example Blink: `ssh -L 8600:127.0.0.1:8600 aegis-pi`) and browse to `http://127.0.0.1:8600`.

Setting `BIND_HOST=0.0.0.0` is possible, but AEGIS refuses to start that way without a password. Without TLS your password would cross the LAN in clear text, so avoid it.

## 7. API token (optional, for scripts or Shortcuts)

```bash
sudo /opt/aegis/.venv/bin/aegis --env-file /etc/aegis/aegis.env create-token   # prints the token once
sudo systemctl restart aegis
```

Send it as `Authorization: Bearer <token>`. See [API.md](API.md).

## 8. Notifications (optional)

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, or the `SMTP_*` keys. Without them, notifications appear only on the dashboard.
