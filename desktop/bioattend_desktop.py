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
APP_VERSION = "1.0.3"
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
    if IS_WINDOWS:
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
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


def copy_to_clipboard(text):
    """Put text on the Windows clipboard, excluded from clipboard history and cloud sync (it may be a password)."""
    if not IS_WINDOWS:
        return False
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = (wintypes.HWND,)
    user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.RegisterClipboardFormatW.argtypes = (wintypes.LPCWSTR,)
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    data = (text + "\0").encode("utf-16-le")
    for _ in range(10):  # another program may hold the clipboard for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        ctypes.memmove(kernel32.GlobalLock(handle), data, len(data))
        kernel32.GlobalUnlock(handle)
        user32.SetClipboardData(13, handle)  # CF_UNICODETEXT (the clipboard now owns the memory)
        for fmt in ("ExcludeClipboardContentFromMonitorProcessing", "CanIncludeInClipboardHistory",
                    "CanUploadToCloudClipboard"):
            marker = kernel32.GlobalAlloc(0x0002, 4)
            ctypes.memmove(kernel32.GlobalLock(marker), b"\0\0\0\0", 4)  # DWORD 0 = "no"
            kernel32.GlobalUnlock(marker)
            user32.SetClipboardData(user32.RegisterClipboardFormatW(fmt), marker)
        return True
    finally:
        user32.CloseClipboard()


def _task_dialog_function():
    """TaskDialogIndirect from Common Controls 6 (loaded from WinSxS if this process has the old version)."""
    import ctypes
    import glob
    try:
        return ctypes.WinDLL("comctl32").TaskDialogIndirect
    except AttributeError:
        pass
    pattern = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "WinSxS",
                           "amd64_microsoft.windows.common-controls_6595b64144ccf1df_6.*", "comctl32.dll")
    for dll in sorted(glob.glob(pattern), reverse=True):
        try:
            return ctypes.WinDLL(dll).TaskDialogIndirect
        except (OSError, AttributeError):
            continue
    return None


def show_password_dialog(username, password):
    """New-password dialog with a "Copy password" button (falls back to auto-copy + message box)."""
    if not IS_WINDOWS:
        print(f"[{APP_NAME}] user {username}: new password {password}")
        return
    import ctypes
    from ctypes import wintypes
    task_dialog = _task_dialog_function()
    content = (f"User name:   {username}\nPassword:    {password}\n\n"
               "Sign in with this password, then change it (your name at the top right → Change password).")
    if task_dialog is None:
        copied = copy_to_clipboard(password)
        message_box("New password for the administrator account\n\n" + content +
                    ("\n\nThe password has been copied to the clipboard." if copied else ""))
        return

    class TASKDIALOG_BUTTON(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("nButtonID", ctypes.c_int), ("pszButtonText", wintypes.LPCWSTR)]

    CALLBACK = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                  ctypes.c_ssize_t)

    class TASKDIALOGCONFIG(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("cbSize", wintypes.UINT), ("hwndParent", wintypes.HWND), ("hInstance", wintypes.HINSTANCE),
                    ("dwFlags", ctypes.c_int), ("dwCommonButtons", ctypes.c_int),
                    ("pszWindowTitle", wintypes.LPCWSTR), ("pszMainIcon", ctypes.c_void_p),
                    ("pszMainInstruction", wintypes.LPCWSTR), ("pszContent", wintypes.LPCWSTR),
                    ("cButtons", wintypes.UINT), ("pButtons", ctypes.POINTER(TASKDIALOG_BUTTON)),
                    ("nDefaultButton", ctypes.c_int), ("cRadioButtons", wintypes.UINT),
                    ("pRadioButtons", ctypes.c_void_p), ("nDefaultRadioButton", ctypes.c_int),
                    ("pszVerificationText", wintypes.LPCWSTR), ("pszExpandedInformation", wintypes.LPCWSTR),
                    ("pszExpandedControlText", wintypes.LPCWSTR), ("pszCollapsedControlText", wintypes.LPCWSTR),
                    ("pszFooterIcon", ctypes.c_void_p), ("pszFooter", wintypes.LPCWSTR),
                    ("pfCallback", CALLBACK), ("lpCallbackData", ctypes.c_ssize_t), ("cxWidth", wintypes.UINT)]

    COPY_ID, TDN_CREATED, TDN_BUTTON_CLICKED, TDM_SET_ELEMENT_TEXT, TDE_CONTENT = 1001, 0, 2, 0x0400 + 108, 0
    copied_text = ctypes.create_unicode_buffer(content + "\n\n✓ Password copied - paste it with Ctrl+V.")
    user32 = ctypes.WinDLL("user32")
    user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)

    def on_event(hwnd, msg, wparam, lparam, ref):
        if msg == TDN_CREATED:
            user32.SetForegroundWindow(hwnd)
        elif msg == TDN_BUTTON_CLICKED and wparam == COPY_ID:
            if copy_to_clipboard(password):
                user32.SendMessageW(hwnd, TDM_SET_ELEMENT_TEXT, TDE_CONTENT, ctypes.addressof(copied_text))
            return 1  # S_FALSE: keep the dialog open
        return 0

    callback = CALLBACK(on_event)
    buttons = (TASKDIALOG_BUTTON * 1)(TASKDIALOG_BUTTON(COPY_ID, "Copy password"))
    config = TASKDIALOGCONFIG()
    config.cbSize = ctypes.sizeof(TASKDIALOGCONFIG)
    config.dwFlags = 0x0008  # TDF_ALLOW_DIALOG_CANCELLATION (Esc closes)
    config.dwCommonButtons = 0x0020  # Close
    config.pszWindowTitle = APP_NAME
    config.pszMainIcon = 0xFFFD  # TD_INFORMATION_ICON
    config.pszMainInstruction = "New password for the administrator account"
    config.pszContent = content
    config.cButtons = 1
    config.pButtons = buttons
    config.nDefaultButton = COPY_ID
    config.pfCallback = callback
    task_dialog.argtypes = (ctypes.POINTER(TASKDIALOGCONFIG), ctypes.POINTER(ctypes.c_int), ctypes.c_void_p,
                            ctypes.c_void_p)
    task_dialog.restype = ctypes.c_long
    pressed = ctypes.c_int(0)
    if task_dialog(ctypes.byref(config), ctypes.byref(pressed), None, None) != 0:  # failed: fall back
        copied = copy_to_clipboard(password)
        message_box("New password for the administrator account\n\n" + content +
                    ("\n\nThe password has been copied to the clipboard." if copied else ""))


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


