"""macOS parts of the BioAttend desktop app (used by bioattend_desktop.py).

How it fits together on a Mac:
* Opening BioAttend.app shows the portal in a native window (pywebview / WebKit). That process is "the app"
  for macOS: clicking the Dock icon while it is open brings the window to the front, Cmd+Q closes it.
* The web server, device worker and backups run in a separate background process (the "service"), started
  by the app when needed and at login by a LaunchAgent. Closing the window does not stop it.
* The BioAttend menu in the menu bar has the actions the Windows tray icon offers (Back up now, Reset
  administrator password, Open data folder, Quit BioAttend completely).

Dialogs and notifications use osascript; texts are passed as arguments, never pasted into the script.
"""
import os
import plistlib
import subprocess
import sys
from pathlib import Path

APP_NAME = "BioAttend"
BUNDLE_ID = "com.bioattend.app"


def _osascript(lines, *args):
    cmd = ["osascript"]
    for line in ["on run argv", *lines, "end run"]:
        cmd += ["-e", line]
    try:
        return subprocess.run(cmd + ["--", *args], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None


def notify(text, title=APP_NAME):
    result = _osascript(["display notification (item 1 of argv) with title (item 2 of argv)"], text, title)
    return result is not None and result.returncode == 0


def message_box(text, title=APP_NAME, error=False):
    icon = "stop" if error else "note"
    result = _osascript([f'display dialog (item 1 of argv) with title (item 2 of argv) buttons {{"OK"}} '
                         f'default button "OK" with icon {icon}'], text, title)
    if result is None or result.returncode not in (0, 1):  # 1 = dialog dismissed
        print(f"[{title}] {text}", file=sys.stderr if error else sys.stdout)


def copy_to_clipboard(text):
    try:
        subprocess.run(["pbcopy"], input=text, text=True, check=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def show_password_dialog(username, password):
    text = (f"New password for the administrator account\n\nUser name:   {username}\nPassword:    {password}\n\n"
            "Sign in with it, then change it (your name at the top right → Change password).")
    result = _osascript(['set answer to display dialog (item 1 of argv) with title "BioAttend" '
                         'buttons {"Close", "Copy password"} default button "Copy password" with icon note',
                         "return button returned of answer"], text)
    if result is not None and result.stdout.strip() == "Copy password":
        if copy_to_clipboard(password):
            notify("Password copied - paste it with Cmd+V.")
    elif result is None or result.returncode not in (0, 1):
        print(f"[{APP_NAME}] user {username}: new password {password}")


# ---------------------------------------------------------------- the app bundle and the service
def bundle_path():
    """/Applications/BioAttend.app when running from the bundle, else None."""
    exe = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False) and exe.parent.name == "MacOS" and exe.parents[1].name == "Contents":
        return exe.parents[2]
    return None


def running_from_disk_image():
    """Started straight from the downloaded .dmg (or a quarantined copy macOS runs from a temporary place)."""
    path = str(Path(sys.executable).resolve())
    return path.startswith("/Volumes/") or "/AppTranslocation/" in path


def program_command():
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve().parent / "bioattend_desktop.py")]


def start_service():
    """Start the background service (web server + device worker) as its own process."""
    subprocess.Popen(program_command() + ["--background", "--now"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def open_app():
    """Show the window: open the app bundle (macOS brings an open window to the front)."""
    bundle = bundle_path()
    try:
        if bundle is not None:
            subprocess.Popen(["open", str(bundle)])
        else:
            subprocess.Popen(program_command())
    except OSError:
        pass


def open_folder(path):
    try:
        subprocess.Popen(["open", str(path)])
    except OSError:
        message_box(f"The data folder is:\n{path}")


# ---------------------------------------------------------------- start at login (LaunchAgent)
def launch_agent_file():
    return Path.home() / "Library" / "LaunchAgents" / f"{BUNDLE_ID}.plist"


def autostart_enabled():
    return launch_agent_file().exists()


def set_autostart(enabled):
    """A LaunchAgent starts the service at login (System Settings > General > Login Items lists it too).
    It is not loaded now - the service is already running - only from the next login on."""
    path = launch_agent_file()
    if not enabled:
        path.unlink(missing_ok=True)
        return
    if running_from_disk_image():
        return  # would point at the .dmg, which is gone after ejecting it
    agent = {
        "Label": BUNDLE_ID,
        "ProgramArguments": program_command() + ["--background"],
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LimitLoadToSessionType": "Aqua",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    data = plistlib.dumps(agent)
    if not path.exists() or path.read_bytes() != data:
        path.write_bytes(data)


def set_low_priority():
    try:
        os.nice(10)  # like Windows "below normal": never compete with the user's programs
    except OSError:
        pass
