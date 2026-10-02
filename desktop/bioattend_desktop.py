"""BioAttend for Windows – the whole system in one desktop app (no server needed).

Runs the web portal (waitress, localhost), the device worker (live punches, syncs, scheduled backups) and the
SQLite database inside this process, shows the portal in a WebView2 window and keeps working from the system
tray when the window is closed.

    BioAttend.exe                 start (or bring the running app to the front)
    BioAttend.exe --background    start hidden in the tray (used for "Start with Windows")
    BioAttend.exe --quit          ask the running app to quit (used by the installer)
    bioattend-cli.exe <command>   Django management command, e.g.  bioattend-cli.exe production_check
"""
import json
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_NAME = "BioAttend"
APP_VERSION = "1.0.1"
DEFAULT_PORT = 30009
OLD_DEFAULT_PORTS = {"8090"}  # earlier versions used 8090
IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------- paths and configuration
def app_home():
    base = os.environ.get("BIOATTEND_HOME")
    if not base:
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
        base = os.path.join(root, APP_NAME)
    path = Path(base)
    for sub in ("", "data", "data/log", "run", "webview"):
        (path / sub).mkdir(parents=True, exist_ok=True)
    return path


HOME = app_home()
CONFIG_FILE = HOME / "config.env"
CONTROL_FILE = HOME / "run" / "control.json"


def read_config():
    cfg = {}
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                cfg[key.strip()] = value.strip()
    return cfg


def write_config(cfg):
    lines = ["# BioAttend desktop settings (created automatically). Keep this file private.",
             *(f"{k}={v}" for k, v in cfg.items())]
    CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def local_time_zone():
    try:
        from tzlocal import get_localzone_name
        return get_localzone_name() or "Asia/Kolkata"
    except Exception:
        return "Asia/Kolkata"


def ensure_config():
    cfg = read_config()
    changed = False
    defaults = {"SECRET_KEY": secrets.token_urlsafe(48), "TIME_ZONE": local_time_zone(),
                "PORT": str(DEFAULT_PORT), "LAN_ACCESS": "false", "AUTOSTART": "true"}
    for key, value in defaults.items():
        if not cfg.get(key):
            cfg[key], changed = value, True
    if cfg.get("PORT") in OLD_DEFAULT_PORTS:  # move installs that still use the old default port
        cfg["PORT"], changed = str(DEFAULT_PORT), True
    if changed:
        write_config(cfg)
    return cfg


def lan_addresses():
    names = {"localhost", "127.0.0.1", socket.gethostname()}
    try:
        names.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return sorted(n for n in names if n)


def bundle_dir():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def configure_environment(cfg):
    lan = cfg.get("LAN_ACCESS", "false").lower() == "true"
    env = {
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "SECRET_KEY": cfg["SECRET_KEY"],
        "DEBUG": "false",
        "TIME_ZONE": cfg["TIME_ZONE"],
        "SQLITE_PATH": str(HOME / "bioattend.sqlite3"),
        "VAR_DIR": str(HOME / "data"),
        "LOG_TO_FILE": "true",
        "ALLOWED_HOSTS": ",".join(lan_addresses() if lan else ["localhost", "127.0.0.1"]),
        "BIOATTEND_DESKTOP": "1",
    }
    rclone = bundle_dir() / "rclone.exe"
    if rclone.exists():
        env["RCLONE_BINARY"] = str(rclone)
    os.environ.update(env)
    return lan


