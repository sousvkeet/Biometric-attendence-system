# BioAttend for Windows

BioAttend for Windows is the complete attendance system in one Windows program. No server or database
needs to be installed: the website, the device connection (live punches, syncing users and
fingerprints), the database and scheduled backups all run inside the app on one office PC.

| | |
|---|---|
| Works on | Windows 10 (21H2+) / Windows 11, 64-bit |
| Needs | The Microsoft Edge **WebView2** runtime (built into Windows 11; on Windows 10 it comes with Edge) |
| Install rights | None. It installs for the current user only, without administrator rights. |
| Python | **Not needed.** The app contains everything it needs. |
| Data location | `%LOCALAPPDATA%\BioAttend` (e.g. `C:\Users\<you>\AppData\Local\BioAttend`) |

---

## 1. Install

1. Run **`BioAttend-Setup-1.0.0.exe`**.
   - Windows may show *"Windows protected your PC"*, because the installer is not code-signed.
   - Click **More info → Run anyway**.
2. Click **Next → Install → Finish**. This creates a desktop icon and a Start-menu entry.
3. BioAttend starts and shows the **first-time setup** page. Enter:
   - company name
   - your administrator **username**
   - **email**
   - **password**, twice

   Then click **Create administrator & start**. No setup code is needed on the PC itself.

**No-install option:** unzip **`BioAttend-portable-1.0.0.zip`** anywhere and run **`BioAttend.exe`**.
Data is stored in the same place as the installed version.

**MSI for IT departments:** **`BioAttend-1.0.0.msi`** installs BioAttend for **all users** of a PC into
`C:\Program Files\BioAttend`. It needs administrator rights and suits Group Policy, Intune and SCCM.
- Every Windows user still gets their own data in `%LOCALAPPDATA%\BioAttend`.
- Silent install: `msiexec /i BioAttend-1.0.0.msi /qn`
- Silent uninstall: `msiexec /x BioAttend-1.0.0.msi /qn`

The data is never removed by the MSI. Use **either** the MSI **or** the per-user Setup on a PC, not both.

### Digital signature

`BioAttend.exe`, `bioattend-cli.exe`, the MSI and the Setup are **digitally signed** (SHA-256, timestamped
by DigiCert). Windows shows the publisher, and any change to the files breaks the signature.

- **The self-made certificate (current builds).** Builds are signed with the company's own
  "BioAttend Code Signing" certificate. Make office PCs trust it once:
  - **One PC:** as administrator, run
    `powershell -ExecutionPolicy Bypass -File Trust-BioAttend-Certificate.ps1`
    (next to `BioAttend-CodeSigning.cer`).
  - **Many PCs:** Group Policy → Computer Configuration → Windows Settings → Security Settings →
    Public Key Policies → import `BioAttend-CodeSigning.cer` into **Trusted Root Certification Authorities**
    **and** **Trusted Publishers**.
- **No warnings anywhere.** For PCs outside your control (no SmartScreen warning at all), buy a code-signing
  certificate (e.g. Sectigo, DigiCert) or use Microsoft Azure Trusted Signing, then build with
  `-CertThumbprint <thumbprint>` or `-PfxPath cert.pfx -PfxPassword ...`.
- **Keep the certificate's private key safe.** It is in the Windows certificate store of the PC that builds
  BioAttend (Admin account). Back it up with *certmgr.msc → Personal → Certificates → BioAttend Code Signing →
  All Tasks → Export* (with private key, password-protected). Future updates must be signed with the same
  certificate to stay trusted.

### Sending BioAttend to others (Teams, Outlook, email)

**Microsoft Teams and Outlook block `.exe` and `.msi` attachments by file type.** The message *"This message
was blocked because it contains a file type that is not permitted"* is not a virus detection. Microsoft 365
blocks every installer this way, whatever is inside, and no change to the file can get past that rule.

- **Send the link instead of the file:**
  `https://github.com/sousvkeet/Biometric-attendence-system/releases/latest`
  (or the website `https://sousvkeet.github.io/Biometric-attendence-system/`).
- Inside the company, put the installer on SharePoint, OneDrive or a network share and send that link.
- Your Microsoft 365 administrator can allow `.msi` files for chosen people in the anti-malware policy
  (Microsoft Defender portal → Policies → Anti-malware → *Common attachments filter*).

### Antivirus and SmartScreen

The Windows builds are made to avoid false alarms from antivirus programs:

- **The launcher is built from source.** BioAttend compiles PyInstaller's launcher (bootloader) itself, instead
  of shipping the ready-made one that many other programs share and that antivirus heuristics often distrust.
- **No packing.** The programs are not compressed with UPX or similar tools.
- **Signed and checksummed.** Every program and installer is signed, and each release lists SHA-256 checksums
  so anyone can check that a download is unchanged.

