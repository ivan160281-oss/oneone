#!/usr/bin/env bash
# Runs on the server after GitHub Actions has copied the new code into
# /opt/altgeo: refresh dependencies and restart the service.
set -euo pipefail
cd /opt/altgeo
[ -d venv ] || python3 -m venv venv
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet -r requirements.txt
sudo /usr/bin/systemctl restart altgeo
sleep 2
curl -fsS -o /dev/null http://127.0.0.1:8000/ && echo "ALTGEO is up"
