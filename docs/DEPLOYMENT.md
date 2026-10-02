# BioAttend – Deployment & Operations Guide

This guide is for the person who installs and runs BioAttend: from a first installation to
HTTPS, backups, upgrades, monitoring and troubleshooting.

**Contents**

1. [How BioAttend is put together](#1-how-bioattend-is-put-together)
2. [Requirements](#2-requirements)
3. [Choose an installation method](#3-choose-an-installation-method)
4. [Option A – Docker Compose (recommended)](#4-option-a--docker-compose-recommended)
5. [Option B – Linux server with systemd](#5-option-b--linux-server-with-systemd)
6. [Option C – Windows PC with WSL](#6-option-c--windows-pc-with-wsl)
7. [First configuration in the portal](#7-first-configuration-in-the-portal)
8. [HTTPS](#8-https)
9. [Moving existing data](#9-moving-existing-data)
10. [Backups and disaster recovery](#10-backups-and-disaster-recovery)
11. [Upgrading](#11-upgrading)
12. [Monitoring and logs](#12-monitoring-and-logs)
13. [Go-live checklist](#13-go-live-checklist)
14. [Troubleshooting](#14-troubleshooting)
15. [Reference](#15-reference)

---

## 1. How BioAttend is put together

```
 Browsers (HR, managers, employees)
          │  HTTPS 443
          ▼
 ┌──────────────────┐     ┌─────────────────────────┐
 │ Caddy / nginx    │────▶│ Web (gunicorn, port 8090)│──┐
 │ (TLS, optional)  │     └─────────────────────────┘  │
 └──────────────────┘                                  ▼
                          ┌─────────────────────────┐  ┌────────────────────────┐
                          │ Worker (exactly ONE)     │─▶│ Database               │
                          │ device sync, live punches│  │ PostgreSQL (or SQLite) │
                          │ scheduled backups        │  └────────────────────────┘
                          └───────────┬─────────────┘
                                      │ TCP 4370 (ZKTeco protocol)
                                      ▼
                     ZKTeco devices on the office network (e.g. 192.168.1.201, .16)
```

| Part | What it does | How many |
|---|---|---|
| **Web** | The website: pages, reports, exports, settings | 1 (with several gunicorn processes) |
| **Worker** | Talks to the devices: downloads punches every few minutes, keeps a live connection for real-time punches, syncs users and fingerprints, runs scheduled backups | **Exactly one.** A second worker refuses to start. |
| **Database** | All data, including fingerprint templates | PostgreSQL recommended; SQLite works for a single small site |
| **Reverse proxy** | HTTPS certificate, forwards to the web part | Optional but recommended |

**Where data lives**

| Data | Location |
|---|---|
| Database | PostgreSQL, or `db.sqlite3` in the program folder |
| Settings and secrets | `.env` (Docker: `.env.docker`) |
| Logs, backup work area, safety copies, cache | `var/` (Docker: the `appdata` volume, mounted at `/data`) |

Only the server's own user can read `.env`, the database and `var/`. They contain personal data and
fingerprint templates.

---

## 2. Requirements

**Server**
- **Operating system:** Linux (Ubuntu 24.04+/Debian 12+ tested), or Windows 10/11 with WSL 2.
- **CPU and memory:** 2 CPU cores, 2 GB RAM. 4 GB is comfortable for PostgreSQL plus Docker.
- **Disk:** 10 GB free. About 1 MB per 1,000 punches; each backup is roughly 40% of the database.
- **Python:** 3.12 or newer, for the non-Docker installation.
- **Clock:** correct time and time zone, kept in sync with NTP. Late/early marks are only as accurate
  as the server and device clocks.

**Network**
- **To the devices:** the server must reach every device on **TCP port 4370**, outbound from the server.
  Test it from the server:
  ```bash
  nc -vz 192.168.1.201 4370       # or:  timeout 3 bash -c 'echo > /dev/tcp/192.168.1.201/4370' && echo open
  ```
- **Device addresses:** give each device a **fixed IP**, either a DHCP reservation or a static IP in the
  device menu (Comm. → Ethernet). If a device's IP changes, the portal loses it.
- **Browsers:** they reach the server on port 443 (with HTTPS) or 8090 (without).

**Devices**
- **Model:** ZKTeco devices with the standard TCP/IP protocol. Tested on an x 2008 / ZLM30_TFT running
  firmware 6.60/6.70.
- **Fingerprint algorithm:** every device must use the same version (shown on each device page),
  otherwise fingerprints can't be copied between them.

---

## 3. Choose an installation method

| | A. Docker Compose | B. Linux + systemd | C. Windows + WSL |
|---|---|---|---|
| Best for | New dedicated server or VM | Existing Linux server, no Docker | An office PC that stays on |
| Database | PostgreSQL (included) | PostgreSQL or SQLite | SQLite or PostgreSQL |
| HTTPS | Caddy included (optional profile) | Caddy or nginx | Caddy on Windows/WSL, or none |
| Starts after reboot | Yes | Yes | Needs the WSL autostart steps |
| Effort | Lowest | Medium | Medium |

The current installation runs on **Option C** (a Windows PC with WSL), started by hand with `./run.sh`.
For daily use switch to a service: pm2 (C), systemd (B) or Docker (A). That way the portal
survives closed terminals and reboots.

---

## 4. Option A – Docker Compose (recommended)

Requires Docker Engine 24+ with the Compose plugin.

```bash
# 1. Get the program (copy the folder or clone your repository) onto the server, then:
cd /opt/bioattend

# 2. Configuration
cp .env.docker.example .env.docker
chmod 600 .env.docker
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # → SECRET_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(24))"   # → database password
nano .env.docker
```

Fill in at least:

```ini
SECRET_KEY=<the long random value>
ALLOWED_HOSTS=192.168.1.50,attendance.example.com     # how people reach the server
POSTGRES_PASSWORD=<database password>
DATABASE_URL=postgres://bioattend:<database password>@db:5432/bioattend
TIME_ZONE=Asia/Kolkata
```

```bash
# 3. Build and start (database, web, worker)
docker compose up -d --build
docker compose ps                     # db healthy, web healthy, worker up

# 4. First-time setup code (needed once, on the setup page)
docker compose exec web python manage.py setup_code

# 5. Check
curl http://localhost:8090/healthz    # {"status": "ok", "database": true, "worker": true}
docker compose exec web python manage.py production_check
```

Open `http://<server-ip>:8090`. The **first-time setup** page asks for the company name, the administrator
username, **email** and password, plus the **setup code** from step 4. Change the port with
`WEB_PORT=80 docker compose up -d`.

**Add HTTPS with the included Caddy:**
1. Edit `deploy/caddy/Caddyfile.docker` and set your address (see [§8](#8-https)).
2. In `.env.docker`, set `HTTPS=true` and `CSRF_TRUSTED_ORIGINS=https://attendance.example.com`.
3. Start with:
   ```bash
   docker compose --profile https up -d
   ```
4. Optionally stop publishing port 8090 directly by removing the `ports:` section of `web`.

**Notes**
- **Reaching the devices:** the containers use Docker's default network, so outgoing connections to
  the office network (the devices) work without extra settings.
- **Where data is kept:** in the Docker volumes `pgdata` (database) and `appdata` (logs, safety copies).
  **Never run `docker compose down -v` in production**: `-v` deletes the volumes.
- **Commands:** run with `docker compose exec web python manage.py <command>`.
- **Logs:** `docker compose logs -f web worker`.

---

## 5. Option B – Linux server with systemd

The commands are for Ubuntu/Debian; adjust package names for other distributions.

```bash
# 1. System packages
sudo apt update
sudo apt install -y python3 python3-venv postgresql postgresql-client caddy
#   (rclone is optional and only needed for rclone backup destinations:
#    curl https://rclone.org/install.sh | sudo bash)

# 2. Service user and program folder
sudo useradd --system --create-home --home-dir /opt/bioattend --shell /usr/sbin/nologin bioattend
sudo cp -r /path/to/Biometric-attendence-management-system/. /opt/bioattend/
sudo chown -R bioattend:bioattend /opt/bioattend
sudo chmod 750 /opt/bioattend

# 3. Python environment
sudo -u bioattend python3 -m venv /opt/bioattend/.venv
sudo -u bioattend /opt/bioattend/.venv/bin/pip install -r /opt/bioattend/requirements.txt

# 4. PostgreSQL database
sudo -u postgres psql -c "CREATE USER bioattend WITH PASSWORD 'CHANGE-ME';"
sudo -u postgres psql -c "CREATE DATABASE bioattend OWNER bioattend;"

# 5. Configuration
sudo -u bioattend cp /opt/bioattend/.env.example /opt/bioattend/.env
sudo chmod 600 /opt/bioattend/.env
sudo -u bioattend nano /opt/bioattend/.env
#    SECRET_KEY=..., ALLOWED_HOSTS=..., TIME_ZONE=...,
#    DATABASE_URL=postgres://bioattend:CHANGE-ME@localhost:5432/bioattend

# 6. Database tables, static files, defaults (prints the one-time setup code)
cd /opt/bioattend
sudo -u bioattend .venv/bin/python manage.py migrate
sudo -u bioattend .venv/bin/python manage.py collectstatic --noinput
sudo -u bioattend .venv/bin/python manage.py setup_system

# 7. Services
sudo cp deploy/systemd/bioattend-web.service deploy/systemd/bioattend-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bioattend-web bioattend-worker
systemctl status bioattend-web bioattend-worker

# 8. HTTPS front end (see §8), then:
curl -s http://127.0.0.1:8090/healthz
sudo -u bioattend .venv/bin/python manage.py production_check
```

- **Bind address:** the web service listens on `127.0.0.1:8090` only, so the reverse proxy is the way in.
  To run without a proxy, add `Environment=GUNICORN_BIND=0.0.0.0:8090` to `bioattend-web.service`.
- **Process manager:** pm2 also works instead of systemd: `pm2 start ecosystem.config.js && pm2 save && pm2 startup`.

---

## 6. Option C – Windows PC with WSL

This is how the portal currently runs: Ubuntu in WSL 2, with the program in
`~/Biometric-attendence-management-system`.

**1. Run it as a service instead of `./run.sh`.** Inside WSL:
```bash
cd ~/Biometric-attendence-management-system
.venv/bin/python manage.py collectstatic --noinput
sudo npm install -g pm2           # pm2 is already installed on the current PC
pm2 start ecosystem.config.js
pm2 save
```
`ecosystem.config.js` runs gunicorn on `0.0.0.0:8090` plus exactly one worker. Check them with
`pm2 status`, and read the logs with `pm2 logs`.

**2. Start automatically with Windows.**
1. Turn on systemd in WSL. Put this in `/etc/wsl.conf`:
   ```ini
   [boot]
   systemd=true
   ```
   Then run `wsl --shutdown` in Windows PowerShell and open Ubuntu again.
2. Run `pm2 startup` inside WSL and execute the `sudo …` command it prints.
3. Make WSL start at Windows logon: Windows **Task Scheduler** → Create task → Trigger "At log on" →
   Action `wsl.exe` with arguments `-d Ubuntu --exec /bin/true`. Tick "Run whether user is logged on
   or not".
4. Windows power settings: never sleep. A sleeping PC downloads no punches.

**3. Let other PCs open the portal.** WSL normally hides its ports from the network. Choose one:
- **Mirrored networking** (Windows 11 22H2+). Put this in `%UserProfile%\.wslconfig`:
  ```ini
  [wsl2]
  networkingMode=mirrored
  ```
  Then run `wsl --shutdown`.
- **Port forwarding.** In an administrator PowerShell; repeat after reboots, because the WSL IP changes:
  ```powershell
  $ip = (wsl hostname -I).Trim().Split(" ")[0]
  netsh interface portproxy add v4tov4 listenport=8090 listenaddress=0.0.0.0 connectport=8090 connectaddress=$ip
  ```
- Either way, also allow the port in the Windows firewall:
  ```powershell
  New-NetFirewallRule -DisplayName "BioAttend 8090" -Direction Inbound -Protocol TCP -LocalPort 8090 -Action Allow
  ```

Then add the PC's office IP to `ALLOWED_HOSTS` in `.env`, e.g. `ALLOWED_HOSTS=localhost,127.0.0.1,192.168.1.50`,
and restart with `pm2 restart all`.

**Reaching the devices from WSL** works without any of the above (outgoing connections). This
installation already reaches 192.168.1.201 and 192.168.1.16.

---

## 7. First configuration in the portal

**First-time setup.** On a new installation every page leads to the setup page:
- **What it asks for:** company name, administrator **username**, **email** and **password** (entered twice,
  checked against the password rules).
- **Setup code (web servers only):** a one-time code, so nobody else on the network can claim a new server
  first. Find it in the log, in `var/setup-code.txt`, or with `manage.py setup_code`.
- **Afterwards:** the code is deleted and the page is never shown again.

Unattended installs can create the administrator directly instead:
`manage.py setup_system --admin-user NAME --admin-email you@example.com --admin-password '...'`.

Then, signed in as the administrator:

1. **Check your details:** user menu → Change password if needed.
2. **Settings:**
   - company name
   - time format (24-hour or AM/PM)
   - default shift
   - sync intervals (attendance every 5 min, users every 30 min)
3. **Shifts** (Setup → Shifts): create your real shifts. With "auto-detect", each day's shift is chosen
   from the first punch.
4. **Devices → Add device:**
   - IP, port 4370, comm key
   - Start with **Write protection ON**, then click **Test connection** and **Download users & fingerprints**.
   - **Preview upload (dry run)** shows exactly what an upload would change.
   - Turn write protection off only after checking that preview.
5. **Sync clock** on each device. Check first that the server's clock is correct.
6. **Backups:** set it up now, not later (see [§10](#10-backups-and-disaster-recovery)).
7. **Clean up former staff:** Employees → "mark employees with no punch in the last N days as inactive".
8. **Check:**
   ```bash
   manage.py production_check
   ```
   It must end with **0 problems**.

**Device safety rules:**
- Attendance records are never deleted from a device.
- Users are removed from a device only when an administrator deletes an employee with
  "Portal and all devices". The name must match, and device administrators need the main administrator.
- Details are in the README.

---

## 8. HTTPS

Without HTTPS, passwords and session cookies cross the office network unencrypted. Choose one:

| Situation | Setup |
|---|---|
| A public domain points to the server (e.g. `attendance.example.com`) | Caddy obtains and renews a free Let's Encrypt certificate automatically |
| Office network only, accessed by IP or internal name | Caddy with `tls internal` (own certificate authority). Install Caddy's root certificate on the office PCs (`caddy trust`, or export `/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`), or accept the browser warning once |
| Company certificate or existing nginx | `deploy/nginx/bioattend.conf` |

**Caddy (Linux):**
1. Copy `deploy/caddy/Caddyfile` to `/etc/caddy/Caddyfile`.
2. Replace `attendance.example.com` with your address. For internal use, write `https://192.168.1.50 {`
   and add a `tls internal` line inside the block.
3. Run `sudo systemctl reload caddy`.

**Then, in `.env`:**
```ini
HTTPS=true
ALLOWED_HOSTS=attendance.example.com
CSRF_TRUSTED_ORIGINS=https://attendance.example.com
```
Restart the web part. `HTTPS=true` turns on:
- secure cookies
- HSTS
- redirect to https
- trust in the proxy's `X-Forwarded-Proto` header

> Only set `HTTPS=true` when the portal really is behind HTTPS. Otherwise browsers drop the login cookie
> and nobody can sign in.

---

## 9. Moving existing data

### From SQLite to PostgreSQL (same server)
Tested with this installation's real data: 42,464 rows, about one minute, all row counts and fingerprint
templates verified.

```bash
# 1. Stop the portal (pm2 stop all / systemctl stop bioattend-web bioattend-worker / Ctrl+C on run.sh)
# 2. Create an EMPTY PostgreSQL database (see §5 step 4), then:
deploy/scripts/sqlite-to-postgres.sh postgres://bioattend:PASSWORD@localhost:5432/bioattend
# 3. Put DATABASE_URL=postgres://... into .env, start the portal, run production_check.
```
- **The script stops without changing anything** if the target already contains data, or if the row
  counts differ after the copy.
- **Your old database:** `db.sqlite3` is untouched. Keep it as a fallback; removing `DATABASE_URL`
  switches back.
- **Sign-ins:** everyone signs in again afterwards, because sessions are not copied.

### To a new server
1. On the old server, go to Backups → **Back up now**. Use an off-site destination, or download the file.
2. Install the new server (A, B or C). Use the same `SECRET_KEY` if you want stored destination passwords
   to keep working; otherwise re-enter them.
3. Restore, either in the portal (Backups → Upload & restore) or from the command line:
   ```bash
   manage.py restore_backup /path/to/bioattend-backup-…enc --passphrase '…'
   ```
4. The database type must match the backup: SQLite backups restore into SQLite, PostgreSQL into
   PostgreSQL. To change type, restore first, then use the script above.

---

## 10. Backups and disaster recovery

Set it up in **Setup → Backups** (main administrator only).

1. **Passphrase:** set an encryption passphrase and **store it outside the server** (password manager,
   sealed envelope). Encrypted backups cannot be restored without it.
2. **Destinations:** add at least two, following the **3-2-1 rule**: 3 copies, 2 kinds of storage,
   1 off-site:
   - **local folder or NAS:** fast restores
   - **off-site:** S3 / Backblaze / Wasabi, SFTP to another site, or rclone → Google Drive / OneDrive
3. **Test** each destination, then click **Back up now**.
4. **Schedule:** daily at night is a good default. "Keep last" 14–30 per destination.
5. **Login details:** backups leave out user accounts and passwords by default. On SQLite, tick
   "Include user accounts & passwords" for complete backups; PostgreSQL backups always include them.
   Restores keep the current accounts by default either way.
   After a total loss with a backup that has no login details, the **first-time setup** page creates the
   administrator again.
6. **Restore drill, once a month:** restore the latest backup on a test machine or a copy, and check
   that recent employees and punches are there. A backup that has never been restored is not proven.

**What a restore does:**
1. Pauses the portal and shows a progress screen to everyone.
2. Backs up the current data first, by default.
3. Downloads, decrypts and integrity-checks the backup. Anything that fails up to here changes nothing.
4. Keeps a safety copy of the current database (Backups → Undo).
5. Replaces and upgrades the database.
6. Re-downloads attendance from the devices, so punches made after the backup are recovered.

**After a total server loss:**
1. Install a new server (A, B or C).
2. Add the off-site destination, with the same credentials, in the fresh portal.
3. Run Backups → 📂 → **Restore**, or from the command line:
   ```bash
   manage.py restore_backup --destination "NAME" --latest --passphrase '…'
   ```

The devices keep their users, fingerprints and the full punch history, so the restore plus one
attendance download brings everything back.

---

## 11. Upgrading

Always back up first.

**Docker:**
```bash
cd /opt/bioattend
# copy/pull the new program version over the folder (keep .env.docker)
docker compose up -d --build          # migrations run automatically when the web container starts
docker compose exec web python manage.py production_check
```

**systemd / pm2 / WSL:**
```bash
cd /opt/bioattend                     # or ~/Biometric-attendence-management-system
# copy/pull the new program version (keep .env, db.sqlite3 and var/)
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart bioattend-web bioattend-worker      # or: pm2 restart all
.venv/bin/python manage.py production_check
```

**Rollback:** put the previous program version back, then restore the backup taken before the upgrade
(Backups → Undo also works for the last restore).

---

## 12. Monitoring and logs

**Health check:** `GET /healthz` needs no login and returns no personal data.
- `200 {"status": "ok", "database": true, "worker": true}` means everything is fine.
- `worker: false` means the device worker is not running: punches are not downloaded and backups do not run.
- `503` means the database is unreachable, or a restore is in progress.

Point an uptime monitor (Uptime Kuma, UptimeRobot, Zabbix…) at it and alert on non-200 responses or
`"worker": false`.

**In the portal:**
- **Top bar:** shows "Sync worker online/offline".
- **Dashboard:** device status and live connections.
- **Sync jobs:** every device conversation with its log.
- **Activity log:** administrator actions and login lockouts.

**Error e-mails:** set `EMAIL_*` and `ADMIN_EMAILS` in `.env`. Server errors and failed jobs logged as
errors are then mailed.

**Log files:**

| Installation | Where |
|---|---|
| systemd / pm2 / WSL | `var/log/bioattend.log` (everything) and `var/log/errors.log` (errors only); 10 × 10 MB rotation |
| systemd extra | `journalctl -u bioattend-web -u bioattend-worker -f` |
| pm2 | `pm2 logs` |
| Docker | `docker compose logs -f web worker` |

Run `manage.py production_check` after every change and once a month.

---

## 13. Go-live checklist

- [ ] `production_check` ends with **0 problems**.
- [ ] `DEBUG=false`, a long random `SECRET_KEY`, `ALLOWED_HOSTS` without `*`.
- [ ] HTTPS in front, with `HTTPS=true` and `CSRF_TRUSTED_ORIGINS` set.
- [ ] Web and worker run as services and **restart after a reboot** (reboot once to verify).
- [ ] Exactly one worker running.
- [ ] Every device reachable, with a fixed IP; device and server clocks correct.
- [ ] Write protection reviewed per device; a dry-run upload checked.
- [ ] Backups:
  - [ ] encrypted, passphrase stored off the server
  - [ ] a local and an off-site destination
  - [ ] scheduled
  - [ ] one test restore done
- [ ] Administrator password changed. Staff accounts use their own logins: HR as staff, only you as superuser.
- [ ] Health monitoring and (optionally) error e-mails configured.
- [ ] `.env`, the database and `var/` are not readable by other users (`production_check` verifies this).

---

## 14. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Port 8090 is already in use` (run.sh) | The portal is already running (check `pm2 status`), or another program uses the port: `PORT=8091 ./run.sh` |
| Top bar shows **Sync worker offline** | Start the worker (`pm2 start bioattend-worker` / `systemctl start bioattend-worker` / `docker compose up -d worker`). Read its log. |
| `Another BioAttend worker is already running` | Only one is allowed. Stop the extra one, or the old `run.sh`. |
| **Bad Request (400)** on every page | The address you typed is not in `ALLOWED_HOSTS`; add it and restart |
| **CSRF verification failed** after enabling HTTPS | Set `CSRF_TRUSTED_ORIGINS=https://your-address`; restart |
| Nobody can sign in after `HTTPS=true` | The portal is not actually served over HTTPS. Fix the proxy, or set `HTTPS=false` |
| "Too many failed sign-in attempts" | Wait 15 minutes (`LOGIN_LOCK_MINUTES`). Lockouts are listed in the Activity log. |
| Device **offline** | Ping the IP; test TCP 4370 from the server (§2); check the device's comm key; check that no other program (e.g. ZKTime) holds the device. After a restart, devices can refuse connections for 1–2 minutes. |
| Live punches don't appear, but the 5-minute download works | Devices → Edit → "Live punches" on; check the dashboard chip; some firmware drops idle connections, and the worker reconnects automatically |
| Wrong late/early marks | Server or device clock wrong (Devices → Sync clock), or the shift times / auto-detect windows need adjusting |
| Fingerprint copied but not recognised on another device | The devices use different fingerprint algorithm versions (shown on the device pages) |
| Portal stuck on "Restoring the database…" | It unlocks itself within 2 minutes if the restore process died. Then see Backups for the result, and **Undo** if the database was replaced. |
| Static files (layout) missing | `manage.py collectstatic --noinput`, then restart |
| `Set a long random SECRET_KEY` on start | `.env` is missing or not readable by the service user |

---

## 15. Reference

**Management commands** (`.venv/bin/python manage.py …` or `docker compose exec web python manage.py …`)

| Command | Purpose |
|---|---|
| `setup_system [--company …] [--device-ip … --read-only]` | Default shift and leave types; prints the setup code (optional `--admin-user/--admin-email/--admin-password` for unattended installs) |
| `setup_code` | Print the one-time code for the first-time setup page |
| `run_worker` | The device worker (normally run by the service manager) |
| `production_check [--skip-devices]` | Go-live and health checklist; exit code 1 when something must be fixed |
| `backup_now [--with-logins/--without-logins]` | Back up now to all enabled destinations (no sign-in needed) |
| `restore_backup FILE` / `--destination NAME --list` / `--latest` [`--restore-logins`] | Restore from the command line (stop the portal first); keeps current logins unless `--restore-logins` |
| `migrate`, `collectstatic --noinput`, `createsuperuser`, `changepassword USER` | Standard Django maintenance |

**Settings (`.env` / `.env.docker`)**

| Variable | Default | Meaning |
|---|---|---|
| `SECRET_KEY` | — (required) | Long random value; also encrypts stored destination credentials |
| `DEBUG` | `false` | Never `true` in production |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Addresses used to open the portal |
| `HTTPS` / `CSRF_TRUSTED_ORIGINS` | `false` / empty | See §8 |
| `DATABASE_URL` | SQLite `db.sqlite3` | `postgres://user:pass@host:5432/db` |
| `TIME_ZONE` | `Asia/Kolkata` | Time zone of the device clocks |
| `LOGIN_MAX_FAILURES` / `LOGIN_LOCK_MINUTES` | `5` / `15` | Login lockout |
| `LOG_TO_FILE` | `true` | `false` = console only (Docker) |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_FROM`, `ADMIN_EMAILS` | — | Error e-mails |
| `VAR_DIR` | `./var` | Writable data folder |
| `RCLONE_BINARY` | `var/bin/rclone` or `rclone` | rclone program |
| `GUNICORN_BIND`, `WEB_CONCURRENCY` | `127.0.0.1:8090`, CPU-based | Web server address and processes |

**Deployment files**

| File | Purpose |
|---|---|
| `Dockerfile`, `docker-compose.yml`, `deploy/docker/entrypoint.sh`, `.env.docker.example` | Docker installation |
| `deploy/gunicorn.conf.py` | Web server settings |
| `deploy/systemd/*.service` | systemd services |
| `deploy/caddy/Caddyfile`, `deploy/caddy/Caddyfile.docker`, `deploy/nginx/bioattend.conf` | HTTPS reverse proxy |
| `ecosystem.config.js` | pm2 |
| `deploy/scripts/sqlite-to-postgres.sh` | Move data from SQLite to PostgreSQL |
| `run.sh` | Development / quick start (`./run.sh`, `./run.sh prod`) |