**If an antivirus still reports BioAttend** (a false positive), send the file to the vendor for analysis. They
usually clear it within one or two days:

| Vendor | Where to report |
|---|---|
| Microsoft Defender | https://www.microsoft.com/wdsi/filesubmission (choose *Software developer*, *Incorrectly detected as malware*) |
| Others | Most vendors have a "false positive" form; [VirusTotal](https://www.virustotal.com) shows which engines report a file |

**SmartScreen** (*"Windows protected your PC"*) appears for programs from publishers that Windows does not know
yet. It is not a virus warning. It disappears when the files are signed with a **publicly trusted**
code-signing certificate and enough people have downloaded them. Options: Microsoft Artifact Signing (formerly
Trusted Signing, monthly fee), or an OV/EV certificate from Sectigo, DigiCert or Certum (Certum also offers a
low-cost certificate for open-source projects). Build with `-CertThumbprint <thumbprint>` once you have one.

---

## 2. Everyday use

- **Opening BioAttend:** use the desktop or Start-menu icon. It opens in its own window and works like
  the website.
- **BioAttend starts automatically when you sign in to Windows**, without opening a window. The window only opens
  when you click BioAttend (desktop icon, Start menu or the tray icon). The tray icon is shown next to the clock.
  To hide it under the **^** arrow, use Settings → Personalization → Taskbar → Other system tray icons.
- **BioAttend is light in the background.** It needs about 80 MB of memory, runs at *below normal* priority so
  it never slows down other programs, and waits one minute after Windows sign-in before it talks to the devices.
  The window is a separate process that starts when you open BioAttend. Closing the window frees its memory
  (about 500 MB).
- **Closing the window does not stop BioAttend.** It keeps running in the **system tray** (the green
  fingerprint icon near the clock) and keeps receiving punches from the devices. Double-click the tray
  icon to open it again.
- **Tray icon (right-click):**

| Menu item | What it does |
|---|---|
| Open BioAttend | Show the window |
| Open in web browser | Open the portal in your normal browser |
| **Back up now** | Back up immediately with the saved destinations and passphrase. No sign-in is needed, and a Windows notification shows the result. The Start menu also has **"Back up BioAttend now"**. |
| **Start with Windows** | **On by default:** BioAttend starts hidden in the tray when you sign in to Windows (no window), so punches are always collected. Untick to stop that |
| Allow other computers on the network | Lets other office PCs open BioAttend in their browser (see §4) |
| Open data folder | Opens `%LOCALAPPDATA%\BioAttend` |
| Reset administrator password… | Sets a new random password for the main administrator and shows it with a **Copy password** button (also lifts a login lockout) |
| Quit BioAttend | Stops everything. **No punches are collected until it is started again.** Punches are not lost: they stay on the devices and are downloaded on the next start. |

> The PC must be switched on, with BioAttend running, to collect punches in real time. Disable
> sleep/hibernate on this PC (Settings → System → Power).

---

## 3. First setup

1. **Settings:** company name, time format, default shift.
2. **Setup → Shifts:** create your shifts.
3. **Devices → Add device:** enter the device IP (e.g. `192.168.1.201`), port `4370` and comm key.
   - Keep **Write protection** on at first.
   - Click **Test connection**, then **Download users & fingerprints**.
   - The PC must be on the same network as the devices.
4. **Setup → Backups:** set a passphrase and add destinations. Use a local folder or USB drive **plus an
   off-site one** (Google Drive/OneDrive via rclone, S3, SFTP…). Then turn on the schedule. The rclone program is
   downloaded automatically (about 25 MB, checksum-verified) the first time you test or use an rclone destination.

Everything else (employees, fingerprint registration, live punches, attendance, leave, reports) works
exactly as described in the main [README](../README.md).

---

## 4. Use from other office PCs (optional)

1. Right-click the tray icon → **Allow other computers on the network**.
2. Quit BioAttend and start it again.
3. When **Windows Firewall** asks, allow BioAttend on **private networks**.
4. Other PCs open `http://<this-PC-name>:30009` or `http://<this-PC-IP>:30009`. The address is shown in
   the tray icon tooltip.

Traffic between PCs is plain HTTP. On a trusted office network this is acceptable. For wider access
use the server installation with HTTPS ([DEPLOYMENT.md](DEPLOYMENT.md)).

---

## 5. Update to a new version

Run the new **BioAttend-Setup** over the existing installation:
1. It closes the running app.
2. It replaces the program files.
3. It keeps all data.

The database is upgraded automatically on the next start. Take a backup first (Backups → Back up now).

## 6. Uninstall

Use **Settings → Apps → BioAttend → Uninstall**, or *Uninstall BioAttend* in the Start menu. You are asked
whether to also delete the data:
- **No** (the default) keeps everything in `%LOCALAPPDATA%\BioAttend` for a later reinstall.
- **Yes** deletes employees, attendance and fingerprints from this PC. The biometric devices themselves
  are not changed.

---

## 7. Moving to a server later

1. In the Windows app, go to **Backups → Back up now** and keep the file (or use an off-site destination).
2. Install the server ([DEPLOYMENT.md](DEPLOYMENT.md)).
3. Restore the backup there (Backups → **Upload & restore**). Desktop backups are SQLite backups:
   - restore them into an SQLite server installation, or
   - restore into SQLite first and then use `deploy/scripts/sqlite-to-postgres.sh`.
4. Quit the Windows app and turn off *Start with Windows*. **Only one BioAttend installation should
   manage the same devices.**

---

## 8. Command line (advanced)

`bioattend-cli.exe`, in the installation folder (`%LOCALAPPDATA%\Programs\BioAttend`), runs maintenance
commands on the same data:

```bat
cd %LOCALAPPDATA%\Programs\BioAttend
bioattend-cli.exe production_check --skip-devices
bioattend-cli.exe backup_now
bioattend-cli.exe restore_backup --destination "Google Drive" --latest
bioattend-cli.exe changepassword admin
```

Quit BioAttend before running `restore_backup` from the command line. Restoring from inside the app does
not need this.

---

## 9. Troubleshooting

| Problem | Solution |
|---|---|
| "Windows protected your PC" | The app is not code-signed: **More info → Run anyway** |
| Window stays blank or the app does not open | Install the **WebView2 Runtime** from Microsoft (search "WebView2 Runtime download", Evergreen x64), then start again. Meanwhile use tray → *Open in web browser*. |
| "BioAttend is already running" | It is in the tray. Double-click the tray icon (it may be hidden under the **^** arrow). |
| Forgot the admin password / account locked | Tray icon → **Reset administrator password…** |
| Devices show **offline** | Same network as the devices? `ping 192.168.1.201` in Command Prompt. Is another program (e.g. ZKTime) connected to the device? Is the comm key correct? |
| Other PCs cannot open it | Network access enabled and BioAttend restarted (§4)? Windows Firewall rule for BioAttend allowed on *Private* networks? Is the PC's network set to *Private*? |
| Port 30009 used by another program, or reserved by Windows (Hyper-V, WSL, Docker) | BioAttend automatically uses another free port and keeps it from then on (`PORT=` in `%LOCALAPPDATA%\BioAttend\config.env`). The tray tooltip shows the port. Before 1.0.9 this showed "No free network port found". |
| Something else | Logs are in `%LOCALAPPDATA%\BioAttend\data\log\` (`bioattend.log`, `errors.log`, `desktop.log`) |

---

## 10. Building the app from source (developers)

On any Windows 10/11 PC. **No Python installation is needed.** If no officially signed Python 3.12/3.13 is
installed, the script downloads the official portable Python (NuGet package from the Python Software Foundation),
checks its digital signature and uses it from the build folder:

```powershell
cd path\to\Biometric-attendence-management-system
powershell -ExecutionPolicy Bypass -File desktop\build_windows.ps1
#   optional: -Python C:\Path\to\python.exe  -Version 1.0.1
```

The script uses its own `.buildvenv` and installs nothing system-wide. It refuses Python executables that are not
signed by the Python Software Foundation. Steps:
1. Install the dependencies.
2. Draw the icon.
3. Download a checksum-verified **rclone** and the **NSIS** installer compiler.
4. Collect the static files.
5. Package with **PyInstaller**.
6. Run a smoke test on the packaged app.
7. Sign the programs.
8. Write `dist\BioAttend-<version>.msi` (WiX Toolset 5, checked against NuGet's checksum),
   `dist\BioAttend-Setup-<version>.exe`, `dist\BioAttend-portable-<version>.zip`, and the
   certificate plus trust script.

The log is in `dist\build.log`.

| File | Purpose |
|---|---|
| `desktop/bioattend_desktop.py` | The desktop launcher: window, tray, built-in web server and worker, first-run setup |
| `desktop/bioattend.spec` | PyInstaller packaging (`BioAttend.exe` + console `bioattend-cli.exe`) |
| `desktop/installer.nsi` | Installer and uninstaller (per-user) |
| `desktop/msi/BioAttend.wxs` | MSI package (per machine) |
| `desktop/Trust-BioAttend-Certificate.ps1` | Makes a PC trust the self-signed certificate |
| `desktop/build_windows.ps1` | One-step build |
| `desktop/requirements-desktop.txt` | Extra packages for the desktop app |

**Code signing (recommended for distribution):** sign `BioAttend.exe`, `bioattend-cli.exe` and the setup
file with your company's code-signing certificate (`signtool sign /fd SHA256 /a /tr http://timestamp.digicert.com /td SHA256 <file>`).
This removes the SmartScreen warning.
