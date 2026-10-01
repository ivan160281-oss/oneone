"""Entry point for Phusion Passenger on REG.RU shared hosting.

update.sh copies this file into the site folder (~/www/<domain>) with
__APP_DIR__ and __ENV_FILE__ filled in; the code itself stays outside the
site folder so Apache never serves the sources or the database as files.
"""
import os
import sys

APP_DIR = "__APP_DIR__"
ENV_FILE = "__ENV_FILE__"

sys.path.insert(0, APP_DIR)

if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

from a2wsgi import ASGIMiddleware  # noqa: E402

from app import db  # noqa: E402
from app.main import app, bootstrap  # noqa: E402

# Passenger speaks WSGI, so the ASGI lifespan hook never runs: do its work here.
_conn = db.connect()
try:
    bootstrap(_conn)
finally:
    _conn.close()

application = ASGIMiddleware(app)
