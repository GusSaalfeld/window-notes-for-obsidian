"""Window Notes  -  https://github.com/GusSaalfeld/window-notes  (MIT License)

Shows Markdown notes from an Obsidian vault folder on top of the window they are
attached to. Attach a note with properties at the top of the file:

    ---
    attach-app: "excel.exe"        # program name (".exe" optional)
    attach-title: "*Budget*"       # window title
    attach-url: "github.com/*"     # browser tab address (Chrome, Edge, Firefox, Brave...)
    ---

Use any combination (at least one). Case-insensitive; * and ? are wildcards.

Ctrl+Alt+N   attach a new note to the window you're in (opens it in Obsidian)
Click title  collapse / expand a note
Click a box  tick a "- [ ]" checkbox (saved to the file)
Right-click  open notes folder, start with Windows, reload, quit

Windows only. Needs Python 3; URL matching also needs:  pip install comtypes
"""
import ctypes
import fnmatch
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import urllib.parse
from ctypes import wintypes
from pathlib import Path

# ---- Settings -------------------------------------------------------------
HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.toml"


def load_config():
    """Settings from config.toml next to this file (environment variables win).
    On first run, asks for the vault folder and saves it."""
    cfg = {}
    if CONFIG.exists():
        import tomllib
        with open(CONFIG, "rb") as f:
            cfg = tomllib.load(f)
    vault = os.environ.get("WN_VAULT") or cfg.get("vault", "")
    if not vault:
        from tkinter import filedialog
        r = tk.Tk(); r.withdraw()
        vault = filedialog.askdirectory(title="Window Notes: choose your Obsidian vault (or any notes folder)")
        r.destroy()
        if not vault:
            sys.exit("No notes folder chosen.")
        import json  # a JSON string is also a valid TOML string (handles quotes and backslashes)
        CONFIG.write_text(f"# Window Notes settings\nvault = {json.dumps(vault)}\nfolder = \"Window notes\"\nhotkey = \"N\"\n",
                          encoding="utf-8")
    return (Path(vault), os.environ.get("WN_FOLDER") or cfg.get("folder", "Window notes"),
            (cfg.get("hotkey") or "N").upper()[:1])


VAULT = FOLDER = HOTKEY_KEY = None                     # filled in by load_config() at start-up
# FOLDER: inside the vault; subfolders are read too.  HOTKEY_KEY: used with Ctrl+Alt
POLL_MS = 250                                           # backup check of the active window
QUEUE_MS = 30                                           # how often the UI picks up events
RESCAN_MS = 2000                                        # how often to re-read the notes folder
BOX_WIDTH = 300                                         # note box width, at 100% scaling
MAX_BODY_LINES = 20
BROWSERS = {"chrome", "msedge", "firefox", "brave", "vivaldi", "opera", "librewolf", "waterfox", "chromium"}

STARTUP_FILE = Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup\Window Notes.bat"

