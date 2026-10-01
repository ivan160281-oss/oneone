#!/usr/bin/env bash
# Puts an old engineering database (wifigps.db from the previous server) in
# place. The person uploads it with the hosting's file manager into
# ~/altgeo-data/eng/import/ under any name ending in .db; update.sh runs this.
set -euo pipefail
DATA_DIR="$1"
IMPORT_DIR="$DATA_DIR/import"
[ -d "$IMPORT_DIR" ] || exit 0
NEW=$(ls -t "$IMPORT_DIR"/*.db 2>/dev/null | head -1 || true)
[ -n "$NEW" ] || exit 0

APP_PY="$HOME/altgeo/venv/bin/python"
CHECK=$("$APP_PY" - "$NEW" <<'PY'
import sqlite3, sys
try:
    c = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
    ok = c.execute("PRAGMA integrity_check").fetchone()[0]
    n = c.execute("SELECT COUNT(*) FROM points").fetchone()[0]
    s = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    print("%s sessions=%d points=%d" % (ok, s, n))
except Exception as e:
    print("bad: %s" % e)
PY
)
echo "Import candidate $(basename "$NEW"): $CHECK"
case "$CHECK" in
  ok\ *) ;;
  *) mv "$NEW" "$NEW.rejected"; echo "Not a usable wifigps database, renamed to .rejected"; exit 1 ;;
esac

STAMP=$(date +%Y%m%d_%H%M%S)
mkdir -p "$DATA_DIR/replaced"
for f in wifigps.db wifigps.db-wal wifigps.db-shm; do
  if [ -f "$DATA_DIR/$f" ]; then mv "$DATA_DIR/$f" "$DATA_DIR/replaced/${STAMP}_$f"; fi
done
mv "$NEW" "$DATA_DIR/wifigps.db"
echo "Imported into $DATA_DIR/wifigps.db (the previous one is in replaced/)"
