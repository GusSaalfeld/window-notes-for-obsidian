"""Temporary diagnostic: after each TextEdit move (via Accessibility, like macos_check.py),
sample the gap between TextEdit and the note box every 50 ms for 2 s, and print the timeline."""
import os, subprocess, sys, time
from pathlib import Path
import ApplicationServices as AX
import Quartz as Q
from AppKit import NSRunningApplication

ROOT = Path(__file__).resolve().parent.parent
vault = Path("/tmp/vault-tl"); (vault / "Window notes").mkdir(parents=True, exist_ok=True)
(vault / "Window notes" / "TextEdit.md").write_text('---\nattach-app: "TextEdit"\n---\n\n- [ ] x\n')
app = subprocess.Popen([sys.executable, str(ROOT / "window_notes.py")], env=dict(os.environ, WN_VAULT=str(vault)),
                       stdout=open("/tmp/tl-app.log", "w"), stderr=subprocess.STDOUT)
time.sleep(7)
Path("/tmp/tl.txt").write_text("x\n")
subprocess.run(["open", "-a", "TextEdit", "/tmp/tl.txt"], timeout=15); time.sleep(3)
pid = NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.TextEdit")[0].processIdentifier()

def win():
    err, ws = AX.AXUIElementCopyAttributeValue(AX.AXUIElementCreateApplication(pid), AX.kAXWindowsAttribute, None)
    return ws[0]

def move(x, y, w=500, h=400):
    AX.AXUIElementSetAttributeValue(win(), AX.kAXPositionAttribute, AX.AXValueCreate(AX.kAXValueCGPointType, Q.CGPointMake(x, y)))
    AX.AXUIElementSetAttributeValue(win(), AX.kAXSizeAttribute, AX.AXValueCreate(AX.kAXValueCGSizeType, Q.CGSizeMake(w, h)))

def bounds(p):
    for w in Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly, Q.kCGNullWindowID) or []:
        if w.get("kCGWindowOwnerPID") == p and w["kCGWindowBounds"]["Width"] > 50 and w.get("kCGWindowLayer", 0) in (0, 3):
            b = w["kCGWindowBounds"]; return int(b["X"]), int(b["Y"]), int(b["X"] + b["Width"]), int(b["Y"] + b["Height"])

move(100, 100); time.sleep(2)
never = 0
for i in range(1, 11):
    move(100 + i * 15, 100 + i * 10)
    t0, line, first_ok = time.time(), [], None
    while time.time() - t0 < 2:
        b, t = bounds(app.pid), bounds(pid)
        g = (t[2] - b[2], b[1] - t[1]) if b and t else None
        if g == (16, 32) and first_ok is None:
            first_ok = time.time() - t0
        line.append(g)
        time.sleep(0.05)
    final_ok = line[-1] == (16, 32)
    never += not final_ok
    changes = [g for k, g in enumerate(line) if k == 0 or g != line[k - 1]]
    print(f"move {i}: right after={line[0]} correct after={'%.2fs' % first_ok if first_ok is not None else 'never'} "
          f"after 2 s={line[-1]} changes={changes}", flush=True)
print("moves that never ended correct:", never)
app.terminate()
print(open("/tmp/tl-app.log").read()[-800:])