# pythonw has no console: send messages and errors to a log file instead
if sys.stdout is None or sys.stderr is None:
    _log = open(HERE / "window-notes.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---- Win32 ----------------------------------------------------------------
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
user32.IsWindowVisible.argtypes = [H]
user32.ShowWindow.argtypes = [H, ctypes.c_int]
user32.SetWindowPos.argtypes = [H, H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.GetWindowLongPtrW.argtypes = [H, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [H, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetDpiForWindow.argtypes = [H]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
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
user32.ShowWindowAsync.argtypes = [H, ctypes.c_int]
EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_MINIMIZESTART, EVENT_OBJECT_LOCATIONCHANGE = 0x3, 0x16, 0x800B
WINEVENT_OUTOFCONTEXT, WINEVENT_SKIPOWNPROCESS = 0x0, 0x2
IGNORED_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
MY_PID = os.getpid()


def process_name(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        return os.path.basename(buf.value) if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(h)


def window_pid(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def store_app_name(frame, frame_pid):
    """Windows Store apps run inside ApplicationFrameHost; find the real app's process."""
    found = []

    def cb(child, _):
        pid = window_pid(child)
        if pid != frame_pid:
            found.append(pid)
            return False
        return True

    user32.EnumChildWindows(frame, EnumProc(cb), 0)
    return process_name(found[0]) if found else ""


def window_info(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    title = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, title, n + 1)
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    pid = window_pid(hwnd)
    app = process_name(pid)
    if app.lower() == "applicationframehost.exe":
        app = store_app_name(hwnd, pid) or app
    return {"hwnd": hwnd, "app": app, "title": title.value, "pid": pid, "class": cls.value, "url": ""}


def window_rect(hwnd):
    r = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:  # DWMWA_EXTENDED_FRAME_BOUNDS
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def monitor_work_area(hwnd):
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi))  # nearest monitor
    r = mi.rcWork
    return r.left, r.top, r.right, r.bottom


def dpi_scale(hwnd):
    try:
        return max(user32.GetDpiForWindow(hwnd), 96) / 96
    except Exception:
        return 1.0


# ---- Browser address bar (UI Automation) ----------------------------------
class UrlReader:
    def __init__(self):
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
        if not self.uia or app_key(win["app"]) not in BROWSERS:
            return ""
        key = (win["hwnd"], win["title"])   # re-read only when the tab title changes
        if key not in self.cache:
            if len(self.cache) > 300:
                self.cache.clear()
            self.cache[key] = self._read(win["hwnd"])
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


def norm_url(u):
    u = u.strip().lower()
    u = re.sub(r"^[a-z][a-z0-9+.-]*://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


# ---- Notes ----------------------------------------------------------------
def unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
        if "\\" in v:
            v = v.replace('\\"', '"').replace("\\\\", "\\")
    return v


def parse_note(path):
    """{path, name, app, title, url, lines=[(file line number, text)]} for attached notes, else None."""
    try:
        text = path.read_text(encoding="utf-8-sig")   # -sig: ignore a byte-order mark some editors add
    except (OSError, UnicodeDecodeError):
        return None
    rows = text.split("\n")
    if not rows or rows[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(rows)) if rows[i].strip() == "---"), None)
    if end is None:
        return None
    props = {}
    for line in rows[1:end]:
        m = re.match(r"^(attach-app|attach-title|attach-url)\s*:\s*(.*?)\s*$", line)
        if m:
            props[m.group(1)] = unquote(m.group(2))
    note = {"path": path, "name": path.stem, "app": props.get("attach-app", ""),
            "title": props.get("attach-title", ""), "url": props.get("attach-url", "")}
    if not (note["app"] or note["title"] or note["url"]):
        return None
    lines = [(i, rows[i].rstrip("\r")) for i in range(end + 1, len(rows))]
    while lines and not lines[0][1].strip():
        lines.pop(0)
    while lines and not lines[-1][1].strip():
        lines.pop()
    note["lines"] = lines
    return note


def app_key(name):
    name = name.lower()
    return name[:-4] if name.endswith(".exe") else name


def matches(note, win):
    if note["app"] and not fnmatch.fnmatchcase(app_key(win["app"]), app_key(note["app"])):
        return False
    if note["title"] and not fnmatch.fnmatchcase(win["title"].lower(), note["title"].lower()):
        return False
    if note["url"]:
        u, rule = norm_url(win["url"]), norm_url(note["url"])
        if not u or not (fnmatch.fnmatchcase(u, rule) or fnmatch.fnmatchcase(u + "/", rule)):
            return False
    return True


INLINE = [
    (re.compile(r"\[\[([^\]|]+)\|([^\]]+)\]\]"), r"\2"),   # [[note|alias]]
    (re.compile(r"\[\[([^\]]+)\]\]"), r"\1"),              # [[note]]
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),         # [text](url)
    (re.compile(r"(\*\*|__|`|==|~~)"), ""),                # bold, code, highlight, strike markers
]


def plain(s):
    for rx, rep in INLINE:
        s = rx.sub(rep, s)
    return s


def yaml_str(v):
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def obsidian_open(path):
    rel = path.relative_to(VAULT).with_suffix("").as_posix()
    os.startfile("obsidian://open?vault=" + urllib.parse.quote(VAULT.name) + "&file=" + urllib.parse.quote(rel))


def toggle_checkbox(path, lineno):
    text = path.read_text(encoding="utf-8-sig")
    rows = text.split("\n")
    if lineno >= len(rows):
        return
    m = re.match(r"^(\s*[-*+] \[)([ xX])(\].*)$", rows[lineno], re.S)
    if not m:
        return
    rows[lineno] = m.group(1) + (" " if m.group(2) in "xX" else "x") + m.group(3)
    path.write_text("\n".join(rows), encoding="utf-8", newline="")


# ---- Hotkey and window events (own thread, own message loop) ---------------
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
WM_HOTKEY, WM_WATCH, WM_STOP = 0x0312, 0x8001, 0x8002


class Events(threading.Thread):
    """Gets the hotkey and Windows' window events. Moves the note box the moment the
    target window moves, without waiting for the Tk loop (Win32 calls only here)."""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app, self.tid, self.ready = app, None, threading.Event()

    def run(self):
        self.tid = kernel32.GetCurrentThreadId()
        self.proc = WinEventProc(self.on_event)
        hooks = [user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, self.proc,
                                        0, 0, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)]
        watched = []
        if not user32.RegisterHotKey(None, 1, 0x2 | 0x1 | 0x4000, ord(HOTKEY_KEY)):  # Ctrl+Alt, no repeat
            self.app.q.put("hotkey-failed")
        self.ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.app.q.put("new-note")
            elif msg.message == WM_WATCH:   # follow a different process's windows
                for h in watched:
                    user32.UnhookWinEvent(h)
                pid = msg.wParam
                watched = [user32.SetWinEventHook(e, e, None, self.proc, pid, 0, WINEVENT_OUTOFCONTEXT)
                           for e in (EVENT_SYSTEM_MINIMIZESTART, EVENT_OBJECT_LOCATIONCHANGE)]
            elif msg.message == WM_STOP:
                break
        for h in hooks + watched:
            user32.UnhookWinEvent(h)

    def watch(self, pid):
        user32.PostThreadMessageW(self.tid, WM_WATCH, pid, 0)

    def stop(self):
        user32.PostThreadMessageW(self.tid, WM_STOP, 0, 0)

    def on_event(self, hook, event, hwnd, id_object, id_child, thread, ms):
        try:
            app = self.app
            if event == EVENT_SYSTEM_FOREGROUND:
                app.q.put("foreground")
                return
            target = app.target
            if id_object or id_child or not target or hwnd != target["hwnd"]:
                return
            if event == EVENT_SYSTEM_MINIMIZESTART or user32.IsIconic(hwnd):
                user32.ShowWindowAsync(app.hwnd, SW_HIDE)
                app.visible, app.shown_key = False, None
            elif app.visible:
                app.move_to(target, asynchronous=True)
        except Exception as e:
            log("Event error:", e)


# ---- App ------------------------------------------------------------------
class App:
    BG, FG, MUTED, DONE, BORDER = "#fff4b8", "#2b2610", "#6b5f2a", "#9a8f5c", "#d9c56a"

    def __init__(self):
        self.dir = VAULT / FOLDER
        self.dir.mkdir(parents=True, exist_ok=True)
        self.notes, self.stamp, self.scan_age = [], None, RESCAN_MS
        self.target = None          # the window the box belongs to
        self.shown_key = None       # what the box currently displays
        self.visible = False
        self.size = (BOX_WIDTH, 50)
        self.dismissed = set()      # (hwnd, note path) hidden with × this session
        self.collapsed = set()      # note paths collapsed to their title
        self.q = queue.Queue()
        self.urls = UrlReader()

        self.root = tk.Tk()
        self.root.title("Window Notes")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=self.BORDER)
        self.root.geometry("+-3000+-3000")
        self.box = tk.Frame(self.root, bg=self.BG)
        self.box.pack(padx=1, pady=1, fill="both")
        self.autostart = tk.BooleanVar(value=STARTUP_FILE.exists())
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Open notes folder", command=lambda: os.startfile(self.dir))
        self.menu.add_command(label="Reload notes", command=lambda: self.rescan(force=True))
        self.menu.add_checkbutton(label="Start with Windows", variable=self.autostart, command=self.set_autostart)
        self.menu.add_separator()
        self.menu.add_command(label="Quit Window Notes", command=self.quit)
        self.root.update_idletasks()
        self.hwnd = int(self.root.wm_frame(), 16)
        ex = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        self.hide()

        # Windows tells the event thread when the active window changes or the target window moves
        self.loc_pid = None
        self.events = Events(self)
        self.events.start()
        self.events.ready.wait(2)
        self.rescan(force=True)
        self.since_full = 0
        self.root.after(QUEUE_MS, self.tick)
        log(f"Window Notes running. Notes folder: {self.dir}")
        self.welcome()

    def welcome(self, seconds=4):
        """Brief message in the bottom-right corner so you know it started."""
        self.toast_until = time.time() + seconds
        for child in self.box.winfo_children():
            child.destroy()
        fg = user32.GetForegroundWindow()
        s = dpi_scale(fg) if fg else 1.0
        card = tk.Frame(self.box, bg=self.BG, padx=12, pady=9)
        card.pack(fill="x")
        tk.Label(card, text="Window Notes is running", bg=self.BG, fg=self.FG,
                 font=("Segoe UI Semibold", 10), anchor="w").pack(fill="x")
        n = len(self.notes)
        tk.Label(card, text=f"{n} attached note{'' if n == 1 else 's'}. Press Ctrl+Alt+{HOTKEY_KEY} in any window to attach one.",
                 bg=self.BG, fg=self.MUTED, font=("Segoe UI", 9), anchor="w", justify="left",
                 wraplength=int(BOX_WIDTH * s) - 24).pack(fill="x", pady=(2, 0))
        tk.Frame(self.box, bg=self.BG, width=int(BOX_WIDTH * s), height=0).pack()
        self.root.update_idletasks()
        w, h = int(BOX_WIDTH * s) + 2, self.root.winfo_reqheight()
        self.root.geometry(f"{w}x{h}")
        ml, mt, mr, mb = monitor_work_area(fg) if fg else (0, 0, 1280, 720)
        m = int(16 * s)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, mr - w - m, mb - h - m, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)
        self.shown_key = "welcome"
        self.show()

    def watch_moves(self, pid):
        """Location events only for the target's process, so we're not woken by every window on screen."""
        if pid != self.loc_pid:
            self.events.watch(pid)
            self.loc_pid = pid

    # -- visibility and position (never steals focus from the app you're typing in)
    def hide(self):
        user32.ShowWindow(self.hwnd, SW_HIDE)
        self.visible = False
        self.shown_key = None

    def move_to(self, win, asynchronous=False):
        left, top, right, bottom = window_rect(win["hwnd"])
        s = dpi_scale(win["hwnd"])
        w, h = self.size
        x, y = right - w - int(16 * s), top + int(44 * s)
        ml, mt, mr, mb = monitor_work_area(win["hwnd"])
        m = int(8 * s)
        x = max(ml + m, min(max(x, left), mr - w - m))
        y = max(mt + m, min(y, mb - h - m))
        flags = SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER | (SWP_ASYNCWINDOWPOS if asynchronous else 0)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, 0, 0, flags)

    def show(self):
        if not self.visible:
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            self.visible = True

    # -- notes folder
    def rescan(self, force=False):
        try:
            files = sorted(p for p in self.dir.rglob("*.md") if ".trash" not in p.parts)
            stamp = tuple((str(f), f.stat().st_mtime_ns) for f in files)
        except OSError:
            return
        if force or stamp != self.stamp:
            self.stamp = stamp
            self.notes = [n for n in (parse_note(f) for f in files) if n]
            self.shown_key = None

    # -- main loop: events from the event thread, plus a slower full check
    #    (title changes, note edits, anything the events missed)
    def tick(self):
        try:
            update = False
            while not self.q.empty():
                msg = self.q.get()
                if msg == "hotkey-failed":
                    log(f"Ctrl+Alt+{HOTKEY_KEY} is already used by another app; the new-note hotkey is off.")
                elif msg == "new-note":
                    self.new_note()
                elif msg == "foreground":
                    update = True
            self.since_full += QUEUE_MS
            if self.since_full >= POLL_MS:
                self.since_full, update = 0, True
                self.scan_age += POLL_MS
                if self.scan_age >= RESCAN_MS:
                    self.rescan()
                    self.scan_age = 0
            if update:
                self.update_box()
        except Exception as e:
            log("Error:", e)
        self.root.after(QUEUE_MS, self.tick)

    def current_target(self):
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return self.target
        if self.target and hwnd == self.target["hwnd"] and window_pid(hwnd) != MY_PID:
            # same window: refresh only the title (cheap), and the URL when the title changed
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            win = dict(self.target, title=buf.value)
        else:
            win = window_info(hwnd)
            if win["pid"] == MY_PID:
                return self.target     # clicking our own box keeps the note up
            if win["class"] in IGNORED_CLASSES:
                return None
        win["url"] = self.urls.url(win)
        return win

    def update_box(self):
        if time.time() < getattr(self, "toast_until", 0):
            return                      # let the start-up message stay up for a moment
        if self.shown_key == "welcome":
            self.hide()
        win = self.current_target()
        self.target = win
        if not win or not user32.IsWindow(win["hwnd"]) or user32.IsIconic(win["hwnd"]):
            return self.hide()
        self.watch_moves(win["pid"])
        hits = [n for n in self.notes if matches(n, win) and (win["hwnd"], n["path"]) not in self.dismissed]
        if not hits:
            return self.hide()
        key = (win["hwnd"], tuple(n["path"] for n in hits), self.stamp, tuple(sorted(map(str, self.collapsed))))
        if key != self.shown_key:
            self.draw(hits, win)
            self.shown_key = key
        self.move_to(win)
        self.show()

    # -- drawing
    def draw(self, hits, win):
        for child in self.box.winfo_children():
            child.destroy()
        s = dpi_scale(win["hwnd"])
        width = int(BOX_WIDTH * s)
        for i, note in enumerate(hits):
            if i:
                tk.Frame(self.box, bg=self.BORDER, height=1).pack(fill="x")
            card = tk.Frame(self.box, bg=self.BG, padx=10, pady=7)
            card.pack(fill="x")
            open_ = note["path"] not in self.collapsed
            head = tk.Frame(card, bg=self.BG)
            head.pack(fill="x")
            title = tk.Label(head, text=("▾ " if open_ else "▸ ") + note["name"], bg=self.BG, fg=self.FG,
                             font=("Segoe UI Semibold", 10), anchor="w", justify="left",
                             wraplength=width - 90, cursor="hand2")
            title.pack(side="left", fill="x", expand=True)
            title.bind("<Button-1>", lambda e, n=note: self.toggle_collapse(n))
            close = tk.Label(head, text="×", bg=self.BG, fg=self.MUTED, font=("Segoe UI", 12), cursor="hand2")
            close.pack(side="right")
            close.bind("<Button-1>", lambda e, n=note, h=win["hwnd"]: self.dismiss(h, n))
            edit = tk.Label(head, text="Edit", bg=self.BG, fg=self.MUTED, font=("Segoe UI", 9, "underline"), cursor="hand2")
            edit.pack(side="right", padx=(0, 8))
            edit.bind("<Button-1>", lambda e, n=note: obsidian_open(n["path"]))
            if open_:
                self.draw_body(card, note, width - 20)
        tk.Frame(self.box, bg=self.BG, width=width, height=0).pack()  # fixes the width
        for w in [self.box] + self.box.winfo_children():
            self.bind_menu(w)
        self.root.update_idletasks()
        self.size = (width + 2, self.root.winfo_reqheight())
        self.root.geometry(f"{self.size[0]}x{self.size[1]}")

    def draw_body(self, card, note, wrap):
        lines = note["lines"]
        shown = lines[:MAX_BODY_LINES]
        body = tk.Frame(card, bg=self.BG)
        body.pack(fill="x", pady=(3, 0))
        for lineno, raw in shown:
            text = raw.rstrip()
            if not text.strip():
                tk.Frame(body, bg=self.BG, height=5).pack(fill="x")
                continue
            indent = (len(text) - len(text.lstrip())) // 2 * 14
            stripped = text.strip()
            kw = dict(bg=self.BG, fg=self.FG, font=("Segoe UI", 9), anchor="w", justify="left", wraplength=wrap - indent)
            box = re.match(r"^[-*+] \[([ xX])\] ?(.*)$", stripped)
            head = re.match(r"^#{1,6}\s+(.*)$", stripped)
            bullet = re.match(r"^(?:[-*+]|\d+[.)])\s+(.*)$", stripped)
            if box:
                done = box.group(1) in "xX"
                kw.update(text=("☑  " if done else "☐  ") + plain(box.group(2)), cursor="hand2",
                          fg=self.DONE if done else self.FG, font=("Segoe UI", 9, "overstrike") if done else kw["font"])
                lab = tk.Label(body, **kw)
                lab.bind("<Button-1>", lambda e, n=note, ln=lineno: self.check(n, ln))
            elif head:
                kw.update(text=plain(head.group(1)), font=("Segoe UI Semibold", 9))
                lab = tk.Label(body, **kw)
            elif bullet:
                num = re.match(r"^(\d+[.)])", stripped)
                kw.update(text=(num.group(1) + " " if num else "•  ") + plain(bullet.group(1)))
                lab = tk.Label(body, **kw)
            else:
                kw.update(text=plain(stripped))
                lab = tk.Label(body, **kw)
            lab.pack(fill="x", padx=(indent, 0))
        if len(lines) > MAX_BODY_LINES:
            more = tk.Label(body, text=f"{len(lines) - MAX_BODY_LINES} more lines: open in Obsidian", bg=self.BG,
                            fg=self.MUTED, font=("Segoe UI", 8, "underline"), anchor="w", cursor="hand2")
            more.pack(fill="x", pady=(3, 0))
            more.bind("<Button-1>", lambda e, n=note: obsidian_open(n["path"]))

    def bind_menu(self, widget):
        widget.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        for c in widget.winfo_children():
            self.bind_menu(c)

    # -- actions
    def toggle_collapse(self, note):
        self.collapsed.symmetric_difference_update({note["path"]})
        self.update_box()

    def check(self, note, lineno):
        try:
            toggle_checkbox(note["path"], lineno)
        except OSError as e:
            log("Couldn't save the checkbox:", e)
        self.rescan(force=True)
        self.update_box()

    def dismiss(self, hwnd, note):
        self.dismissed.add((hwnd, note["path"]))
        self.update_box()

    def set_autostart(self):
        try:
            if self.autostart.get():
                pyw = Path(sys.executable).with_name("pythonw.exe")
                STARTUP_FILE.write_text(f'@echo off\r\nstart "" "{pyw}" "{Path(__file__).resolve()}"\r\n', encoding="utf-8")
            elif STARTUP_FILE.exists():
                STARTUP_FILE.unlink()
        except OSError as e:
            log("Couldn't change Start with Windows:", e)
        self.autostart.set(STARTUP_FILE.exists())

    def new_note(self):
        win = self.target
        if not win:
            return
        title = win["title"] or "Untitled window"
        safe = re.sub(r'[<>:"/\\|?*#^\[\]]+', " ", title).strip()[:60].strip() or "Window note"
        path = self.dir / f"{safe}.md"
        i = 2
        while path.exists():
            path = self.dir / f"{safe} {i}.md"
            i += 1
        props = [f"attach-app: {yaml_str(win['app'])}"]
        if win["url"]:
            page = re.split(r"[?#]", norm_url(win["url"]))[0].rstrip("/")
            props.append(f"attach-url: {yaml_str(page + '*')}")   # this page, any ?query, and pages under it
            hint = 'Change attach-url to "site.com/*" to show it on the whole site.'
        else:
            props.append(f"attach-title: {yaml_str(re.sub(r'[*?\[\]]', '?', win['title']))}")
            hint = 'Use * in attach-title (for example "*Budget*"), or delete it to show this on every window of the app.'
        path.write_text("---\n" + "\n".join(props) + "\n---\n\nWrite your note here.\n\n" + hint + "\n", encoding="utf-8")
        log(f"New note: {path.name}")
        self.rescan(force=True)
        obsidian_open(path)

    def quit(self):
        self.events.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("Window Notes is Windows-only for now.")
    kernel32.CreateMutexW(None, False, "WindowNotesPrototype")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        log("Window Notes is already running.")
        user32.MessageBoxW(None, "Window Notes is already running.\n\nNotes appear on the windows they're attached to. "
                           "Press Ctrl+Alt+N in any window to attach one. Right-click a note to quit.",
                           "Window Notes", 0x40 | 0x40000)  # info icon, topmost
        sys.exit(0)
    VAULT, FOLDER, HOTKEY_KEY = load_config()
    App().run()
