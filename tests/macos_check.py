"""Hands-off check of the macOS version, run by .github/workflows/macos-check.yml on GitHub's Macs.
Starts Window Notes with a note attached to TextEdit, then checks that the note box appears on
TextEdit, follows it when it moves, and hides when TextEdit is minimized. Saves screenshots.
Not a unit test (needs a real Mac desktop), so `python -m unittest` skips it."""
import os
import subprocess
import sys
import time
from pathlib import Path

import ApplicationServices as AX
import Quartz as Q
from AppKit import NSRunningApplication

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("OUT", "mac-check"))
OUT.mkdir(exist_ok=True)
vault = Path("/tmp/vault")
(vault / "Window notes").mkdir(parents=True, exist_ok=True)
(vault / "Window notes" / "TextEdit.md").write_text(
    '---\nattach-app: "TextEdit"\nnote-color: "green"\n---\n\nShows on every **TextEdit** window.\n\n- [ ] Tick this box\n')

results = {}


def textedit_window():
    """TextEdit's first window as an Accessibility element. Driving TextEdit this way needs only
    the Accessibility permission, which GitHub's Macs have. AppleScript needs a separate
    "control TextEdit" permission, and that dialog waits for a click that never comes."""
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.TextEdit")
    if not apps:
        return None
    err, windows = AX.AXUIElementCopyAttributeValue(
        AX.AXUIElementCreateApplication(apps[0].processIdentifier()), AX.kAXWindowsAttribute, None)
    return windows[0] if not err and windows else None


def move_textedit(x, y, w=800, h=500):
    win = textedit_window()
    if win is None:
        print("no TextEdit window to move")
        return
    AX.AXUIElementSetAttributeValue(win, AX.kAXPositionAttribute,
                                    AX.AXValueCreate(AX.kAXValueCGPointType, Q.CGPointMake(x, y)))
    AX.AXUIElementSetAttributeValue(win, AX.kAXSizeAttribute,
                                    AX.AXValueCreate(AX.kAXValueCGSizeType, Q.CGSizeMake(w, h)))


def activate(app_name):
    subprocess.run(["open", "-a", app_name], timeout=15)


def dump_windows():
    """Print what's on screen, front to back, to show why the box didn't appear."""
    for w in Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly, Q.kCGNullWindowID) or []:
        b = w["kCGWindowBounds"]
        print(f"  {w.get('kCGWindowOwnerName')!s:24} layer {w.get('kCGWindowLayer')!s:4} "
              f"{int(b['X'])},{int(b['Y'])} {int(b['Width'])}x{int(b['Height'])} {w.get('kCGWindowName') or ''}")


def shot(name):
    try:
        subprocess.run(["screencapture", "-x", str(OUT / f"{name}.png")], timeout=15)
    except subprocess.TimeoutExpired:
        print("screencapture timed out")


def box_bounds(pid):
    """Bounds of our note box: the Window Notes process's on-screen window."""
    for w in Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly, Q.kCGNullWindowID) or []:
        if w.get("kCGWindowOwnerPID") == pid and w["kCGWindowBounds"]["Width"] > 50:
            b = w["kCGWindowBounds"]
            return int(b["X"]), int(b["Y"]), int(b["X"] + b["Width"]), int(b["Y"] + b["Height"])
    return None


results["accessibility permission"] = bool(AX.AXIsProcessTrusted())

env = dict(os.environ, WN_VAULT=str(vault))
app = subprocess.Popen([sys.executable, str(ROOT / "window_notes.py")], env=env,
                       stdout=open(OUT / "app.log", "w"), stderr=subprocess.STDOUT)
time.sleep(7)                                   # start-up message disappears after 4 s

doc = Path("/tmp/check.txt")
doc.write_text("A TextEdit window for the check.\n")
subprocess.run(["open", "-a", "TextEdit", str(doc)], timeout=15)
time.sleep(3)
move_textedit(100, 100)
time.sleep(1.5)
shot("1-note-on-textedit")
box = box_bounds(app.pid)
results["box visible on TextEdit"] = box is not None
results["box position"] = box
if not box:
    print("Windows on screen when the box was missing:")
    dump_windows()

if box:
    # Move TextEdit in steps, staying fully on screen (near an edge the box is kept on screen
    # instead, which is intended). At each step, measure where TextEdit really is rather than
    # trusting the requested position: macOS may adjust it.
    textedit_pid = NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.TextEdit")[0].processIdentifier()
    gaps = []
    for i in range(1, 11):
        move_textedit(100 + i * 15, 100 + i * 10, w=500, h=400)
        time.sleep(0.25)
        b, t = box_bounds(app.pid), box_bounds(textedit_pid)
        if b and t:
            gaps.append((t[2] - b[2], b[1] - t[1]))     # (TextEdit's right edge - box's, box top - TextEdit top)
    results["gaps while TextEdit moves (right, top)"] = sorted(set(gaps))
    results["box follows TextEdit"] = len(gaps) == 10 and len(set(gaps)) == 1
    shot("2-after-moving")

    activate("Finder")
    time.sleep(1.0)
    results["hidden when another app is active"] = box_bounds(app.pid) is None
    shot("3-finder-active")

    activate("TextEdit")
    time.sleep(1.0)
    results["back on TextEdit"] = box_bounds(app.pid) is not None
    AX.AXUIElementSetAttributeValue(textedit_window(), AX.kAXMinimizedAttribute, True)
    time.sleep(1.5)
    results["hidden when TextEdit is minimized"] = box_bounds(app.pid) is None

app.terminate()
for key, value in results.items():
    print(f"{key}: {value}")
print("--- app log")
print((OUT / "app.log").read_text())

# Fail the GitHub check if any yes/no result is "no", so a green check means the box really works.
sys.exit(0 if all(v for v in results.values() if isinstance(v, bool)) else 1)
