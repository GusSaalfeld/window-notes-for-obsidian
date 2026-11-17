"""Windows part of Window Notes: finding windows, the note box, hotkey and window events."""
import ctypes
import os
import threading
from ctypes import wintypes
from pathlib import Path

NAME = "Windows"
AUTOSTART_LABEL = "Start with Windows"
FONT = "Segoe UI"
FONT_BOLD = "Segoe UI Semibold"   # a real semibold face reads better than synthetic bold
SUPPORTS_URL = True
NOTICE = ""          # a message to log at start-up, if any

try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi
H = wintypes.HWND

user32.GetForegroundWindow.restype = H
user32.GetWindowTextLengthW.argtypes = [H]
user32.GetWindowTextW.argtypes = [H, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [H, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [H, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowRect.argtypes = [H, ctypes.POINTER(wintypes.RECT)]
user32.IsIconic.argtypes = [H]
user32.IsWindow.argtypes = [H]
user32.ShowWindow.argtypes = [H, ctypes.c_int]
user32.ShowWindowAsync.argtypes = [H, ctypes.c_int]
user32.SetWindowPos.argtypes = [H, H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.GetWindowLongPtrW.argtypes = [H, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [H, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetDpiForWindow.argtypes = [H]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
dwmapi.DwmGetWindowAttribute.argtypes = [H, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]

WinEventProc = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, H, wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WinEventProc, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, H, wintypes.LPARAM)
user32.EnumChildWindows.argtypes = [H, EnumProc, wintypes.LPARAM]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


user32.MonitorFromWindow.argtypes = [H, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HMONITOR
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST = H(-1)
SWP_NOSIZE, SWP_NOACTIVATE, SWP_NOOWNERZORDER, SWP_ASYNCWINDOWPOS = 0x1, 0x10, 0x200, 0x4000
EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_MINIMIZESTART, EVENT_OBJECT_LOCATIONCHANGE = 0x3, 0x16, 0x800B
WINEVENT_OUTOFCONTEXT, WINEVENT_SKIPOWNPROCESS = 0x0, 0x2
WM_HOTKEY, WM_WATCH, WM_STOP = 0x0312, 0x8001, 0x8002
IGNORED_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
STARTUP_FILE = Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup\Window Notes.bat"


# ---- app start ------------------------------------------------------------
def single_instance():
    """True if this is the only copy running."""
    single_instance.mutex = kernel32.CreateMutexW(None, False, "WindowNotesForObsidian")
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def message(text):
    user32.MessageBoxW(None, text, "Window Notes", 0x40 | 0x40000)  # info icon, topmost


def open_target(target):
    """Open a file, folder, web address or obsidian:// link with its default app."""
    os.startfile(str(target))


def data_dir():
    """Settings folder for the downloaded .exe: %APPDATA%\\Window Notes"""
    return Path(os.environ.get("APPDATA") or Path.home()) / "Window Notes"


def autostart_enabled():
    return STARTUP_FILE.exists()


def set_autostart(on, command):
    """`command`: the program and its arguments, e.g. [pythonw.exe, window_notes.py] or [WindowNotes.exe]."""
    if on:
        args = " ".join(f'"{part}"' for part in command)
        STARTUP_FILE.write_text(f'@echo off\r\nstart "" {args}\r\n', encoding="utf-8")
    elif STARTUP_FILE.exists():
        STARTUP_FILE.unlink()


# ---- windows --------------------------------------------------------------
def _title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _pid(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _process_name(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        return os.path.basename(buf.value) if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(h)


def _store_app_name(frame, frame_pid):
    """Windows Store apps run inside ApplicationFrameHost; find the real app's process."""
    found = []

    def cb(child, _):
        pid = _pid(child)
        if pid != frame_pid:
            found.append(pid)
            return False
        return True

    user32.EnumChildWindows(frame, EnumProc(cb), 0)
    return _process_name(found[0]) if found else ""


def foreground(known=None):
    """The active window as {id, app, title, pid}, or None for the desktop or taskbar.
    `known` is the last result: if it's the same window, only the title is re-read."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    if known and known["id"] == hwnd:
        return dict(known, title=_title(hwnd))
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    if cls.value in IGNORED_CLASSES:
        return None
    pid = _pid(hwnd)
    app = _process_name(pid)
    if app.lower() == "applicationframehost.exe":
        app = _store_app_name(hwnd, pid) or app
    return {"id": hwnd, "app": app, "title": _title(hwnd), "pid": pid}


def exists(wid):
    return bool(user32.IsWindow(wid))


def minimized(wid):
    return bool(user32.IsIconic(wid))


def rect(wid):
    """Visible window bounds (left, top, right, bottom) without the invisible resize border."""
    r = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(wid, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:  # DWMWA_EXTENDED_FRAME_BOUNDS
        user32.GetWindowRect(wid, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def work_area(wid=None):
    """Screen area without the taskbar, on the monitor the window is on (or the active one)."""
    wid = wid or user32.GetForegroundWindow()
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    user32.GetMonitorInfoW(user32.MonitorFromWindow(wid, 2), ctypes.byref(mi))  # nearest monitor
    r = mi.rcWork
    return r.left, r.top, r.right, r.bottom


def scale(wid=None):
    try:
        return max(user32.GetDpiForWindow(wid or user32.GetForegroundWindow()), 96) / 96
    except Exception:
        return 1.0


TITLE_BAR = 44   # px at 100%: the box sits just below the window's title bar


# ---- the note box ---------------------------------------------------------
class Overlay:
    """Shows, hides and moves the Tk note box without ever taking focus."""

    def __init__(self, root):
        root.update_idletasks()
        self.hwnd = self.id = int(root.wm_frame(), 16)    # id: what rect() etc. accept
        ex = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)

    def show(self):
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)

    def hide(self, from_thread=False):
        (user32.ShowWindowAsync if from_thread else user32.ShowWindow)(self.hwnd, SW_HIDE)

    def move(self, x, y, from_thread=False):
        flags = SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER | (SWP_ASYNCWINDOWPOS if from_thread else 0)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, 0, 0, flags)

    def is_own(self, win):
        return win["pid"] == os.getpid()

    def after_click(self, win):
        pass   # the box never takes focus here, so there's nothing to give back


# ---- hotkey and window events (own thread, own message loop) ---------------
class Events(threading.Thread):
    """Calls back on: the active window changing, the watched window moving or being
    minimized, and the hotkey. Callbacks run on this thread: keep them quick."""

    def __init__(self, hotkey, on_foreground, on_moved, on_hotkey, on_hotkey_failed):
        super().__init__(daemon=True)
        self.hotkey = hotkey
        self.on_foreground, self.on_moved = on_foreground, on_moved
        self.on_hotkey, self.on_hotkey_failed = on_hotkey, on_hotkey_failed
        self.tid, self.ready = None, threading.Event()

    def run(self):
        self.tid = kernel32.GetCurrentThreadId()
        self.proc = WinEventProc(self._event)
        hooks = [user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, self.proc,
                                        0, 0, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)]
        watched = []
        if not user32.RegisterHotKey(None, 1, 0x2 | 0x1 | 0x4000, ord(self.hotkey)):  # Ctrl+Alt, no repeat
            self.on_hotkey_failed()
        self.ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.on_hotkey()
            elif msg.message == WM_WATCH:   # follow a different process's windows
                for h in watched:
                    user32.UnhookWinEvent(h)
                watched = [user32.SetWinEventHook(e, e, None, self.proc, msg.wParam, 0, WINEVENT_OUTOFCONTEXT)
                           for e in (EVENT_SYSTEM_MINIMIZESTART, EVENT_OBJECT_LOCATIONCHANGE)]
            elif msg.message == WM_STOP:
                break
        for h in hooks + watched:
            user32.UnhookWinEvent(h)

    def watch(self, win):
        """Report moves of this window (hooks are per process, so this is cheap)."""
        if win["pid"] != getattr(self, "_pid", None):
            self._pid = win["pid"]
            user32.PostThreadMessageW(self.tid, WM_WATCH, win["pid"], 0)

    def stop(self):
        user32.PostThreadMessageW(self.tid, WM_STOP, 0, 0)

    def _event(self, hook, event, hwnd, id_object, id_child, thread, ms):
        if event == EVENT_SYSTEM_FOREGROUND:
            self.on_foreground()
        elif not id_object and not id_child:
            self.on_moved(hwnd)


# ---- browser address bar (UI Automation) ----------------------------------
class UrlReader:
    BROWSERS = {"chrome", "msedge", "firefox", "brave", "vivaldi", "opera", "librewolf", "waterfox", "chromium"}

    def __init__(self, log):
        self.uia, self.cache = None, {}
        try:
            import comtypes.client
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as U
            self.U = U
            self.uia = comtypes.client.CreateObject(U.CUIAutomation, interface=U.IUIAutomation)
            edit = self.uia.CreatePropertyCondition(U.UIA_ControlTypePropertyId, U.UIA_EditControlTypeId)
            self.conds = [
                self.uia.CreatePropertyCondition(U.UIA_AutomationIdPropertyId, "urlbar-input"),  # Firefox
                self.uia.CreateAndCondition(edit, self.uia.CreatePropertyCondition(U.UIA_NamePropertyId, "Address and search bar")),  # Chromium
                edit,  # fallback: first text box in the browser's toolbar
            ]
        except Exception as e:
            log("Browser URL matching is off (run: pip install comtypes).", e)

    def url(self, win):
        app = win["app"].lower().removesuffix(".exe")
        if not self.uia or app not in self.BROWSERS:
            return ""
        key = (win["id"], win["title"])   # re-read only when the tab title changes
        if key not in self.cache:
            if len(self.cache) > 300:
                self.cache.clear()
            self.cache[key] = self._read(win["id"])
        return self.cache[key]

    def _read(self, hwnd):
        U = self.U
        try:
            root = self.uia.ElementFromHandle(hwnd)
            for cond in self.conds:
                el = root.FindFirst(U.TreeScope_Descendants, cond)
                if el:
                    pat = el.GetCurrentPattern(U.UIA_ValuePatternId)
                    if pat:
                        return pat.QueryInterface(U.IUIAutomationValuePattern).CurrentValue or ""
        except Exception:
            pass
        return ""
