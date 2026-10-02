"""Operating-system helpers that behave the same on Linux and Windows (incl. the frozen desktop app)."""
import os
import subprocess
import sys

IS_WINDOWS = os.name == "nt"
FROZEN = getattr(sys, "frozen", False)  # running inside the packaged Windows app


def acquire_exclusive_lock(path):
    """Open `path` and lock it without waiting. Returns the open file (keep it!) or None if already locked."""
    handle = open(path, "a+")
    try:
        if IS_WINDOWS:
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def pid_alive(pid):
    """True if a process with this id is running. (Never use os.kill(pid, 0) on Windows: it terminates it.)"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def manage_command(*args):
    """Command line that runs a Django management command in a new process."""
    from django.conf import settings
    if FROZEN:  # the packaged app understands:  BioAttend.exe manage <command> ...
        return [sys.executable, "manage", *args]
    return [sys.executable, str(settings.BASE_DIR / "manage.py"), *args]


def background_process_kwargs():
    """Popen options for a detached background process without a console window."""
    if IS_WINDOWS:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
        return {"creationflags": flags}
    return {"start_new_session": True}
