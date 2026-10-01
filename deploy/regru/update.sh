#!/usr/bin/env bash
# Runs on REG.RU shared hosting after GitHub Actions has copied the code to
# ~/altgeo. Usage: bash ~/altgeo/deploy/regru/update.sh <domain>
set -euo pipefail

DOMAIN="${1:?usage: update.sh <domain>}"
APP_DIR="$HOME/altgeo"
ENV_FILE="$HOME/altgeo.env"
SITE_DIR="$HOME/www/$DOMAIN"

[ -d "$SITE_DIR" ] || { echo "Site folder $SITE_DIR not found"; exit 1; }
if [ ! -f "$ENV_FILE" ]; then
  echo "Create $ENV_FILE first (see README: ADMIN_LOGIN, ADMIN_PASSWORD, WIFIGPS_PASSWORD)"
  exit 1
fi
mkdir -p "$HOME/altgeo-data/eng"
grep -q '^ALTGEO_DB=' "$ENV_FILE" || echo "ALTGEO_DB=$HOME/altgeo-data/altgeo.db" >> "$ENV_FILE"
grep -q '^WIFIGPS_DATA_DIR=' "$ENV_FILE" || echo "WIFIGPS_DATA_DIR=$HOME/altgeo-data/eng" >> "$ENV_FILE"
chmod 600 "$ENV_FILE"

# The system python3 on REG.RU is too old; their newer builds live in /opt/python.
pick_python() {
  local best="" best_ver=0 p v
  for p in /opt/python/python-3.*/bin/python3 "$(command -v python3 || true)"; do
    [ -x "$p" ] || continue
    v=$("$p" -c 'import sys; print(sys.version_info[0] * 100 + sys.version_info[1])') || continue
    if [ "$v" -ge 308 ] && [ "$v" -gt "$best_ver" ]; then best="$p"; best_ver="$v"; fi
  done
  [ -n "$best" ] || { echo "No Python 3.8+ found"; exit 1; }
  echo "$best"
}

cd "$APP_DIR"
if [ ! -x venv/bin/python ]; then
  PY=$(pick_python)
  echo "Creating venv with $PY ($("$PY" -V))"
  "$PY" -m venv venv
fi
venv/bin/python -m pip install --quiet --upgrade pip
venv/bin/python -m pip install --quiet -r requirements.txt

# Passenger takes the parent of the site folder (~/www) as the app root unless
# the panel sets it, so the entry point goes in both places.
APP_ROOT=$(dirname "$SITE_DIR")
for d in "$SITE_DIR" "$APP_ROOT"; do
  sed -e "s|__APP_DIR__|$APP_DIR|" -e "s|__ENV_FILE__|$ENV_FILE|" \
    deploy/regru/passenger_wsgi.py > "$d/passenger_wsgi.py"
  mkdir -p "$d/tmp"
  touch "$d/tmp/restart.txt"
done

FORCE_HTTPS=$(grep -E '^FORCE_HTTPS=' "$ENV_FILE" | cut -d= -f2 || true)
{
  echo "PassengerEnabled On"
  if [ "$FORCE_HTTPS" = "1" ]; then
    # The GSM tracker posts over plain HTTP and can't follow redirects.
    echo "RewriteEngine On"
    echo "RewriteCond %{HTTPS} off"
    echo "RewriteCond %{HTTP:X-Forwarded-Proto} !https"
    echo "RewriteCond %{REQUEST_URI} !^/api/device/"
    echo "RewriteRule ^ https://%{HTTP_HOST}%{REQUEST_URI} [R=301,L]"
  fi
} > "$SITE_DIR/.htaccess"

# A hosting placeholder page would shadow the app's own "/": set it aside.
for f in index.html index.php; do
  if [ -f "$SITE_DIR/$f" ]; then mv "$SITE_DIR/$f" "$SITE_DIR/$f.bak"; fi
done

# An early version left its database in the public folder: move it out of reach.
for f in "$SITE_DIR"/*.db "$SITE_DIR"/*.db-wal "$SITE_DIR"/*.db-shm; do
  if [ -f "$f" ]; then mv "$f" "$HOME/altgeo-data/old-site-$(basename "$f")"; fi
done

echo "Deployed to $DOMAIN"

# Check that the live site is the app and not a hosting placeholder (those answer 200 too).
sleep 3
BODY=$(curl -s -m 60 "http://$DOMAIN/" || true)
if printf '%s' "$BODY" | grep -q 'ALTGEO'; then
  echo "http://$DOMAIN/ serves ALTGEO"
else
  echo "http://$DOMAIN/ does not serve ALTGEO: $(printf '%s' "$BODY" | grep -o '<title>[^<]*' | head -1)"
  tail -5 "$HOME/logs/$DOMAIN.error.log" 2>/dev/null || true
  exit 1
fi
