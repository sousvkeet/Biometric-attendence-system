"""Linux parts of the BioAttend desktop app (used by bioattend_desktop.py).

There is no tray icon on Linux: GNOME hides tray icons, and a working tray menu needs GTK libraries that
cannot be bundled reliably. Instead the launcher (bioattend.desktop) has right-click actions - Back up now,
Reset administrator password, Open data folder, Quit - and the background service starts at login through
XDG autostart. The portal opens as an app window of Chrome / Chromium / Edge / Brave, or in the default browser.

All system programs (zenity, notify-send, xdg-open, the browser...) are started with the system's library
path, not the bundled one: PyInstaller points LD_LIBRARY_PATH at its own libraries, which can break them.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_ID = "bioattend"
APP_NAME = "BioAttend"
SYSTEM_LAUNCHER = Path("/usr/share/applications/bioattend.desktop")  # installed by the .deb package
BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge",
            "microsoft-edge-stable", "brave-browser", "brave", "vivaldi-stable", "vivaldi")
ACTIONS = [  # (id, menu text, command-line option)
    ("backup", "Back up now", "--backup"),
    ("reset-password", "Reset administrator password", "--reset-password"),
    ("data-folder", "Open data folder", "--open-data"),
    ("quit", "Quit BioAttend", "--quit"),
]


# ---------------------------------------------------------------- running system programs
def system_env():
    env = dict(os.environ)
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)  # saved by the PyInstaller bootloader
    if original:
        env["LD_LIBRARY_PATH"] = original
    else:
        env.pop("LD_LIBRARY_PATH", None)
    for key in ("PYTHONHOME", "PYTHONPATH", "_MEIPASS2"):
        env.pop(key, None)
    return env


def run(cmd, **kwargs):
    """Run a system program and wait; returns the CompletedProcess, or None if it is not installed."""
    if not shutil.which(cmd[0]):
        return None
    try:
        return subprocess.run(cmd, env=system_env(), stdin=kwargs.pop("stdin", subprocess.DEVNULL),
                              capture_output=True, text=True, **kwargs)
    except OSError:
        return None


def spawn(cmd):
    """Start a program in the background, detached from this process."""
    return subprocess.Popen(cmd, env=system_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def has_display():
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


# ---------------------------------------------------------------- messages
def icon_file():
    bundle = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    path = bundle / "desktop" / "bioattend.png"
    return str(path) if path.exists() else APP_ID


def notify(text, title=APP_NAME):
    result = run(["notify-send", "--app-name", APP_NAME, "--icon", icon_file(), title, text])
    return result is not None and result.returncode == 0


def message_box(text, title=APP_NAME, error=False):
    if has_display():
        if run(["zenity", "--error" if error else "--info", "--title", title, "--text", text, "--no-markup",
                "--width", "460"]) is not None:
            return
        if run(["kdialog", "--title", title, "--error" if error else "--msgbox", text]) is not None:
            return
        if notify(text, title):
            return
    print(f"[{title}] {text}", file=sys.stderr if error else sys.stdout)


def copy_to_clipboard(text):
    """Copy with wl-copy (Wayland), xclip or xsel - whichever is installed."""
    candidates = []
    if os.environ.get("WAYLAND_DISPLAY"):
        candidates.append(["wl-copy"])
    if os.environ.get("DISPLAY"):
        candidates += [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    for cmd in candidates:
        if shutil.which(cmd[0]):
            try:
                # xclip/xsel keep running to serve the clipboard: hand the text over and do not wait for them
                proc = subprocess.Popen(cmd, env=system_env(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True, text=True)
                proc.stdin.write(text)
                proc.stdin.close()
                return True
            except OSError:
                continue
    return False


def show_password_dialog(username, password):
    """New password, shown in a selectable field (Ctrl+C) and copied to the clipboard when possible."""
    copied = copy_to_clipboard(password)
    text = (f"New password for the administrator account\n\nUser name:  {username}\n\n"
            + ("The password below has been copied to the clipboard.\n" if copied else
               "Select the password below and press Ctrl+C to copy it.\n")
            + "Sign in with it, then change it (your name at the top right → Change password).")
    if has_display():
        if run(["zenity", "--entry", "--title", APP_NAME, "--text", text, "--entry-text", password,
                "--width", "460"]) is not None:
            return
        if run(["kdialog", "--title", APP_NAME, "--inputbox", text, password]) is not None:
            return
    message_box(f"{text}\n\nPassword: {password}")


# ---------------------------------------------------------------- launcher, autostart, data folder
def program_command():
    """How to start this program again (an AppImage: the .AppImage file, not its temporary mount)."""
    if os.environ.get("APPIMAGE"):
        return [os.environ["APPIMAGE"]]
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve().parent / "bioattend_desktop.py")]


def _exec_line(extra=""):
    def quote(part):  # Desktop Entry Specification quoting
        if any(ch in part for ch in ' \t"`$\\'):
            return '"' + part.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$") + '"'
        return part
    return " ".join(quote(p) for p in program_command()) + (f" {extra}" if extra else "")


def desktop_entry(background=False):
    lines = [
        "[Desktop Entry]", "Type=Application", f"Name={APP_NAME}", "GenericName=Biometric attendance",
        "Comment=Attendance from ZKTeco biometric devices", f"Exec={_exec_line('--background' if background else '')}",
        f"Icon={APP_ID}", "Terminal=false", "Categories=Office;", "StartupWMClass=BioAttend",
        "StartupNotify=false" if background else "StartupNotify=true",
    ]
    if background:
        lines += ["NoDisplay=true", "X-GNOME-Autostart-enabled=true"]
    else:
        lines.append("Actions=" + "".join(f"{a};" for a, _, _ in ACTIONS))
        for action, name, option in ACTIONS:
            lines += ["", f"[Desktop Action {action}]", f"Name={name}", f"Exec={_exec_line(option)}"]
    return "\n".join(lines) + "\n"


def config_home():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def data_home():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def autostart_file():
    return config_home() / "autostart" / f"{APP_ID}.desktop"


def autostart_enabled():
    return autostart_file().exists()


def set_autostart(enabled):
    path = autostart_file()
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(desktop_entry(background=True), encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def install_user_launcher():
    """AppImage / tar.gz: add BioAttend to the applications menu (the .deb package installs its own).
    Rewritten when the program moved, e.g. a newer AppImage file."""
    if SYSTEM_LAUNCHER.exists() or not getattr(sys, "frozen", False):
        return False
    launcher = data_home() / "applications" / f"{APP_ID}.desktop"
    icon = data_home() / "icons" / "hicolor" / "256x256" / "apps" / f"{APP_ID}.png"
    content = desktop_entry()
    source = Path(icon_file())
    if launcher.exists() and launcher.read_text(encoding="utf-8") == content and (icon.exists() or not source.exists()):
        return False
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(content, encoding="utf-8")
    if source.exists():
        icon.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, icon)
    run(["update-desktop-database", str(launcher.parent)])
    return True


def remove_user_integration():
    """`bioattend --uninstall`: remove the menu entry, icon and autostart created by install_user_launcher()."""
    for path in (data_home() / "applications" / f"{APP_ID}.desktop",
                 data_home() / "icons" / "hicolor" / "256x256" / "apps" / f"{APP_ID}.png", autostart_file()):
        path.unlink(missing_ok=True)


def open_folder(path):
    if run(["xdg-open", str(path)]) is None:
        message_box(f"The data folder is:\n{path}")


# ---------------------------------------------------------------- the window
def find_browser():
    preferred = os.environ.get("BIOATTEND_BROWSER")
    for name in ([preferred] if preferred else []) + list(BROWSERS):
        path = shutil.which(name)
        if path:
            return path
    return None


def open_window(url, profile_dir):
    """Open the portal as an app window (own profile, so it keeps its sign-in and behaves like an app).
    Returns the browser process, or None when the default browser was used instead."""
    browser = find_browser()
    if browser:
        return spawn([browser, f"--app={url}", f"--user-data-dir={profile_dir}", f"--class={APP_NAME}",
                      "--no-first-run", "--no-default-browser-check", "--window-size=1360,860"])
    if run(["xdg-open", url]) is None:
        import webbrowser
        webbrowser.open(url)
    return None


def focus_window():
    """Bring the open BioAttend window to the front (X11 helpers, when installed)."""
    for cmd in (["wmctrl", "-x", "-a", APP_NAME], ["xdotool", "search", "--class", APP_NAME, "windowactivate"]):
        result = run(cmd)
        if result is not None and result.returncode == 0:
            return True
    return False


def set_low_priority():
    try:
        os.nice(10)  # like Windows "below normal": never compete with the user's programs
    except OSError:
        pass
