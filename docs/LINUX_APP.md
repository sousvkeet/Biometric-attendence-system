# BioAttend for Linux

BioAttend for Linux is the complete attendance system as one desktop app. It works like the Windows app:
the website, the device connection (live punches, syncing users and fingerprints), the database and
scheduled backups all run inside the app on one office PC. No server, database or Python installation is
needed.

| | |
|---|---|
| Works on | 64-bit (x86_64) Linux with glibc 2.34 or newer: Ubuntu 22.04+, Debian 12+, Linux Mint 21+, Fedora 35+, RHEL / Rocky / Alma 9+, openSUSE Tumbleweed |
| Window | An app window of **Chrome, Chromium, Edge or Brave** (whichever is installed). Without one of these, the portal opens in your default browser. |
| Python | **Not needed.** The app contains everything it needs. |
| Data location | `~/.local/share/BioAttend` (one folder per Linux user) |

---

## 1. Install

Pick **one** of these downloads from the
[Releases page](https://github.com/sousvkeet/Biometric-attendence-system/releases/latest).

| Download | For | How |
|---|---|---|
| **`bioattend_<version>_amd64.deb`** | Ubuntu, Debian, Mint | `sudo apt install ./bioattend_<version>_amd64.deb` |
| **`bioattend-<version>-1.x86_64.rpm`** | Fedora, RHEL, Rocky, Alma | `sudo dnf install ./bioattend-<version>-1.x86_64.rpm` |
| **`BioAttend-<version>-x86_64.AppImage`** | Any distribution, no installation | `chmod +x BioAttend-*.AppImage` and run it |
| **`BioAttend-<version>-linux-x86_64.tar.gz`** | Any distribution, no installation | `tar xzf BioAttend-*.tar.gz` and run `BioAttend-<version>/bioattend/bioattend` |

On the first start BioAttend:
- adds itself to the applications menu (AppImage and tar.gz; the .deb and .rpm do this on install),
- starts automatically at login from then on, hidden in the background,
- opens the **first-time setup** page. Enter your administrator **username**, **email** and **password**
  (twice), then click **Create administrator & start**.

**AppImage on older systems:** if it says *"dlopen(): error loading libfuse.so.2"* or *"fuse: device not
found"*, install FUSE (`sudo apt install fuse3` / `sudo dnf install fuse3`), or start it with
`./BioAttend-*.AppImage --appimage-extract-and-run`.

**Updating:** install the new .deb / .rpm the same way, or replace the AppImage / tar.gz folder. Your data is kept.
With a new AppImage file, start the new file once: the menu entry and autostart then point at it.

## 2. Daily use

- **Open BioAttend** from the applications menu. If it is already running in the background, the window
  simply opens again.
- **Closing the window does not stop BioAttend.** It keeps collecting punches from the devices, runs the
  scheduled syncs and backups, and starts again at login.
- **Right-click the BioAttend launcher** (in the dock or the applications menu) for:

| Action | What it does |
|---|---|
| Back up now | Backs up with the saved backup settings. No sign-in needed. |
| Reset administrator password | Sets a new random password for the main administrator (also lifts a login lockout). It is shown in a dialog and copied to the clipboard when `xclip`, `xsel` or `wl-clipboard` is installed. |
| Open data folder | Opens `~/.local/share/BioAttend` |
| Quit BioAttend | Stops the app until you open it again (or until the next login) |

Linux has no tray icon for BioAttend: GNOME hides tray icons, and a tray menu would need extra system
libraries. The launcher actions above do the same job.

The same actions work from a terminal:

```bash
bioattend --backup            # back up now
bioattend --reset-password    # new administrator password
bioattend --open-data         # open the data folder
bioattend --quit              # quit
bioattend --autostart off     # do not start at login (on to turn it back on)
bioattend --lan on            # allow other computers on the network (quit and start again to apply)
bioattend manage <command>    # Django management command, e.g.  bioattend manage production_check
```

(AppImage: use `./BioAttend-*.AppImage --backup` and so on. tar.gz: `./bioattend/bioattend --backup`.)

## 3. Network access, devices and firewall

- By default only this PC can open BioAttend (`http://127.0.0.1:30009`).
- `bioattend --lan on`, then quit and start BioAttend again, lets other computers open
  `http://<this PC's IP>:30009`. If a firewall is active, allow the port:
  `sudo ufw allow 30009/tcp` (Ubuntu) or `sudo firewall-cmd --add-port=30009/tcp --permanent && sudo firewall-cmd --reload` (Fedora/RHEL).
- The PC must reach the biometric devices on the network (TCP port 4370 by default).

## 4. Choosing the window's browser

BioAttend uses the first of these it finds: Google Chrome, Chromium, Microsoft Edge, Brave, Vivaldi. To use a
specific one, set `BIOATTEND_BROWSER`, e.g. in `~/.profile`: `export BIOATTEND_BROWSER=chromium`.
The window has its own browser profile (`~/.local/share/BioAttend/browser`), so it keeps you signed in and
does not mix with your normal browsing.

## 5. Remove

- **.deb:** `sudo apt remove bioattend`
- **.rpm:** `sudo dnf remove bioattend`
- **AppImage / tar.gz:** run it with `--uninstall` (removes the menu entry and autostart), then delete the file
  or folder.

Your data in `~/.local/share/BioAttend` is never removed automatically. Delete that folder to remove it too
(back it up first if you may need it).

## 6. Building the Linux app

On any Linux PC with Docker:

```bash
desktop/build_linux.sh            # version from desktop/bioattend_desktop.py
```

The build has two stages in containers:

1. **AlmaLinux 9** builds the app and the .rpm. Its glibc 2.34 sets the oldest supported systems, and the
   app bundles RHEL 9's maintained OpenSSL 3. Django needs SQLite 3.37+, but RHEL 9 has 3.34, so the build
   compiles a pinned SQLite release from sqlite.org (SHA3-256 checked). Before packaging, it starts the built
   app, checks that the portal answers, and quits it again.
2. **Debian 12** builds the .deb, the AppImage and the .tar.gz with Debian's own tools.

Everything goes to `dist/linux/`, with `SHA256SUMS-linux-<version>.txt`. The AppImage runtime comes from the
AppImage project and is checked against the SHA-256 digest that GitHub publishes for it.
