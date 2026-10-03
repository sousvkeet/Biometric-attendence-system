# BioAttend for Mac

BioAttend for Mac is the complete attendance system as one Mac app, like the Windows and Linux apps. The
website, the device connection (live punches, syncing users and fingerprints), the database and scheduled
backups all run on the Mac itself. No server, database or Python installation is needed.

| | |
|---|---|
| Works on | macOS 12 Monterey or newer |
| Downloads | **Apple Silicon** (M1, M2, M3, M4…): `BioAttend-<version>-macos-arm64.dmg`. **Intel** Macs: `BioAttend-<version>-macos-x86_64.dmg`. Not sure? Apple menu → About This Mac → "Chip" (Apple) or "Processor" (Intel). |
| Python | **Not needed.** The app contains everything it needs. |
| Data location | `~/Library/Application Support/BioAttend` |

---

## 1. Install

1. Open the downloaded **.dmg** and drag **BioAttend** onto the **Applications** folder.
2. Open BioAttend from **Applications** (or Launchpad).
3. **The first time only**, macOS says it *cannot verify* BioAttend, because the app is not signed with a paid
   Apple Developer ID.
   - Click **Done** (not "Move to Trash").
   - Open **System Settings → Privacy & Security**, scroll down, and click **Open Anyway** next to
     *"BioAttend" was blocked*. Confirm with your password, then click **Open Anyway** once more.
   - On macOS 14 and older you can also right-click BioAttend in Applications → **Open** → **Open**.
4. When macOS asks *"Allow BioAttend to find devices on local networks?"*, click **Allow**. BioAttend
   needs this to reach the biometric devices.
5. The **first-time setup** page opens. Enter your administrator **username**, **email** and **password**
   (twice), then click **Create administrator & start**.

**Updating:** download the new version and drag it onto Applications again (choose **Replace**). Your data is
kept. Quit BioAttend first (**Actions → Quit BioAttend completely**).

## 2. Daily use

- **Open BioAttend** from Applications, Launchpad or the Dock.
- **Closing the window (or Cmd+Q) does not stop BioAttend.** It keeps collecting punches from the devices,
  runs the scheduled syncs and backups, and starts again at login. Open the app to see the window again.
- The **Actions** menu in the menu bar:

| Action | What it does |
|---|---|
| Back up now | Backs up with the saved backup settings. No sign-in needed. |
| Reset administrator password… | Sets a new random password for the main administrator (also lifts a login lockout). It is shown in a dialog with a **Copy password** button. |
| Open data folder | Opens `~/Library/Application Support/BioAttend` in Finder |
| Open in web browser | Opens the portal in your normal browser |
| Quit BioAttend completely | Stops the background part too, until you open BioAttend again or the next login |

**Start at login** is listed in **System Settings → General → Login Items** (*Allow in the Background*), where
you can turn it off. To remove it completely, run in Terminal:
`/Applications/BioAttend.app/Contents/MacOS/BioAttend --autostart off`

Other Terminal commands (same as on Windows and Linux; `BA=/Applications/BioAttend.app/Contents/MacOS/BioAttend`):

```bash
$BA --backup             # back up now
$BA --reset-password     # new administrator password
$BA --quit               # quit completely
$BA --lan on             # allow other computers on the network (quit and open again to apply)
$BA manage <command>     # Django management command, e.g.  $BA manage production_check
```

## 3. Network access and devices

- By default only this Mac can open BioAttend (`http://127.0.0.1:30009`).
- `--lan on` (see above), then quit and open BioAttend again, lets other computers open
  `http://<this Mac's IP>:30009`. When macOS asks whether BioAttend may accept incoming network connections,
  click **Allow**.
- **The devices cannot be reached?** Check System Settings → Privacy & Security → **Local Network** →
  BioAttend is on. The Mac must reach the devices on the network (TCP port 4370 by default).

## 4. Remove

1. **Actions → Quit BioAttend completely**.
2. In Terminal: `/Applications/BioAttend.app/Contents/MacOS/BioAttend --uninstall` (removes start at login).
3. Drag BioAttend from Applications to the Trash.

Your data in `~/Library/Application Support/BioAttend` is not removed. Delete that folder to remove it too
(back it up first if you may need it).

## 5. Building the Mac app

macOS apps can only be built on a Mac:

- **GitHub:** the workflow `.github/workflows/mac-app.yml` builds both versions (Apple Silicon and Intel) on
  GitHub's Macs, on every push to the `mac-app` branch or manually from the Actions tab. Each build runs the
  tests, starts the app's background service and window once, takes a screenshot, and uploads the .dmg,
  .zip and checksums.
- **Your own Mac:** `desktop/build_macos.sh` (needs Python 3.12 or 3.13 from python.org or Homebrew). It builds
  for the Mac's own chip.

**Signing with an Apple Developer ID** (later, $99/year) removes the "cannot verify" step: sign with
`codesign --options runtime --sign "Developer ID Application: …"`, then notarize with
`xcrun notarytool submit … --wait` and `xcrun stapler staple`.
