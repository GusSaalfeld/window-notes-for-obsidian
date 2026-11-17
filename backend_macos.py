"""macOS part of Window Notes.

Needs PyObjC:  pip install pyobjc-framework-Quartz pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices
and the Accessibility permission (System Settings > Privacy & Security > Accessibility) for
the app that runs it (Terminal when run from source, or Window Notes.app). Without it,
window titles and the hotkey don't work; notes attached by app name still do.

Coordinates: macOS's window list uses points with (0, 0) at the top-left of the main screen,
like Tk. AppKit (NSWindow, NSScreen) puts (0, 0) at the bottom-left, so those get flipped.
AppKit must only be used from the main thread, so the event thread here just polls and
reports; the Tk thread does all moving and showing.
"""
import fcntl
import os
import subprocess
import sys
import threading
from pathlib import Path

try:
    import ApplicationServices as AX
    import Quartz as Q
    from AppKit import NSApplication, NSRunningApplication, NSScreen, NSWorkspace
    from PyObjCTools import AppHelper
except ImportError:
    sys.exit("Window Notes needs PyObjC: pip install pyobjc-framework-Quartz pyobjc-framework-Cocoa "
             "pyobjc-framework-ApplicationServices")

NAME = "macOS"
AUTOSTART_LABEL = "Start at login"
FONT = "Helvetica Neue"
FONT_BOLD = None
SUPPORTS_URL = True
TITLE_BAR = 32              # points: window bounds include the title bar
NOTICE = ("" if AX.AXIsProcessTrustedWithOptions({AX.kAXTrustedCheckOptionPrompt: True}) else
          "Accessibility permission is off, so window titles and the hotkey won't work. Turn it on in "
          "System Settings > Privacy & Security > Accessibility, then restart Window Notes.")

ON_SCREEN = Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements
# Mac virtual key codes for letters (US layout positions), for the hotkey.
KEYCODES = dict(zip("ASDFHGZXCVBQWERYTOUIPLJKNM",
                    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15, 16, 17, 31, 32, 34, 35, 37, 38, 40, 45, 46]))


# ---- app start ------------------------------------------------------------
def single_instance():
    """True if this is the only copy running (an exclusive lock on a file)."""
    single_instance.lock = open(Path(os.environ.get("TMPDIR", "/tmp")) / f"window-notes-{os.getuid()}.lock", "w")
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
    Raises OSError if no app can open it, so the app can say so."""
    try:
        done = subprocess.run(["open", str(target)], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return
    if done.returncode:
        raise OSError(done.stderr.strip() or "no app can open this")


def data_dir():
    return Path.home() / "Library" / "Application Support" / "Window Notes"


AUTOSTART_FILE = Path.home() / "Library" / "LaunchAgents" / "io.github.window-notes.plist"


def autostart_enabled():
    return AUTOSTART_FILE.exists()


def set_autostart(on, command):
    if on:
        import plistlib
        AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(AUTOSTART_FILE, "wb") as f:
            plistlib.dump({"Label": "io.github.window-notes", "ProgramArguments": list(command), "RunAtLoad": True}, f)
    elif AUTOSTART_FILE.exists():
        AUTOSTART_FILE.unlink()


# ---- windows --------------------------------------------------------------
def _windows(options=ON_SCREEN, wid=Q.kCGNullWindowID):
    return Q.CGWindowListCopyWindowInfo(options, wid) or []


def _info(wid):
    """The window list entry for one window, or None if it's gone."""
    found = _windows(Q.kCGWindowListOptionIncludingWindow, wid)
    return found[0] if found else None


def _front_window():
    """The frontmost normal window (layer 0) on screen; the list is ordered front to back."""
    return next((w for w in _windows() if w.get("kCGWindowLayer", 0) == 0), None)


