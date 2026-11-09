#!/usr/bin/env bash
# Install the bot unit while retaining the existing local Telegram API unit.
set -euo pipefail
if [[ ${EUID} -ne 0 || -z ${SUDO_USER:-} || ${SUDO_USER} == root ]]; then
  printf '%s\n' 'Run this from your checkout with: sudo bash scripts/install-systemd.sh' >&2
  exit 1
fi
satou_user=$SUDO_USER
satou_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ ! $satou_user =~ ^[a-z_][a-z0-9_-]*$ || ! $satou_root =~ ^/[a-zA-Z0-9_./-]+$ ]]; then
  printf '%s\n' 'The username or checkout path has unsupported characters; configure the unit manually.' >&2
  exit 1
fi
if [[ ! -x $satou_root/.venv/bin/python || ! -f $satou_root/.env ]]; then
  printf '%s\n' 'The checkout needs .venv/bin/python and .env before installing the service.' >&2
  exit 1
fi
if ! runuser -u "$satou_user" -- test -r "$satou_root/.env"; then
  printf '%s\n' 'The selected user cannot read the checkout .env.' >&2
  exit 1
fi
if ! systemctl cat satoubot-api.service --no-pager >/dev/null 2>&1; then
  printf '%s\n' 'satoubot-api.service was not found. Install your Telegram local Bot API service first.' >&2
  exit 1
fi
unit=/etc/systemd/system/satoubot.service
if [[ -f $unit ]]; then
  backup=$(mktemp /etc/systemd/system/satoubot.service.backup.XXXXXX)
  cp -p -- "$unit" "$backup"
  printf 'Existing unit backed up to %s\n' "$backup"
fi
tmp=$(mktemp /etc/systemd/system/.satoubot.service.XXXXXX)
trap 'rm -f -- "$tmp"' EXIT
cat > "$tmp" <<EOF
[Unit]
Description=SatouBot
Wants=network-online.target
After=network-online.target satoubot-api.service
Requires=satoubot-api.service

[Service]
Type=simple
User=$satou_user
WorkingDirectory=$satou_root
ExecStartPre=$satou_root/.venv/bin/python -m satoubot.wait_bot_api
ExecStart=$satou_root/.venv/bin/python -m satoubot.app
Restart=on-failure
RestartSec=5
TimeoutStartSec=100

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$tmp"
mv -f -- "$tmp" "$unit"
systemctl daemon-reload
printf '%s\n' 'Service installed. Your Telegram Bot API unit was preserved.'
printf '%s\n' 'After stopping any manually running SatouBot, run:'
printf '%s\n' '  sudo systemctl enable satoubot.service'
printf '%s\n' '  sudo systemctl restart satoubot.service'
