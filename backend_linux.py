"""Linux part of Window Notes, for X11 desktops (and X11 apps in a Wayland session).

Uses python-xlib (Ubuntu/Debian: sudo apt install python3-xlib python3-tk).
Window information comes from the standard EWMH properties every X11 window manager sets:
_NET_ACTIVE_WINDOW (which window is active), _NET_WM_NAME (title), _NET_WM_PID (program),
_NET_FRAME_EXTENTS (title bar and borders), _NET_WORKAREA (screen minus panels).
"""
import fcntl
import os
import select
import shutil
import subprocess
import sys
import threading
from pathlib import Path

try:
    import Xlib.threaded   # noqa: F401  (makes one X connection safe to use from two threads)
    from Xlib import X, XK, display, error
except ImportError:
    sys.exit("Window Notes needs python-xlib:  sudo apt install python3-xlib  (or: pip install python-xlib)")

if not os.environ.get("DISPLAY"):
    sys.exit("Window Notes needs an X11 desktop (no DISPLAY found). Pure Wayland sessions aren't supported yet.")

NAME = "Linux"
AUTOSTART_LABEL = "Start at login"
FONT = "DejaVu Sans"
FONT_BOLD = None            # use FONT with Tk's "bold" style
SUPPORTS_URL = False        # browser addresses aren't readable here yet; match browsers by title
TITLE_BAR = 44              # px: rect() includes the title bar, so the box sits just below it
NOTICE = ("Wayland session: only X11 (XWayland) apps can be detected; native Wayland apps won't show notes."
          if os.environ.get("XDG_SESSION_TYPE") == "wayland" else "")

_d = display.Display()      # used by the main thread (and on_moved, thanks to Xlib.threaded)
_root = _d.screen().root
_atoms = {}
INTERPRETERS = ("python", "perl", "ruby", "node", "java", "bash", "sh", "electron")


def _atom(name):
    if name not in _atoms:
        _atoms[name] = _d.intern_atom(name)
    return _atoms[name]


def _win(wid):
    return _d.create_resource_object("window", wid)


def _prop(window, name):
    """A window property's value (list or bytes), or None if it's missing or the window is gone."""
    try:
        p = window.get_full_property(_atom(name), X.AnyPropertyType)
        return p.value if p else None
    except error.XError:
        return None