WINDOW_LOCK = HOME / "run" / "window.lock"
BELOW_NORMAL_PRIORITY_CLASS, NORMAL_PRIORITY_CLASS = 0x4000, 0x20
STARTUP_DELAY_SECONDS = 60  # after Windows sign-in, wait before talking to the devices


def set_process_priority(priority_class):
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE  # 64-bit handle (a plain int would be cut to 32 bit)
        kernel32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.SetPriorityClass.restype = wintypes.BOOL
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), priority_class)


def find_app_window():
    """Handle of the visible BioAttend window (not message boxes), or None."""
    if not IS_WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if title.value == APP_NAME and cls.value != "#32770":
            found.append(hwnd)
            return False
        return True
    user32.EnumWindows(visit, 0)
    return found[0] if found else None


def window_process_running():
    from core.osutil import acquire_exclusive_lock
    handle = acquire_exclusive_lock(WINDOW_LOCK)
    if handle is None:
        return True
    handle.close()
    return False


def run_window(url):
    """The window process: shows the portal; closing it ends this process and frees its memory."""
    from core.osutil import acquire_exclusive_lock
    lock = acquire_exclusive_lock(WINDOW_LOCK)
    if lock is None:  # a window is already open
        bring_to_front(find_app_window())
        return
    set_process_priority(NORMAL_PRIORITY_CLASS)  # the background process runs below normal
    import webview
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.create_window(APP_NAME, url, width=1360, height=860, min_size=(960, 640), text_select=True)
    webview.start(private_mode=False, storage_path=str(HOME / "webview"))
    lock.close()


def bring_to_front(hwnd):
    if hwnd and IS_WINDOWS:
        import ctypes
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)


class DesktopApp:
    """Background process: tray icon, web server and device worker (no window, below-normal priority)."""

    def __init__(self, background):
        self.background = background
        self.icon = None
        self.server = None
        self.stop = threading.Event()
        self.quitting = False
        self.told_tray = False

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
        self.server = create_server(get_wsgi_application(), host=host, port=self.port, threads=4,
                                    ident=APP_NAME, channel_timeout=300)
        threading.Thread(target=self.server.run, daemon=True, name="web").start()
        threading.Thread(target=self._run_worker, daemon=True, name="worker").start()
        self.url = f"http://127.0.0.1:{self.port}/"

    def _run_worker(self):
        if self.background and self.stop.wait(STARTUP_DELAY_SECONDS):
            return  # quit during the start-up delay
        from core.management.commands.run_worker import Command
        while not self.stop.is_set():
            try:
                Command().serve(self.stop)
            except Exception:
                import logging
                logging.getLogger("core").exception("Worker stopped unexpectedly; restarting in 10 s")
                self.stop.wait(10)

    # -- window (separate process, only while it is open)
    def show(self):
        if window_process_running():
            bring_to_front(find_app_window())
            return
        import subprocess
        args = [sys.executable, "--window", self.url] if getattr(sys, "frozen", False) else \
            [sys.executable, os.path.abspath(__file__), "--window", self.url]
        subprocess.Popen(args, close_fds=True)
        threading.Thread(target=self._watch_window, daemon=True, name="window-watch").start()

    def _watch_window(self):
        for _ in range(30):  # wait until the window process has started
            if window_process_running():
                break
            time.sleep(1)
        while window_process_running() and not self.quitting:
            time.sleep(2)
        if not self.quitting and not self.told_tray and self.icon is not None:
            self.told_tray = True
            try:
                self.icon.notify("BioAttend keeps running in the background and collects punches. "
                                 "Click the BioAttend icon to open it again.", APP_NAME)
            except Exception:
                pass

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
            show_password_dialog(admin.get_username(), password)
        threading.Thread(target=work, daemon=True).start()

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        self.stop.set()
        hwnd = find_app_window()
        if hwnd:
            import ctypes
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE: close the window process too
        if self.server is not None:
            try:
                self.server.close()
            except Exception:
                pass
        if self.icon is not None:
            self.icon.stop()

    def run(self):
        """Tray icon on the main thread; returns when the user quits."""
        self.build_tray()

        def setup(icon):
            icon.visible = True
            threading.Thread(target=self._promote_tray_icon_once, daemon=True, name="tray-promote").start()
            if not self.background:
                self.show()
        self.icon.run(setup=setup)
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

    if args and args[0] == "--window":  # the window process (started by the background process)
        run_window(args[1] if len(args) > 1 else f"http://127.0.0.1:{DEFAULT_PORT}/")
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
    set_process_priority(BELOW_NORMAL_PRIORITY_CLASS)  # never compete with the user's programs
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