def _ax_title(pid):
    """Title of the app's focused window, via Accessibility (empty without permission).
    The timeout matters: by default macOS waits up to 6 s for a busy app to answer, and
    every wait would freeze the note box."""
    app = AX.AXUIElementCreateApplication(pid)
    AX.AXUIElementSetMessagingTimeout(app, 0.3)
    err, window = AX.AXUIElementCopyAttributeValue(app, AX.kAXFocusedWindowAttribute, None)
    if err or window is None:
        return ""
    err, title = AX.AXUIElementCopyAttributeValue(window, AX.kAXTitleAttribute, None)
    return "" if err or title is None else str(title)


def foreground(known=None):
    """The active window as {id, app, title, pid}, or None (only the desktop is active).
    `known` is the last result: if it's the same window, only the title is re-read."""
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return None
    pid = app.processIdentifier()
    w = next((w for w in _windows() if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer", 0) == 0), None)
    if w is None:
        return None
    wid = w["kCGWindowNumber"]
    title = _ax_title(pid) or str(w.get("kCGWindowName") or "")
    if known and known["id"] == wid:
        return dict(known, title=title)
    return {"id": wid, "app": str(app.localizedName() or ""), "title": title, "pid": pid}


def exists(wid):
    return _info(wid) is not None


def minimized(wid):
    info = _info(wid)
    return not (info and info.get("kCGWindowIsOnscreen"))


def rect(wid):
    """Window bounds (left, top, right, bottom) in points, title bar included."""
    info = _info(wid)
    if not info:
        return 0, 0, 0, 0
    b = info["kCGWindowBounds"]
    return b["X"], b["Y"], b["X"] + b["Width"], b["Y"] + b["Height"]


def _main_height():
    return NSScreen.screens()[0].frame().size.height


def work_area(wid=None):
    """Visible screen area (without menu bar and Dock) of the screen the window is on."""
    h0 = _main_height()
    cx = cy = None
    if wid:
        l, t, r, b = rect(wid)
        cx, cy = (l + r) / 2, (t + b) / 2
    best = NSScreen.mainScreen()
    for screen in NSScreen.screens():
        f = screen.frame()
        top = h0 - (f.origin.y + f.size.height)
        if cx is not None and f.origin.x <= cx < f.origin.x + f.size.width and top <= cy < top + f.size.height:
            best = screen
    v = best.visibleFrame()
    top = h0 - (v.origin.y + v.size.height)
    return v.origin.x, top, v.origin.x + v.size.width, top + v.size.height


def scale(wid=None):
    return 1.0   # everything is in points already


# ---- the note box ---------------------------------------------------------
class Overlay:
    """Shows, hides and moves the Tk note box through its NSWindow: a floating window that
    stays above other apps' windows without bringing Window Notes to the front."""

    def __init__(self, root):
        NSApplication.sharedApplication().setActivationPolicy_(1)   # accessory app: no Dock icon
        root.update_idletasks()
        title = root.title()
        windows = list(NSApplication.sharedApplication().windows())
        self.window = next((w for w in windows if w.title() == title), windows[-1])
        self.window.setLevel_(3)                 # NSFloatingWindowLevel
        self.window.setCollectionBehavior_(1 | 256)   # on all Spaces, and over full-screen apps
        self.window.setHidesOnDeactivate_(False)
        self.id = int(self.window.windowNumber())          # the window list's id for it

    def show(self):
        self.window.orderFrontRegardless()

    # AppKit windows may only be changed on the main thread, so calls from the event thread
    # are handed over with callAfter (Tk's event loop runs the main thread's Cocoa run loop).
    def hide(self, from_thread=False):
        if from_thread:
            return AppHelper.callAfter(self.window.orderOut_, None)
        self.window.orderOut_(None)

    def move(self, x, y, from_thread=False):
        if from_thread:
            return AppHelper.callAfter(self.move, x, y)
        self.window.setFrameTopLeftPoint_((x, _main_height() - y))

    def is_own(self, win):
        return win["pid"] == os.getpid()

    def after_click(self, win):
        """Clicking the box makes Window Notes the active app; give focus back to the app
        the note belongs to, so you can keep typing there."""
        if win:
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(win["pid"])
            if app:
                app.activateWithOptions_(0)


# ---- hotkey and window events (own thread) -----------------------------------
class Events(threading.Thread):
    """Polls the window list (cheap) every 20 ms and listens for the hotkey. Reports a new
    front window with on_foreground (a full re-check), and a move or minimize of the watched
    window with on_moved (just repositions the box, so dragging stays smooth)."""

    def __init__(self, hotkey, on_foreground, on_moved, on_hotkey, on_hotkey_failed):
        super().__init__(daemon=True)
        self.hotkey = hotkey
        self.on_foreground, self.on_moved = on_foreground, on_moved
        self.on_hotkey, self.on_hotkey_failed = on_hotkey, on_hotkey_failed
        self.ready, self.stopped, self.want = threading.Event(), False, None

    def _key_tap(self):
        """A listen-only keyboard tap for Ctrl+Option+<key>. Needs Accessibility permission."""
        keycode = KEYCODES.get(self.hotkey)
        wanted = Q.kCGEventFlagMaskControl | Q.kCGEventFlagMaskAlternate
        others = Q.kCGEventFlagMaskCommand | Q.kCGEventFlagMaskShift

        def callback(proxy, kind, event, refcon):
            if kind in (Q.kCGEventTapDisabledByTimeout, Q.kCGEventTapDisabledByUserInput):
                Q.CGEventTapEnable(self.tap, True)        # macOS switches slow taps off; switch it back on
            elif (Q.CGEventGetIntegerValueField(event, Q.kCGKeyboardEventKeycode) == keycode
                  and Q.CGEventGetFlags(event) & (wanted | others) == wanted):
                self.on_hotkey()
            return event

        self.tap = Q.CGEventTapCreate(Q.kCGSessionEventTap, Q.kCGHeadInsertEventTap, Q.kCGEventTapOptionListenOnly,
                                      Q.CGEventMaskBit(Q.kCGEventKeyDown), callback, None)
        if self.tap is None or keycode is None:
            self.on_hotkey_failed()
            return
        source = Q.CFMachPortCreateRunLoopSource(None, self.tap, 0)
        Q.CFRunLoopAddSource(Q.CFRunLoopGetCurrent(), source, Q.kCFRunLoopCommonModes)
        Q.CGEventTapEnable(self.tap, True)

    def run(self):
        self._key_tap()
        self.ready.set()
        last_front = last_place = None
        while not self.stopped:
            Q.CFRunLoopRunInMode(Q.kCFRunLoopDefaultMode, 0.02, False)   # delivers key events for 20 ms
            front = _front_window()
            front = front and front["kCGWindowNumber"]
            if front != last_front:
                last_front = front
                self.on_foreground()
            if self.want:
                watched = _info(self.want)
                place = watched and (dict(watched["kCGWindowBounds"]), watched.get("kCGWindowIsOnscreen"))
                if place != last_place:
                    last_place = place
                    self.on_moved(self.want)

    def watch(self, win):
        self.want = win["id"]

    def stop(self):
        self.stopped = True


# ---- browser address (AppleScript) -----------------------------------------
class UrlReader:
    """Asks the browser for its address with AppleScript. The first time, macOS asks you to
    allow Window Notes to control that browser. Firefox doesn't support this."""
    SCRIPTS = {
        "safari": 'tell application "Safari" to get URL of front document',
        "google chrome": 'tell application "Google Chrome" to get URL of active tab of front window',
        "microsoft edge": 'tell application "Microsoft Edge" to get URL of active tab of front window',
        "brave browser": 'tell application "Brave Browser" to get URL of active tab of front window',
        "arc": 'tell application "Arc" to get URL of active tab of front window',
    }

    def __init__(self, log):
        self.cache = {}

    def url(self, win):
        script = self.SCRIPTS.get(win["app"].lower())
        if not script:
            return ""
        key = (win["id"], win["title"])   # ask again only when the tab title changes
        if key not in self.cache:
            if len(self.cache) > 300:
                self.cache.clear()
            try:
                out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=2)
                self.cache[key] = out.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                self.cache[key] = ""
        return self.cache[key]