# ---- app start ------------------------------------------------------------
def single_instance():
    """True if this is the only copy running (an exclusive lock on a file in the runtime folder)."""
    folder = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    single_instance.lock = open(Path(folder) / f"window-notes-{os.getuid()}.lock", "w")
    try:
        fcntl.flock(single_instance.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def message(text):
    import tkinter as tk
    from tkinter import messagebox
    r = tk.Tk()
    r.withdraw()
    messagebox.showinfo("Window Notes", text)
    r.destroy()


def open_target(target):
    """Open a file, folder, web address or obsidian:// link with its default app.
    Raises OSError if that can't be done, so the app can say so."""
    if not shutil.which("xdg-open"):
        raise OSError("xdg-open isn't installed (it comes with the xdg-utils package)")
    proc = subprocess.Popen(["xdg-open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:   # xdg-open fails within a moment when nothing handles the link; when it works it may keep running
        if proc.wait(timeout=1.5) not in (0, None):
            raise OSError(proc.stderr.read().decode(errors="replace").strip() or "no app can open this")
    except subprocess.TimeoutExpired:
        pass


AUTOSTART_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "window-notes.desktop"


def data_dir():
    """Settings folder for the downloaded program: ~/.config/window-notes"""
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "window-notes"


def autostart_enabled():
    return AUTOSTART_FILE.exists()


def set_autostart(on, command):
    """`command`: the program and its arguments, e.g. [python3, window_notes.py] or [./window-notes]."""
    if on:
        AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
        exec_line = " ".join(f'"{part}"' for part in command)
        AUTOSTART_FILE.write_text("[Desktop Entry]\nType=Application\nName=Window Notes\n"
                                  f"Exec={exec_line}\nX-GNOME-Autostart-enabled=true\n")
    elif AUTOSTART_FILE.exists():
        AUTOSTART_FILE.unlink()


# ---- windows --------------------------------------------------------------
def _title(window):
    name = _prop(window, "_NET_WM_NAME")          # UTF-8 title
    if name is None:
        name = _prop(window, "WM_NAME")           # older apps
    if isinstance(name, bytes):
        return name.decode("utf-8", "replace")
    return name or ""


def _app_name(pid, window):
    """The program's name, like 'firefox' or 'gedit'. For scripts (python, java...) the
    interpreter's name says nothing, so the window's class is used instead."""
    try:
        exe = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
        if exe and not exe.startswith(INTERPRETERS):
            return exe
    except OSError:
        pass
    try:
        cls = window.get_wm_class()               # (instance, class), e.g. ('gedit', 'Gedit')
        return cls[0] if cls else ""
    except error.XError:
        return ""


def foreground(known=None):
    """The active window as {id, app, title, pid}, or None (desktop, panel, nothing active).
    `known` is the last result: if it's the same window, only the title is re-read."""
    active = _prop(_root, "_NET_ACTIVE_WINDOW")
    if not active or not active[0]:
        return None
    wid = active[0]
    window = _win(wid)
    if known and known["id"] == wid:
        return dict(known, title=_title(window))
    kinds = _prop(window, "_NET_WM_WINDOW_TYPE") or []
    if _atom("_NET_WM_WINDOW_TYPE_DESKTOP") in kinds or _atom("_NET_WM_WINDOW_TYPE_DOCK") in kinds:
        return None
    pid = (_prop(window, "_NET_WM_PID") or [0])[0]
    return {"id": wid, "app": _app_name(pid, window), "title": _title(window), "pid": pid}


def exists(wid):
    try:
        _win(wid).get_geometry()
        return True
    except error.XError:
        return False


def minimized(wid):
    return _atom("_NET_WM_STATE_HIDDEN") in (_prop(_win(wid), "_NET_WM_STATE") or [])


def rect(wid):
    """Window bounds (left, top, right, bottom) on screen, including the title bar and borders."""
    window = _win(wid)
    try:
        g = window.get_geometry()
        pos = _root.translate_coords(window, 0, 0)   # window's corner in screen coordinates
    except error.XError:
        return 0, 0, 0, 0
    left, right, top, bottom = _prop(window, "_NET_FRAME_EXTENTS") or (0, 0, 0, 0)
    return pos.x - left, pos.y - top, pos.x + g.width + right, pos.y + g.height + bottom


def work_area(wid=None):
    """Screen area without panels. (With several monitors this is the whole desktop.)"""
    area = _prop(_root, "_NET_WORKAREA")
    if area and len(area) >= 4:
        desk = (_prop(_root, "_NET_CURRENT_DESKTOP") or [0])[0]
        x, y, w, h = area[desk * 4: desk * 4 + 4] if len(area) >= desk * 4 + 4 else area[:4]
        return x, y, x + w, y + h
    s = _d.screen()
    return 0, 0, s.width_in_pixels, s.height_in_pixels


def scale(wid=None):
    return 1.0   # Tk already scales fonts for the screen's DPI


# ---- the note box ---------------------------------------------------------
class Overlay:
    """Shows, hides and moves the Tk note box with direct X calls, so the event thread
    can move it too. The box is override-redirect: the window manager ignores it, so it
    never takes focus and stays above normal windows when raised."""

    def __init__(self, root):
        root.update_idletasks()
        # Tk wraps the toplevel in an extra X window; we must move the outermost one
        # (the direct child of the desktop), or we'd only shift the inner window inside it.
        self.window = _win(int(root.wm_frame(), 16))
        while (parent := self.window.query_tree().parent) and parent != _root:
            self.window = parent
        self.id = self.window.id                           # what rect() etc. accept

    def show(self):
        self.window.map()
        self.window.configure(stack_mode=X.Above)
        _d.flush()

    def hide(self, from_thread=False):
        self.window.unmap()
        _d.flush()

    def move(self, x, y, from_thread=False):
        self.window.configure(x=x, y=y, stack_mode=X.Above)
        _d.flush()

    def is_own(self, win):
        return win["pid"] == os.getpid()

    def after_click(self, win):
        pass   # override-redirect windows never take focus, so there's nothing to give back


# ---- hotkey and window events (own thread, own X connection) ---------------
class Events(threading.Thread):
    """Calls back on: the active window changing, the watched window moving or being
    minimized, and the hotkey. Callbacks run on this thread: keep them quick."""

    def __init__(self, hotkey, on_foreground, on_moved, on_hotkey, on_hotkey_failed):
        super().__init__(daemon=True)
        self.hotkey = hotkey
        self.on_foreground, self.on_moved = on_foreground, on_moved
        self.on_hotkey, self.on_hotkey_failed = on_hotkey, on_hotkey_failed
        self.ready, self.stopped = threading.Event(), False
        self.want = None          # window id to watch, set by watch(), picked up by run()

    def run(self):
        d = display.Display()     # a separate connection, so our events don't mix with the main thread's
        root = d.screen().root
        root.change_attributes(event_mask=X.PropertyChangeMask)   # tells us when _NET_ACTIVE_WINDOW changes
        active_atom = d.intern_atom("_NET_ACTIVE_WINDOW")
        watch_atoms = {d.intern_atom("_NET_WM_STATE"), d.intern_atom("_NET_WM_NAME"), d.intern_atom("WM_NAME")}
        self._grab_hotkey(d, root)
        self.ready.set()

        watched, client = [], None   # windows we listen to (client + its window-manager frame)
        while not self.stopped:
            if self.want != client:
                for w in watched:
                    # the window may be gone by now; CatchError keeps X from printing that as an error
                    w.change_attributes(event_mask=X.NoEventMask, onerror=error.CatchError())
                client = self.want
                watched = self._watch(d, client) if client else []
            # wait up to 0.1 s for X events, then loop (to notice watch() and stop())
            if not d.pending_events():
                select.select([d.fileno()], [], [], 0.1)
            while d.pending_events():
                ev = d.next_event()
                if ev.type == X.PropertyNotify and ev.window == root and ev.atom == active_atom:
                    self.on_foreground()
                elif ev.type == X.PropertyNotify and ev.atom in watch_atoms:
                    self.on_foreground()             # title or minimized state changed: re-check
                elif ev.type in (X.ConfigureNotify, X.UnmapNotify, X.MapNotify) and client:
                    self.on_moved(client)
                elif ev.type == X.KeyPress:
                    self.on_hotkey()

    def _grab_hotkey(self, d, root):
        """Ctrl+Alt+<key>, grabbed with and without Caps Lock / Num Lock (they count as modifiers in X)."""
        keycode = d.keysym_to_keycode(XK.string_to_keysym(self.hotkey.lower()))
        catch = error.CatchError(error.BadAccess)
        for extra in (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask):
            root.grab_key(keycode, X.ControlMask | X.Mod1Mask | extra, True,
                          X.GrabModeAsync, X.GrabModeAsync, onerror=catch)
        d.sync()
        if catch.get_error():
            self.on_hotkey_failed()

    @staticmethod
    def _watch(d, wid):
        """Listen for moves on the window and on the frame the window manager wraps it in
        (dragging moves the frame; not every window manager tells the window itself)."""
        windows, w = [], d.create_resource_object("window", wid)
        quiet = error.CatchError()              # the window can close at any moment; don't print that
        try:
            w.change_attributes(event_mask=X.StructureNotifyMask | X.PropertyChangeMask, onerror=quiet)
            windows.append(w)
            root = d.screen().root
            while True:                         # climb to the top-level frame
                parent = w.query_tree().parent
                if not parent or parent == root:
                    break
                w = parent
            if w.id != wid:
                w.change_attributes(event_mask=X.StructureNotifyMask, onerror=quiet)
                windows.append(w)
        except error.XError:
            pass
        return windows

    def watch(self, win):
        self.want = win["id"]

    def stop(self):
        self.stopped = True