def redirect_std_streams():
    """A windowed .exe has no console: send print()/command output to a log file instead of crashing."""
    if sys.stdout is None or sys.stderr is None:
        log = open(HOME / "data" / "log" / "desktop.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log


# ---------------------------------------------------------------- talking to an already running instance
def send_to_running(command):
    try:
        info = json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
        with socket.create_connection(("127.0.0.1", info["control_port"]), timeout=5) as s:
            s.sendall(f"{info['token']} {command}\n".encode())
            return s.recv(64).decode().strip() == "ok"
    except (OSError, ValueError, KeyError):
        return False


class ControlServer(threading.Thread):
    """Tiny local channel so a second launch can bring this window to the front (or ask it to quit)."""

    def __init__(self, app):
        super().__init__(daemon=True, name="control")
        self.app = app
        self.token = secrets.token_hex(16)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)

    def publish(self, web_port):
        CONTROL_FILE.write_text(json.dumps({"control_port": self.sock.getsockname()[1], "token": self.token,
                                            "web_port": web_port, "pid": os.getpid()}), encoding="utf-8")

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    token, command = conn.recv(256).decode().strip().split(" ", 1)
                except ValueError:
                    continue
                if not secrets.compare_digest(token, self.token):
                    continue
                conn.sendall(b"ok")
                if command == "show":
                    self.app.show()
                elif command == "backup":
                    self.app.backup_now()
                elif command == "quit":
                    threading.Thread(target=self.app.quit, daemon=True).start()


# ---------------------------------------------------------------- Windows helpers
def message_box(text, title=APP_NAME, error=False):
    if IS_WINDOWS:
        import ctypes
        flags = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, text, title, flags | 0x10000)  # MB_SETFOREGROUND
    else:
        print(f"[{title}] {text}")


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enabled():
    if not IS_WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{sys.executable}" --background')
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def apply_autostart(cfg):
    """Start with Windows (hidden in the tray) unless the user turned it off; always point at this program."""
    if not IS_WINDOWS or not getattr(sys, "frozen", False):
        return
    try:
        set_autostart(cfg.get("AUTOSTART", "true") == "true")
    except OSError:
        pass


KNOWN_FOLDERS = {  # how Windows stores paths in the tray-icon settings
    "{6D809377-6AF0-444B-8957-A3773F02200E}": "ProgramW6432",   # Program Files (64-bit)
    "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}": "ProgramFiles(x86)",
    "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}": "LOCALAPPDATA",
}


def expand_tray_path(path):
    for guid, env in KNOWN_FOLDERS.items():
        if path.upper().startswith(guid):
            return os.path.join(os.environ.get(env, ""), path[len(guid):].lstrip("\\"))
    return path


def show_tray_icon_on_taskbar():
    """Windows 11 puts new tray icons in the hidden (^) area. Show ours next to the clock once - the user can
    change it back in Settings > Personalization > Taskbar > Other system tray icons."""
    if not IS_WINDOWS:
        return False
    import winreg
    me = os.path.normcase(os.path.abspath(sys.executable))
    root = r"Control Panel\NotifyIconSettings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root) as key:
            count = winreg.QueryInfoKey(key)[0]
            names = [winreg.EnumKey(key, i) for i in range(count)]
        for name in names:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{root}\{name}", 0,
                                winreg.KEY_READ | winreg.KEY_SET_VALUE) as sub:
                try:
                    exe = winreg.QueryValueEx(sub, "ExecutablePath")[0]
                except OSError:
                    continue
                if os.path.normcase(os.path.abspath(expand_tray_path(exe))) == me:
                    winreg.SetValueEx(sub, "IsPromoted", 0, winreg.REG_DWORD, 1)
                    return True
    except OSError:
        pass
    return False


def free_port(preferred, host):
    for port in [preferred, *range(preferred + 1, preferred + 50)]:
        with socket.socket() as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free network port found")


# ---------------------------------------------------------------- the application
def run_backup_text(trigger):
    """Run a backup now; returns (message, is_error)."""
    from backups.engine import BackupError, backup_now
    try:
        run = backup_now(trigger=trigger)
    except BackupError as exc:
        return f"Backup not started:\n\n{exc}", True
    except Exception as exc:  # unexpected
        return f"Backup failed:\n\n{exc}", True
    if run.status == "success":
        return f"Backup complete: {run.file_name}", False
    failed = ", ".join(r["name"] for r in run.results.values() if not r["ok"])
    if run.status == "partial":
        return f"Backup saved, but these destinations failed: {failed}. See Backups in BioAttend.", True
    return "Backup failed - open BioAttend → Backups for details.", True


