# WiFi/GPS/BLE Tracker Server

A Flask app that:
- accepts uploads of the `.txt` log files produced by the T-LoRa Pager tracker
  firmware (GPS + WiFi + BLE + IMU heading/steps)
- stores every point and every WiFi/BLE sighting in a SQLite
  database, cumulatively (each upload adds to the data already collected,
  nothing is overwritten)
- shows the accumulated track and known WiFi networks / BLE devices on a map
  (Leaflet + OpenStreetMap, centered on Moscow region)
- can receive uploads **automatically over WiFi directly from the tracker**
  (no need to pull the SD card out by hand) - the firmware only sends files
  the server doesn't already have (see `/api/sync/check` / `/api/upload`
  below, and the firmware's own README for the on-device setup)
- has a **track detail / telemetry page** per uploaded session: point-by-point
  table of every WiFi/BLE signal seen (plus IMU heading/step data)
  for every point that's still around - **bad_gps points are permanently
  deleted by the automatic cleanup pass** (see below), so this table only
  ever shows 'ok' points with their real GPS position, plus an estimate for
  comparison (see "RSSI-based weighting")
- has a **Point Lab**: upload a file with missing/jammed GPS and see, without
  saving anything to the database, what a WiFi-only /
  BLE-only / combined reconstruction would look like - this is the one place
  reconstruction for GPS-denied stretches is still demonstrated, precisely
  *because* it never writes to `points` and so is unaffected by the purge
- has an **Algorithm Lab**: manually-triggered pass that finds WiFi networks /
  BLE devices re-confirmed across multiple different
  sessions, building a separate, higher-confidence "stable points" database
