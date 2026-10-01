#!/usr/bin/env bash
# Read-only checks for a failing site on REG.RU. Usage: diagnose.sh <domain>
DOMAIN="${1:?usage: diagnose.sh <domain>}"
SITE_DIR="$HOME/www/$DOMAIN"
cd "$HOME"

echo "== device password (sha256 of WIFIGPS_PASSWORD, compare with the firmware's)"
grep -E '^WIFIGPS_PASSWORD=' "$HOME/altgeo.env" | cut -d= -f2- | tr -d '\r\n' | sha256sum
echo "== data folders"; ls -laR "$HOME/altgeo-data" 2>&1 | head -60
echo "== database files anywhere in home"
find "$HOME" -maxdepth 5 \( -iname '*.db' -o -iname '*wifigps*' -o -iname 'import' \) -not -path '*/venv/*' -printf '%TY-%Tm-%Td %TH:%TM %10s %p\n' 2>/dev/null | sort | tail -30
echo "== site folder"; ls -la "$SITE_DIR"
echo "== .htaccess"; cat "$SITE_DIR/.htaccess"
echo "== python"; "$HOME/altgeo/venv/bin/python" -V
echo "== import passenger_wsgi"
(cd "$SITE_DIR" && "$HOME/altgeo/venv/bin/python" -c "import passenger_wsgi; print('import ok')") 2>&1 | tail -20
echo "== live site (nginx)"
curl -s -m 30 -D - -o /tmp/altgeo-live.html "http://$DOMAIN/" | head -12
grep -o '<title>[^<]*' /tmp/altgeo-live.html
echo "== apache directly"
curl -s -m 30 -D - -o /tmp/altgeo-apache.html -H "Host: $DOMAIN" "http://127.0.0.1:8080/" | head -12
grep -o '<title>[^<]*' /tmp/altgeo-apache.html
echo "== api route"
curl -s -m 30 -D - "http://$DOMAIN/api/me" | head -15
echo "== web server config for the site"
for d in /etc/nginx/vhosts/$USER /etc/nginx/vhosts-resources/$DOMAIN /etc/httpd/conf/vhosts/$USER \
         /etc/apache2/vhosts/$USER /etc/nginx/conf.d /etc/httpd/conf.d; do
  [ -e "$d" ] && { echo "--- $d"; ls -la "$d" 2>&1 | head -20; }
done
for f in /etc/nginx/vhosts/$USER/$DOMAIN.conf /etc/httpd/conf/vhosts/$USER/$DOMAIN.conf \
         /etc/apache2/vhosts/$USER/$DOMAIN.conf; do
  [ -r "$f" ] && { echo "--- $f"; grep -v '^\s*#' "$f" | grep -v '^\s*$' | head -80; }
done
echo "== passenger"
for f in /etc/httpd/conf.d/passenger.conf /etc/httpd/conf.d/README /etc/httpd/conf.d/disabled.conf \
         /etc/httpd/conf.d/fakephp.conf; do echo "--- $f"; cat "$f"; done
ls -la "$HOME/www"
command -v passenger-config passenger-status; ls -d /opt/passenger* /usr/share/passenger* 2>/dev/null
echo "== logs"
ls -la "$HOME/logs" 2>/dev/null
for f in $(ls -t "$HOME"/logs/*error* "$HOME"/logs/*"$DOMAIN"* 2>/dev/null | head -4); do
  echo "--- $f"; tail -15 "$f"
done