class DesktopApp:
    def __init__(self, background):
        self.background = background
        self.window = None
        self.icon = None
        self.server = None
        self.stop = threading.Event()
        self.quitting = False

    # -- startup
    def start_backend(self, cfg, lan):
        import django
        django.setup()
        from django.core.management import call_command
        call_command("migrate", interactive=False, verbosity=0)
        from core.firstrun import create_defaults
        create_defaults()  # the administrator is created on the first-time setup page in the window

        from waitress import create_server
        from django.core.wsgi import get_wsgi_application
        host = "0.0.0.0" if lan else "127.0.0.1"
        self.port = free_port(int(cfg.get("PORT") or DEFAULT_PORT), host)
        self.server = create_server(get_wsgi_application(), host=host, port=self.port, threads=8,
                                    ident=APP_NAME, channel_timeout=300)
        threading.Thread(target=self.server.run, daemon=True, name="web").start()
        threading.Thread(target=self._run_worker, daemon=True, name="worker").start()
        self.url = f"http://127.0.0.1:{self.port}/"

    def _run_worker(self):
        from core.management.commands.run_worker import Command
        while not self.stop.is_set():
            try:
                Command().serve(self.stop)
            except Exception:
                import logging
                logging.getLogger("core").exception("Worker stopped unexpectedly; restarting in 10 s")
                self.stop.wait(10)

    # -- window and tray
    def show(self):
        if self.window is not None:
            self.window.show()
            self.window.restore()

    def _on_closing(self):
        if self.quitting:
            return True
        self.window.hide()  # keep running in the tray (device sync and live punches continue)
        if not getattr(self, "_told_tray", False):
            self._told_tray = True
            if self.icon is not None:
                try:
                    self.icon.notify("BioAttend keeps running in the system tray. Right-click the icon to quit.",
                                     APP_NAME)
                except Exception:
                    pass
        return False

    def build_tray(self):
        import pystray
        from PIL import Image
        icon_path = bundle_dir() / "desktop" / "bioattend.ico"
        image = Image.open(icon_path) if icon_path.exists() else Image.new("RGB", (64, 64), (79, 154, 44))
        item = pystray.MenuItem
        menu = pystray.Menu(
            item("Open BioAttend", lambda: self.show(), default=True),
            item("Open in web browser", lambda: webbrowser.open(self.url)),
            item("Back up now", lambda: self.backup_now()),
            pystray.Menu.SEPARATOR,
            item("Start with Windows", self._toggle_autostart, checked=lambda _: autostart_enabled()),
            item("Allow other computers on the network", self._toggle_lan,
                 checked=lambda _: read_config().get("LAN_ACCESS") == "true"),
            item("Open data folder", lambda: os.startfile(HOME) if IS_WINDOWS else None),
            item("Reset administrator password…", self._reset_admin_password),
            pystray.Menu.SEPARATOR,
            item(f"Quit {APP_NAME}", lambda: self.quit()),
        )
        self.icon = pystray.Icon(APP_NAME, image, f"{APP_NAME} – running (port {self.port})", menu)
        self.icon.run_detached()
        threading.Thread(target=self._promote_tray_icon_once, daemon=True, name="tray-promote").start()

    def _promote_tray_icon_once(self):
        cfg = read_config()
        if cfg.get("TRAY_ICON_SHOWN") == "true":
            return
        for _ in range(10):  # Windows registers the icon a moment after it appears
            time.sleep(3)
            if show_tray_icon_on_taskbar():
                cfg = read_config()
                cfg["TRAY_ICON_SHOWN"] = "true"
                write_config(cfg)
                return

    def _toggle_autostart(self, icon, item):
        enabled = not autostart_enabled()
        set_autostart(enabled)
        cfg = read_config()
        cfg["AUTOSTART"] = "true" if enabled else "false"
        write_config(cfg)

    def _toggle_lan(self, icon, item):
        cfg = read_config()
        cfg["LAN_ACCESS"] = "false" if cfg.get("LAN_ACCESS") == "true" else "true"
        write_config(cfg)
        if cfg["LAN_ACCESS"] == "true":
            text = (f"After restarting BioAttend, other computers can open it at http://{socket.gethostname()}:"
                    f"{self.port} (or this PC's IP address). Allow BioAttend when Windows Firewall asks.\n\n"
                    "Quit BioAttend from the tray icon and start it again to apply.")
        else:
            text = "Network access will be turned off after BioAttend is restarted (Quit and start it again)."
        threading.Thread(target=message_box, args=(text,), daemon=True).start()

    def backup_now(self):
        """Back up with the saved destinations and passphrase - no sign-in needed."""
        def work():
            text, error = run_backup_text("tray")
            notified = False
            if self.icon is not None and not error:
                try:
                    self.icon.notify(text, APP_NAME)
                    notified = True
                except Exception:
                    pass
            if not notified:
                message_box(text, error=error)
        threading.Thread(target=work, daemon=True, name="tray-backup").start()

    def _reset_admin_password(self, icon=None, item=None):
        def work():
            from django.contrib.auth import get_user_model
            from django.core.cache import cache
            admin = get_user_model().objects.filter(is_superuser=True, is_active=True).order_by("pk").first()
            if admin is None:
                message_box("No administrator yet: complete the first-time setup page in the BioAttend window.")
                return
            password = secrets.token_urlsafe(9)
            admin.set_password(password)
            admin.save(update_fields=["password"])
            cache.clear()  # also lifts a login lockout
            message_box(f"New password for the administrator account\n\nUser name:  {admin.get_username()}\n"
                        f"Password:   {password}\n\nWrite it down and change it after signing in.")
        threading.Thread(target=work, daemon=True).start()

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        self.stop.set()
        if self.server is not None:
            try:
                self.server.close()
            except Exception:
                pass
        if self.icon is not None:
            self.icon.stop()
        if self.window is not None:
            self.window.destroy()

    def run(self):
        import webview
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        self.window = webview.create_window(APP_NAME, self.url, width=1360, height=860, min_size=(960, 640),
                                            hidden=self.background, text_select=True)
        self.window.events.closing += self._on_closing
        self.build_tray()
        webview.start(private_mode=False, storage_path=str(HOME / "webview"))
        self.quit()