- runs a **daily background job** that reprocesses ALL uploaded data: rebuilds
  the WiFi/BLE aggregates, **permanently deletes every point
  currently marked `bad_gps`** (irreversible - see "Bad_gps points are now
  permanently deleted" below), and recomputes position estimates for what's
  left - `/api/locate` (paste currently-visible SSIDs/BLE macs, get
  a live position estimate) is unaffected by any of this, since it's computed
  fresh from the network/device databases rather than from any stored point
- is protected by a single shared login password (web UI session + a request
  header for the tracker's automatic sync)
- ships as a single Docker container

## Important limitation (read this first)

There's no existing public database mapping WiFi BSSIDs to real-world
coordinates. All positioning here is learned entirely from your own uploaded
data - the more overlapping coverage you upload (ideally the same
routes driven more than once, at least one of those times with a real GPS
fix), the better the GPS-independent estimates get. A brand new install with
one file uploaded won't be able to reconstruct anything yet. Note that this
learning happens through the WiFi/BLE network databases, not
through keeping any specific point's data - a point with no GPS fix at all
gets permanently deleted once it's been used to help build those databases
(see "Bad_gps points are now permanently deleted").

## Quick start (Docker)

```bash
docker compose up -d --build
```

Open http://localhost:5000/. That's it - the SQLite database, uploaded log
files, and device firmware binaries all live directly on the host filesystem
at `./data` and `./uploads` (next to `docker-compose.yml`), via bind mounts -
not Docker-managed named volumes. They survive `docker compose down` and
rebuilds automatically (a bind mount is never implicitly deleted by Compose),
and you can inspect/back them up directly - e.g. `sqlite3 ./data/wifigps.db`,
or just `cp -r ./data ./data-backup`. To start completely fresh, stop the
container and delete those folders yourself:

```bash
docker compose down
rm -rf ./data ./uploads   # deletes all data - back up first if unsure
```

### Upgrading from an older deployment (named Docker volumes)

**If you're updating from a version of this project that used named Docker
volumes** (`wifigps_data` / `wifigps_uploads` referenced under a top-level
`volumes:` key in the old `docker-compose.yml`) **to this bind-mount setup,
your existing data will NOT show up automatically** - changing where Compose
looks doesn't move anything, so the container now sees an empty `./data`
while your real database is still sitting untouched in the old named volume.
Nothing is lost, but you do need one manual step to bring it across:

```bash
sh migrate_old_volumes.sh
```

This lists your Docker volumes, asks which ones are the old ones (usually
named `<something>_wifigps_data` / `<something>_wifigps_uploads`), and copies
their contents into `./data` / `./uploads` here - it never deletes the old
volume, so it's safe to run even just to check. Run it once, then
`docker compose up -d --build` as usual. If you'd rather do it by hand:

```bash
docker volume ls | grep wifigps                      # find the exact old volume names
mkdir -p ./data ./uploads
docker run --rm -v OLD_DATA_VOLUME_NAME:/old:ro -v "$(pwd)/data":/new alpine cp -av /old/. /new/
docker run --rm -v OLD_UPLOADS_VOLUME_NAME:/old:ro -v "$(pwd)/uploads":/new alpine cp -av /old/. /new/
```

### Configuration

Set these via `environment:` in `docker-compose.yml`:
- `WIFIGPS_SECRET_KEY` - used to sign the login session cookie; set a real
  random string for anything beyond local testing
- `WIFIGPS_PASSWORD` - **the login password for the internal engineering
  tool** (a single shared password, separate from individual user accounts -
  see "Users" below). Defaults to `changeme` if unset, with a warning banner
  shown on every page until you change it. The tracker's automatic WiFi sync
  uses this same password too, sent as an `X-Sync-Password` request header
  (see the firmware's own README) since it can't do a browser login
- `WIFIGPS_DATA_DIR` - where the SQLite file and firmware storage live
  (defaults to `/app/data`, matching the `./data` bind mount - only change
  this if you also change the mount path)

### Exposing it publicly

Put a reverse proxy (nginx, Caddy, Traefik) in front of the container for
HTTPS - the container itself only serves plain HTTP on port 5000. Example
nginx config:
```nginx
server {
    listen 80;
    server_name your-domain-or-ip;
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```
Then use `certbot` (Let's Encrypt) for a free certificate if you have a domain.

**Without HTTPS, the login password (and the tracker's sync password header)
travel in plain text** over the network between the client/device and your
server. Fine for a local/trusted network; if the server is reachable over the
open internet without HTTPS in front of it, treat the password as not
actually secret.

**HTTPS is also required for the browser-based firmware flasher** (Tracker →
"Прошить устройство в браузере") to work at all - Web Serial API, which it
depends on, is only exposed by the browser on a secure context (HTTPS, or
`localhost`). Over plain `http://your-domain:5000` it's silently unavailable
in every browser, Chrome and Edge included - not a "wrong browser" problem
even though it can look like one.

## Running without Docker (local dev)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Pages

- **Reports** (`/reports`) - dashboard for everything the automatic cleanup
  does:
  - **Removal summary** - stat cards: how many points have been permanently
    deleted (from `deletion_log`), broken down by reason (speed outlier,
    implausible timestamp, manual cleanup), and how many WiFi/BLE devices are
    currently excluded as co-traveling
  - **Removal log** - chronological table of every cleanup action (which
    session, how many points, when, why) plus every device exclusion (with
    a **Restore** button per device, for when detection got it wrong -
    reversible, and pinned so future auto-detection won't silently re-exclude
    it)
  - **Removal map** - one marker per deleted batch (session + reason), at the
    centroid of whichever points in it had real coordinates, colored by
    reason - not individual points, since those are permanently gone by the
    time this is shown
  - **"Run co-traveling detection now"** button - manual trigger (it also
    runs automatically after every upload and once daily); any newly
    detected co-traveling device is excluded immediately, no review step
  - **Algorithm effectiveness: old vs new** - pick a session (needs real GPS
    to compare against) to see a map (GPS ground truth + WiFi/BLE with-vs-
    without co-traveling-exclusion, each independently
    toggleable) and a bar chart of average position error for each variant
- **Map** (`/`) - full-screen map. A slim controls bar at the top has toggles
  for the track, WiFi networks, BLE devices, and suspicious
  jumps. Plus a session filter and a collapsible **Sessions** panel (splits
  the screen left/right when open, listing every upload with a link to its
  telemetry page). Below the map: a "WiFi-only locate" tool (paste
  currently-visible SSIDs and/or BLE MAC addresses, get a rough position
  estimate).
- **Upload logs** (`/upload`) - upload one or more `.txt` files. The result
  page shows a summary of everything the automatic cleanup pipeline did to
  the newly-uploaded data (see "How processing works" below)
- **Track detail** (`/track/<id>`) - full point-by-point telemetry for one
  session: time, status, real (or estimated) position, speed, IMU heading/steps,
  and every WiFi/BLE sighting seen at that exact point
- **Cleanup** (`/cleanup`) - manual tool for whatever the automatic cleanup
  passes miss (a spoofed jump too subtle to cross the speed threshold, etc).
  Three ways to select what to fix, in increasing scope:
  - **click a single point** on the track for a popup with just that point
  - **click a track segment** (the line between two consecutive points) for a
    popup with two options: "Select both endpoints" (just those two), or
    **"Expand to whole excursion"** - walks outward from whichever endpoint
    looks like the last trustworthy point and grabs the FULL run of
    consecutive corrupted points, however many there are
  - **draw a box** on the map to grab a whole cluster at once
  Any of these shows a preview (point count + which WiFi networks are tied to
  them) before you commit. Then either mark those points
  `bad_gps` (undo works immediately, but only until the next automatic
  processing run - see "Why bad_gps is now temporary" below) or permanently
  delete them right away. Either action triggers an immediate reprocessing
  pass. A **suspicious jumps** layer (yellow dashed lines, also shown on the
  main Map page) highlights consecutive points with an implausible implied
  speed (the one global threshold - see Admin) as a heuristic hint. Co-traveling
  device review has moved to the Reports page, since it's now fully
  automatic and doesn't need a manual "review candidates" step.
- ~~Point Lab~~ - removed from the menu (unlikely to be needed), but the
  routes/templates are still in the codebase - see `templates/base.html` for
  the one line to uncomment to bring it back
- **Algorithm Lab** (`/algolab`) - manually-triggered (not part of the daily
  job) pass that finds WiFi networks / BLE devices that
  reappear at essentially the same place across **multiple different
  uploaded sessions**. A network seen only once is treated very differently
  from one confirmed repeatedly, in the same spot, on separate trips - the
  latter is much more
  likely to be a real, fixed reference point. Each qualifying network/device
  gets a **stability score** based on how many distinct sessions re-confirmed
  it and how tightly clustered those sightings are, building a separate,
  prioritized "stable points" database (this is a different, complementary
  mechanism from the co-traveling exclusion on Reports - stability scoring
  ranks confidence, co-traveling detection removes ride-along noise entirely)
- **Admin** (`/admin`) - daily processing run history, the one global
  speed-outlier threshold setting (default 150 km/h - applies to the
  suspicious-jumps hint, manual Cleanup actions, and the automatic cleanup
  pass consistently), and a button to trigger a reprocess on demand

## Header: active tab + network growth chart

The header highlights the current page and shows a small bar chart (green =
added that day, red = removed) for the last 14 days, alongside the current
total network/device count - **WiFi networks + BLE
devices combined** (GPS points themselves aren't part of this count).
Powered by a daily snapshot (`daily_stats` / `network_snapshot` tables)
recorded after every upload and every processing run, so it's always current
rather than waiting for the next scheduled run.

## How processing works (jobs.py) - fully automatic

Runs after every upload, once every 24 hours, and on demand (Admin). Each run:
1. **Implausible-timestamp cleanup** - flags 'ok' points with a GPS
   cold-start bogus date (e.g. year 1999) as `bad_gps`
2. **Speed-outlier cleanup** - finds every suspicious jump (implied speed
   over the one global threshold, default 150 km/h) in every session, expands
   each to its full corrupted run, and flags them `bad_gps` - the fully
   automatic version of the interactive Cleanup segment-expansion tool
3. **Co-traveling device detection** - excludes WiFi/BLE devices seen across
   most of a session's points (car hotspot, earbuds, in-car multimedia)
   rather than a real roadside reference point's brief window
4. Rebuilds the `networks` / `ble_devices` aggregate tables
   (now correctly excluding everything flagged by steps 1-3)
5. **Far-from-track exclusion (WiFi and BLE)** - any network/device whose
   averaged position doesn't land near ANY point of the actual GPS track
   (each kind has its own global distance setting - see Admin; WiFi defaults
   to 500m, BLE to 200m, since a legitimate fixed WiFi access point is
   routinely detectable from further away than a BLE beacon) is excluded
   entirely. This catches networks/devices whose average got pulled far away
   from anywhere real by a single stray/polluted sighting in an unrelated
   session (e.g. the same BSSID/MAC seen once on two completely unrelated
   trips) - something the presence-ratio co-traveling check alone doesn't
   catch - then rebuilds `networks`/`ble_devices` once more so it takes
   effect immediately
6. **Permanently deletes every point currently marked `bad_gps`**
   (`db.purge_bad_gps_points`) - irreversible, regardless of whether it came
   from the firmware's own no-fix rows or steps 1-2 above. A summary (session,
   reason, count, rough centroid) is kept in `deletion_log` for the Reports
   page; the actual point data is gone. Runs before estimated_positions, so a
   purged point never gets one computed
7. Recomputes `estimated_positions`: for every remaining (i.e. `ok`) point,
   matches its WiFi/BLE sightings against the tables above and
   stores an RSSI-weighted centroid as a GPS-independent position estimate,
   plus the distance between that estimate and the real recorded position -
   a rough accuracy indicator, though an optimistic one (see caveat in
   `db.py`: the point's own data contributed to the average it's matched
   against, so this isn't a true leave-one-out validation)
8. Records today's `daily_stats` snapshot (powers the header chart)

This now runs synchronously right after every upload (not deferred), since
the whole point of automating cleanup is that it takes effect immediately -
the upload result page shows a summary of what got cleaned. It's also the
retroactive path for cleaning up data uploaded before any of steps 1-3/6
existed: run it once (Admin's "Run processing now") and it applies to the
whole existing database.

**Why only one gunicorn worker:** the scheduler lives in-process. Running
multiple workers would start multiple independent copies of it, causing
duplicate/overlapping daily runs and SQLite write contention. This is a
personal-scale tool, not a multi-user web service, so a single worker (already
set in the Dockerfile) is the right trade-off.

## Database schema (db.py)

- `sessions` - one row per uploaded file
- `points` - one row per log line (lat/lon/status/speed/heading_deg/steps -
  the last two are raw IMU data from the tracker's BHI260AP sensor hub;
  `bad_gps_reason`/`bad_gps_at` track why/when our own cleanup flipped a
  point, for the Reports removal log - NULL if untouched or firmware's own)
- `observations` - one row per WiFi network seen at a given point
- `networks` - aggregated WiFi table, keyed by BSSID (falls back to SSID for
  older logs with no BSSID)
- `ble_observations` - one row per BLE device seen at a given point
- `ble_devices` - aggregated BLE table, keyed by MAC address
- `estimated_positions` - one row per point, the GPS-independent estimate,
  RSSI-weighted where available (see "RSSI-based weighting" below) - in
  practice only ever has rows for `status='ok'` points now, since
  `bad_gps` points are purged before this table gets recomputed (see
  "Bad_gps points are now permanently deleted")
- `deletion_log` - permanent audit trail of purged `bad_gps` points (session,
  reason, count, rough centroid) - the underlying point data itself is gone;
  this is what the Reports removal log/summary/map read from
- `processing_runs` - history of every processing run (timestamps fixed,
  speed outliers fixed, co-traveling devices excluded, bad_gps points
  purged, etc), shown on Admin
- `daily_stats` / `network_snapshot` - per-day WiFi+BLE combined
  counts and added/removed deltas, powers the header growth chart
- `settings` - simple key-value store for global settings: `speed_threshold_kmh`
  (default 150), `wifi_track_distance_m` (default 500), and `ble_track_distance_m`
  (default 200) - the latter two are the far-from-track cleanup distances for
  each kind (WiFi's is larger by default) - all three set on Admin
- `stable_networks` / `stable_ble_devices` - the
  prioritized "stable points" databases built by Algorithm Lab (only
  networks/devices re-confirmed across multiple distinct sessions)
- `excluded_devices` - WiFi/BLE co-traveling device detections (see Reports
  page). `excluded=1` means actually removed from `networks`/`ble_devices`;
  `user_override=1` means a person explicitly restored/re-excluded it on
  Reports, which future auto-detection runs will respect instead of
  overwriting

To start over, stop the container and run `docker compose down -v` (removes
the volumes), or locally just delete `wifigps.db` and restart.

## RSSI-based weighting (db._match_weight, db._rssi_to_distance_m)

Position estimates (`estimated_positions` and Point Lab's `match_position`)
don't just average every matching network/device equally - each match is
weighted by a rough distance estimate derived from that specific sighting's
RSSI, using the standard log-distance path loss model
(`distance = 10^((A - RSSI) / (10*n))`, with generic constants `A=-50dBm`,
`n=2.5`). Stronger (closer-looking) signals count for more. This is still far
short of real trilateration - there's no ranging hardware here, and the
constants are generic rather than calibrated per device - but it's a
meaningful step up from a flat observation-count average. `/api/locate`
can't use this (the person just pastes SSID/MAC names with no RSSI), so
it stays a plain observation-count-weighted centroid.

## API endpoints

- `GET /api/track?session_id=<id>` - 'ok' track points (all sessions if omitted)
- `GET /api/networks` - all known WiFi networks with averaged position
- `GET /api/ble_devices` - all known BLE device sightings with averaged position
- `GET /api/sessions` - list of uploaded files with per-file stats
- `GET /api/track_detail/<id>` - full per-point telemetry for one session
  (powers the track detail page)
- `GET /api/locate?ssids=SSID1,SSID2,...` - rough position estimate from visible SSIDs
- `GET /api/locate?bssids=AA:BB:CC:DD:EE:FF,...` - same, matched by BSSID (more precise)
- `GET /api/locate?ble_macs=AA:BB:CC:DD:EE:FF,...` - same, matched by BLE MAC address;
  any combination of `ssids`/`bssids`/`ble_macs` can be combined into one estimate
- `POST /admin/reprocess` - triggers the daily processing job immediately
- `GET /api/daily_stats?days=14` - per-day network counts and added/removed
  deltas, powers the header chart
- `POST /api/lab/analyze` - multipart form `{logfile, use_stable}` -> per-line
  GPS/WiFi-only/BLE-only/combined position views (RSSI-weighted
  where available) plus raw heading/steps, nothing saved (Point Lab)
- `POST /api/algolab/run` - `{min_sessions?}` - (re)computes the stable-points
  databases (Algorithm Lab)
- `GET /api/stable_networks` / `GET /api/stable_ble_devices`
  - the stable-points database contents
- `GET /api/reports/removal_summary` - total points permanently deleted by
  reason (from `deletion_log` - the points themselves no longer exist), and
  devices currently excluded
- `GET /api/reports/removal_log?limit=100` - chronological cleanup log
  (deleted-point batches + device exclusions)
- `GET /api/reports/removal_points` - one marker per deleted batch (centroid
  of whatever real coordinates it had), for the Reports map
- `POST /api/reports/restore_device` - `{key}` - un-excludes a device and
  pins that choice against future auto-detection
- `POST /api/reports/detect` - `{presence_ratio_threshold?}` (default 0.6) -
  detects WiFi/BLE co-traveling device candidates (informational only)
- `GET /api/reports/candidates` - the detected candidates (see excluded_devices)
- `POST /api/reports/toggle_exclude` - `{key, excluded}` - actually
  including/excluding a candidate from the live `networks`/`ble_devices`
  aggregates; triggers an immediate reprocess
- `GET /api/reports/session_comparison?session_id=<id>` - old-vs-new algorithm
  comparison data (per-point tracks + average error) for the Reports page
- `POST /api/sync/check` - `{filenames: [...]}` -> `{missing: [...]}` - used by
  the firmware's automatic WiFi sync (see the firmware README) to only upload
  files the server doesn't already have
- `POST /api/upload` - same as the `/upload` web form, but JSON in/out instead
  of an HTML page; used by the firmware's automatic sync client
- `GET /api/suspicious_jumps?session_id=<id>&threshold_kmh=200` - heuristic hint:
  consecutive points with an implausible implied speed (not proof, just a
  candidate list for a human to check on the Cleanup page)
- `POST /api/cleanup/expand_excursion` - `{session_id, point_id_a, point_id_b, threshold_kmh?}`
  -> expands one clicked segment into the full run of consecutive corrupted
  points (see "why mark bad_gps" section below for the reasoning)
- `POST /api/cleanup/preview` - either `{point_ids: [...]}` (from clicking a
  point/segment directly) or `{min_lat, max_lat, min_lon, max_lon, session_id?}`
  (from drawing a box) -> which 'ok' points match, and which WiFi/BLE
  signals are tied to them
- `POST /api/cleanup/apply` - `{point_ids, action}` where action is
  `mark_bad_gps` (undo works immediately, but only until the next processing
  run permanently deletes it - see below) or `delete` (permanent right away)
- `POST /api/cleanup/restore` - `{point_ids}` - undoes a `mark_bad_gps` action,
  as long as no processing run has happened since

## Implausible-timestamp cleanup (server-side, no firmware change needed)

A GPS module can send a syntactically well-formed but bogus date during a
cold start (classically something like `1999-11-30`) even while the
firmware's `TinyGPSPlus` `isValid()` check says "valid" - that check only
confirms the NMEA sentence parsed correctly, not that the receiver actually
has a real time fix yet. Since a real satellite fix and a real time fix
normally arrive together in the same navigation solution, an impossible year
is a strong signal the whole point - including its "satellites >= 3"
coordinates - came from this cold-start window, not a genuine fix, even
though the firmware wrote it as `ok`.

This is handled entirely server-side (`db.fix_implausible_timestamps`,
default cutoff: any year before 2024), specifically so it never requires
reflashing the tracker:
- runs automatically on **every upload**, flipping any affected point to
  `bad_gps` before the network/device aggregates are rebuilt (the upload
  result page shows how many points this affected)
- also runs as part of the daily/manual processing job (`jobs.run_processing`,
  triggered via **Admin -> Run processing now**), so it retroactively cleans
  up data that was uploaded before this check existed - no need to re-upload
  anything
- excludes the point from the position averages immediately, and - like
  every `bad_gps` point - gets **permanently deleted** by the same
  processing run's final step (see below); nothing sits around as a
  soft-tombstone anymore

## Why "mark bad_gps" first instead of deleting immediately

`bad_gps` points are excluded from the `networks`/`ble_devices`
aggregates (those only use `status='ok'` rows) the moment they're flagged, so a
spoofed/corrupted position stops contaminating a network's averaged location
right away, before it's actually removed. The point then gets **permanently
deleted** (`db.purge_bad_gps_points`) as the last step of the very same
processing run that flagged it - whether that run was triggered by the
upload that just happened, the daily schedule, or a manual "Run processing
now". This means `mark_bad_gps` is a brief staging step, not a lasting
reversible state: the Cleanup page's Undo button only works in the short
window before the next processing run, which for automatic uploads can be
essentially immediate. A permanent record of *what* was deleted (session,
reason, count, a rough centroid) is kept in `deletion_log` for the Reports
page, even though the actual point data is gone - see "Bad_gps points are
now permanently deleted" below. The `delete` action on the Cleanup page skips
the staging step entirely and removes points right away, with no journal
entry beyond the usual manual-cleanup log.

## Bad_gps points are now permanently deleted (irreversible)

Every processing run's final step (`db.purge_bad_gps_points`) deletes every
point currently marked `bad_gps` - regardless of why it got that status: the
firmware's own no-fix rows, `fix_implausible_timestamps`,
`auto_clean_speed_outliers`, or a manual "Mark as bad_gps" on the Cleanup
page. This is a deliberate design choice (not just an implementation detail):
earlier versions kept `bad_gps` points around indefinitely as a reversible
soft-tombstone, but in practice that just accumulated permanently-unusable
rows with no upside, while making the "irreversible" delete option on Cleanup
redundant. Deleting from `points` cascades (`ON DELETE CASCADE`) to that
point's `observations` / `ble_observations` /
`estimated_positions` rows automatically.

Since the actual point data is gone, `deletion_log` keeps a lightweight,
permanent summary instead - one row per (session, reason) batch: how many
points, a centroid of whichever ones had real (non-placeholder) coordinates,
and when. The Reports page's removal log/summary and "removal map" all read
from this table, not from live points - that's *the only reason this table
exists*, since without it every processing run would otherwise erase its own
paper trail the moment it finished.

## Log file format (produced by the firmware, parsed by parser.py)

```
HH:MM:SS_lat_lon_status_speed_ssid1|bssid1|rssi1,...__mac1|rssi1,..._heading_steps
```
- `status`: `ok` or `bad_gps` (lat/lon are `0.000000` for bad_gps)
- `speed`: the firmware no longer calculates speed at all, so this is always
  `NA` for anything logged going forward - kept only for backward
  compatibility with older files that do have real values
- WiFi entries: `ssid|bssid|rssi`, comma-separated, empty list allowed
- LoRa/Meshtastic support was removed from the firmware entirely - that
  field's position in the format is still present (an empty segment between
  its surrounding `_`) so the parser can skip over it and keep BLE/heading/
  steps correctly aligned, but nothing is ever parsed out of it
- BLE entries: `<mac>|rssi`, comma-separated, empty list allowed (passive scan - many
  phones/wearables rotate their address for privacy, so only genuinely fixed devices
  build up repeat sightings)
- `heading`: the tracker's fused compass heading in degrees, or `NA`
- `steps`: the tracker's cumulative hardware step counter, or `NA` (both raw IMU data
  - no dead-reckoning math happens in the firmware itself)
- older logs missing the BLE/heading/steps fields entirely, or without
  BSSID/RSSI on WiFi entries, are still parsed correctly (see `parser.py` for the
  exact fallback rules)
