#!/usr/bin/env bash
# Read-only checks for a failing site on REG.RU. Usage: diagnose.sh <domain>
DOMAIN="${1:?usage: diagnose.sh <domain>}"
SITE_DIR="$HOME/www/$DOMAIN"
cd "$HOME"

echo "== site folder"; ls -la "$SITE_DIR"
echo "== .htaccess"; cat "$SITE_DIR/.htaccess"
echo "== python"; "$HOME/altgeo/venv/bin/python" -V
echo "== import passenger_wsgi"
(cd "$SITE_DIR" && "$HOME/altgeo/venv/bin/python" -c "import passenger_wsgi; print('import ok')") 2>&1 | tail -20
echo "== request through the app"
(cd "$SITE_DIR" && "$HOME/altgeo/venv/bin/python" - <<'PY'
import io, passenger_wsgi
env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/", "SERVER_NAME": "x", "SERVER_PORT": "80",
       "wsgi.url_scheme": "http", "wsgi.input": io.BytesIO(), "wsgi.errors": io.StringIO(),
       "SERVER_PROTOCOL": "HTTP/1.1", "QUERY_STRING": ""}
out = []
body = passenger_wsgi.application(env, lambda s, h, e=None: out.append(s))
print(out, b"".join(body)[:80])
PY
) 2>&1 | tail -20
echo "== live site"
curl -s -m 30 -D - "http://$DOMAIN/" | head -30
echo "== passenger"
ls -la /opt/python 2>/dev/null; command -v python3 python; python3 -V
echo "== logs"
ls -la "$HOME/logs" 2>/dev/null
for f in $(ls -t "$HOME"/logs/*error* "$HOME"/logs/*"$DOMAIN"* 2>/dev/null | head -4); do
  echo "--- $f"; tail -15 "$f"
done