def main():
    args = sys.argv[1:]
    redirect_std_streams()
    exe_name = Path(sys.executable).stem.lower()
    cli = exe_name.endswith("-cli")
    if cli or (args and args[0] == "manage"):
        cfg = ensure_config()
        configure_environment(cfg)
        from django.core.management import execute_from_command_line
        execute_from_command_line(["manage.py", *(args if cli else args[1:])])
        return

    from core.osutil import acquire_exclusive_lock
    if args and args[0] == "--quit":
        sys.exit(0 if send_to_running("quit") else 1)
    if args and args[0] == "--backup":  # Start-menu shortcut "Back up BioAttend now"
        if send_to_running("backup"):
            return
        configure_environment(ensure_config())
        import django
        django.setup()
        from django.core.management import call_command
        call_command("migrate", interactive=False, verbosity=0)
        text, error = run_backup_text("shortcut")
        message_box(text, error=error)
        return
    lock = acquire_exclusive_lock(HOME / "run" / "app.lock")
    if lock is None:  # already running: bring it to the front
        if not send_to_running("show"):
            message_box("BioAttend is already running (see the tray icon).")
        return

    cfg = ensure_config()
    lan = configure_environment(cfg)
    apply_autostart(cfg)
    app = DesktopApp(background="--background" in args)
    try:
        app.start_backend(cfg, lan)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        message_box(f"BioAttend could not start:\n\n{exc}\n\nDetails: {HOME / 'data' / 'log'}", error=True)
        sys.exit(1)
    control = ControlServer(app)
    control.start()
    control.publish(app.port)
    app.run()
    time.sleep(0.5)
    os._exit(0)  # make sure no background thread keeps the process alive


if __name__ == "__main__":
    main()
