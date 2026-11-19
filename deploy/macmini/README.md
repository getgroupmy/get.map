# get.map on the Mac mini

The Mac mini holds the whole map database (Postgres 17) and runs get.map. The route servers stay on the
NAS and send their captures to the Mac mini over the office network. GET.ride's Supabase functions reach
it through GET.route's Cloudflare tunnel.

```
 NAS: get.route pool, traffic-scraper, get-route-apple ──LAN──► Mac mini :8090 (get.map) ──► Postgres 17
 Supabase: geo function ──https──► map.getride.my ──(GET.route's tunnel on the NAS)──┘          │
                                                                    nightly backup ──► NAS share ◄┘
```

## 1. Prepare macOS (once)

- **A fixed address:** in the router, reserve an IP for the Mac mini (e.g. `192.168.1.50`).
- **Never sleep, and start again after a power cut:**

  ```bash
  sudo pmset -a sleep 0 disksleep 0 displaysleep 10 autorestart 1 womp 1
  ```

  (System Settings › Energy: "Prevent automatic sleeping" and "Start up automatically after a power failure".)
- **Log in by itself after a restart:** get.map and Postgres run as your user's launchd agents, so they start
  when that user logs in. Make a dedicated user (e.g. `getmap`) and turn on automatic login for it (System
  Settings › Users & Groups). macOS only allows automatic login with **FileVault off**. If you keep FileVault
  on, someone must type the password after every power cut, and until then nothing is captured. The route
  servers drop captures while the Mac is down; fares themselves are never affected.
- **A small UPS** for the Mac mini (and the NAS) avoids most of these restarts.
- **Updates:** turn off automatic macOS restarts for updates (System Settings › General › Software Update ›
  ⓘ). Install updates by hand on a quiet night.

## 2. Install

```bash
git clone https://github.com/getgroupmy/get.map.git ~/getmap/app
bash ~/getmap/app/deploy/macmini/setup.sh
```

This installs Postgres 17 and Python, tunes Postgres (`postgresql-getmap.conf`, sized for 16 GB of memory;
the file says what to change for 24 or 32 GB), creates the `getmap` database and applies the migrations.
It writes `~/getmap/getmap.env` with a new `CAPTURE_API_KEY` and starts get.map (port 8090) plus a nightly
backup at 03:30. Check it:

```bash
curl -s -H "X-Capture-Key: $(grep ^CAPTURE_API_KEY ~/getmap/getmap.env | cut -d= -f2)" localhost:8090/health
```

macOS asks once whether Python may accept incoming connections: allow it.

## View the data

Open **http://192.168.1.50:8090/ui** (the Mac mini's address) from any computer or phone on the office
network. Sign in as `admin` with the `UI_PASSWORD` from `~/getmap/getmap.env`. `setup.sh` makes one and
prints it. On the Mac mini itself, http://localhost:8090/ui works too.

| Tab | Shows |
|---|---|
| Overview | Searches, routes and failures over time, by app; totals; database and disk; the capture service's state |
| Map | Speed cameras (arrow = direction checked), traffic lights, jams (coloured by level), incidents and places, for the area on screen |
| Searches | Every search with filters (app, request, text, failed only). Click one for its places, routes, weather and the raw answer if one was kept. |
| Routes | Every route: time, distance, traffic, lights, cameras, weather. Click one for its map, every camera on the way (and whether it applies to that direction) and every traffic light. |
| Places | Every place with latitude and longitude, most searched first |
| Cameras & lights | All speed cameras (limit, direction, how often they're on quoted routes) and traffic lights |
| Traffic | When it jams (day × hour), the worst streets, every jam reading, Waze incidents |
| Weather | Readings with time and a summary by condition |
| Storage | Database size against the limits, size per week, retention |

Every table has **Download CSV** (up to 50,000 rows) to open in Excel or Google Sheets. The viewer only
reads: it opens the database read-only, so it can't change or delete anything.

To open it from outside the office, don't publish it on the open internet. Put it behind Cloudflare Access
(Zero Trust › Access › Applications) on a hostname such as `mapdata.getride.my`, pointing at
`http://192.168.1.50:8090`. Then only people you allow can sign in, and the password above still applies.

## 3. Connect the route servers (NAS)

In each route server's `deploy/nas/.env` on the NAS:

```
GETMAP_URL=http://192.168.1.50:8090
GETMAP_KEY=<CAPTURE_API_KEY from ~/getmap/getmap.env>
```

Then add the 2-line drop-in to each server (see [docs/INTEGRATION.md](../../docs/INTEGRATION.md)) and
restart the containers.

## 4. Let GET.ride's Supabase functions reach it

Add a public hostname to **GET.route's existing Cloudflare tunnel** (Cloudflare Zero Trust › Networks ›
Tunnels › get.route's tunnel › Public Hostname): `map.getride.my` → `http://192.168.1.50:8090`. You don't
need a second tunnel or token. Then set the Supabase secrets (`GETMAP_URL=https://map.getride.my`,
`GETMAP_KEY`, `GETMAP_CLIENT_SALT`) as in the integration guide. Every call must carry `X-Capture-Key`.

## 5. Backups to the NAS

1. On the NAS, make a shared folder (e.g. `backup`) and a user that can write to it.
2. On the Mac mini, use Finder › Go › Connect to Server › `smb://NAS_IP/backup`. Tick "Remember this password
   in my keychain", then drag the mounted share into System Settings › General › Login Items so it mounts
   at every login.
3. Make the folder `getmap` inside it. `BACKUP_DIR=/Volumes/backup/getmap` in `getmap.env` points there.

Every night at 03:30, `backup.sh` dumps the database without the raw answers, compressed, and keeps the
newest 7. See `~/getmap/logs/backup.log`. To restore:

```bash
createdb getmap_restore && pg_restore -d getmap_restore /Volumes/backup/getmap/getmap-YYYYMMDD-HHMM.dump
```

Once the database is over ~100 GB, a nightly full dump takes a long time. Then switch to pgBackRest
(incremental backups and WAL archiving to the NAS).

## Day to day

| | |
|---|---|
| Logs | `~/getmap/logs/getmap.log`, `~/getmap/logs/backup.log` |
| Restart get.map | `launchctl kickstart -k gui/$(id -u)/com.getride.getmap` |
| Update | `cd ~/getmap/app && git pull && ~/getmap/venv/bin/pip install -r requirements.txt`, then restart |
| Storage | `~/getmap/venv/bin/python -m getmap.retention` (what would be dropped; `--apply` to do it now) |
| Database size | `psql -d getmap -c "select table_name, pg_size_pretty(sum(bytes)) from map.partitions group by 1"` |

## Out of space?

You can't run out: the disk guard (in get.map, hourly) drops the oldest weeks when the database passes
`MAX_DB_GB` (300) or free space falls under `MIN_FREE_DISK_GB` (80). It drops raw answers first, then old
traffic, then old searches, and it never drops the current week. To keep more history, add an external
USB4 / Thunderbolt NVMe SSD (2-4 TB) and move Postgres onto it:

1. Stop Postgres: `brew services stop postgresql@17`.
2. Copy the data directory to the SSD with `rsync -a`.
3. Point Homebrew's `postgresql@17` plist at the new data directory, then start Postgres again.
4. In `getmap.env`, set `DISK_PATH=/Volumes/<ssd>` and raise `MAX_DB_GB`.

See [docs/STORAGE.md](../../docs/STORAGE.md) for how much a day takes.
