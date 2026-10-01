#!/usr/bin/env bash
# One-time setup of an Ubuntu/Debian VPS for ALTGEO. Run as root:
#
#   curl -fsSL <raw url of this file> -o setup.sh
#   DOMAIN=altgeo.su ADMIN_LOGIN=admin ADMIN_PASSWORD='...' WIFIGPS_PASSWORD='...' \
#   DEPLOY_PUBKEY='ssh-ed25519 AAAA... github-actions' bash setup.sh
#
# After this, every push to the main branch is copied here by GitHub Actions
# (.github/workflows/deploy.yml) and the service is restarted.
set -euo pipefail

: "${DOMAIN:?set DOMAIN}"
: "${ADMIN_LOGIN:?set ADMIN_LOGIN}"
: "${ADMIN_PASSWORD:?set ADMIN_PASSWORD}"
: "${WIFIGPS_PASSWORD:?set WIFIGPS_PASSWORD}"
: "${DEPLOY_PUBKEY:?set DEPLOY_PUBKEY (public half of the GitHub Actions deploy key)}"

APP_DIR=/opt/altgeo
DATA_DIR=/var/lib/altgeo
APP_USER=altgeo

apt-get update
apt-get install -y python3 python3-venv rsync debian-keyring debian-archive-keyring apt-transport-https curl gnupg

# Caddy: HTTPS with automatic Let's Encrypt certificates.
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
fi

id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "$APP_USER"
mkdir -p "$APP_DIR" "$DATA_DIR" "/home/$APP_USER/.ssh"
chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$DATA_DIR"

grep -qxF "$DEPLOY_PUBKEY" "/home/$APP_USER/.ssh/authorized_keys" 2>/dev/null \
  || echo "$DEPLOY_PUBKEY" >> "/home/$APP_USER/.ssh/authorized_keys"
chown -R "$APP_USER:$APP_USER" "/home/$APP_USER/.ssh"
chmod 700 "/home/$APP_USER/.ssh"
chmod 600 "/home/$APP_USER/.ssh/authorized_keys"

# The deploy user may restart the service and nothing else.
echo "$APP_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart altgeo" > /etc/sudoers.d/altgeo
chmod 440 /etc/sudoers.d/altgeo

umask 077
cat > /etc/altgeo.env <<EOF
ALTGEO_DB=$DATA_DIR/altgeo.db
ADMIN_LOGIN=$ADMIN_LOGIN
ADMIN_PASSWORD=$ADMIN_PASSWORD
WIFIGPS_PASSWORD=$WIFIGPS_PASSWORD
EOF
umask 022

cat > /etc/systemd/system/altgeo.service <<EOF
[Unit]
Description=ALTGEO server
After=network.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=/etc/altgeo.env
ExecStart=$APP_DIR/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers
Restart=always

[Install]
WantedBy=multi-user.target
EOF

# The GSM tracker posts over plain HTTP and can't follow redirects, so its
# endpoint stays on http://; everything else is redirected to https://.
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	reverse_proxy 127.0.0.1:8000
}

http://$DOMAIN {
	handle /api/device/* {
		reverse_proxy 127.0.0.1:8000
	}
	handle {
		redir https://{host}{uri} 308
	}
}
EOF

systemctl daemon-reload
systemctl enable altgeo
systemctl reload caddy || systemctl restart caddy

echo
echo "Done. Push to the main branch on GitHub to deploy the code."
