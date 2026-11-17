"""End-to-end fail-state test of Window Notes on a real desktop (Windows, Linux X11 or macOS).
Run by .github/workflows/integration.yml; you can also run it yourself:  python tests/integration.py
It needs a desktop session it may take over for about two minutes, and must not run while
another copy of Window Notes is running.

It starts the app with a temporary vault, opens windows it controls (tests/target_window.py),
and checks both what the app reports (WN_TEST_STATE) and what's really on screen (through the
same backend module the app uses). Every scenario is recorded; the run continues past failures
and exits non-zero if any failed."""
import faulthandler
import json
import os
import queue
import threading
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

# If the whole run stalls, print where every thread is stuck and stop, instead of hanging
# until the CI job is cancelled with no clue.
faulthandler.dump_traceback_later(600, exit=True)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if sys.platform == "win32":
    import backend_windows as B
elif sys.platform == "darwin":
    import backend_macos as B
else:
    import backend_linux as B

WORK = Path(tempfile.mkdtemp(prefix="wn-test-"))
VAULT, DATA = WORK / "vault", WORK / "data"
NOTES = VAULT / "Window notes"
STATE = WORK / "state.json"
for d in (NOTES, DATA):
    d.mkdir(parents=True)
TARGET_NOTE = NOTES / "Target note.md"
TARGET_TEXT = '---\nattach-title: "WN Target*"\n---\n\nShown on the target.\n\n- [ ] Tick me\n- [ ] Another\n'

results = []        # (scenario, passed, detail)
STOPS_APP = {"damaged config.toml doesn't stop it", "missing vault folder: clear exit, nothing created"}


# ---- helpers -------------------------------------------------------------------
def wait_for(check, timeout=4.0, every=0.05):
    """Call check() until it returns something truthy; return that, or None on timeout."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            value = check()
            if value:
                return value
        except Exception:
            pass
        time.sleep(every)
    return None


def state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def visible(wid):
    """Is the box really on screen, according to the system (not the app)?"""
    if sys.platform == "win32":
        return bool(B.user32.IsWindowVisible(wid))
    if sys.platform == "darwin":
        info = B._info(wid)
        return bool(info and info.get("kCGWindowIsOnscreen"))
    try:
        return B._win(wid).get_attributes().map_state == 2      # IsViewable
    except Exception:
        return False


def box_rect():
    return B.rect(state()["box_id"])


def showing():
    s = state()
    return s.get("showing") and visible(s["box_id"])


def why():
    """What the app reports and what the system says, for failure messages."""
    s = state()
    age = time.time() - s.get("alive", 0)
    return (f"app (last report {age:.1f}s ago, {s.get('notes_on_disk')} attached notes on disk) says "
            f"showing={s.get('showing')} target={s.get('target')!r} notes={s.get('notes')} "
            f"errors={s.get('errors', [])[-2:]}; on screen={visible(s['box_id']) if s else '?'}; "
            f"front window={(B.foreground() or {}).get('title')!r}")


def hidden():
    s = state()
    return s and not s.get("showing") and not visible(s["box_id"])


def click(x, y):
    if sys.platform == "win32":
        B.user32.SetCursorPos(int(x), int(y))
        B.user32.mouse_event(2, 0, 0, 0, 0)
        B.user32.mouse_event(4, 0, 0, 0, 0)
    elif sys.platform == "darwin":
        import Quartz as Q
        for kind in (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp):
            Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateMouseEvent(None, kind, (x, y), Q.kCGMouseButtonLeft))
            time.sleep(0.05)
    else:
        subprocess.run(["xdotool", "mousemove", str(int(x)), str(int(y)), "click", "1"], timeout=5)


def press_hotkey():
    if sys.platform == "win32":
        for key in (0x11, 0x12, 0x4E):
            B.user32.keybd_event(key, 0, 0, 0)
        for key in (0x4E, 0x12, 0x11):
            B.user32.keybd_event(key, 0, 2, 0)
    elif sys.platform == "darwin":
        import Quartz as Q
        for down in (True, False):
            ev = Q.CGEventCreateKeyboardEvent(None, 45, down)          # 45 = N
            Q.CGEventSetFlags(ev, Q.kCGEventFlagMaskControl | Q.kCGEventFlagMaskAlternate)
            Q.CGEventPost(Q.kCGHIDEventTap, ev)
    else:
        subprocess.run(["xdotool", "key", "ctrl+alt+n"], timeout=5)


def click_in_box(kind):
    """Click the first clickable of this kind ("checkbox" or "edit") in the note box."""
    target = next(c for c in state()["clickable"] if c["kind"] == kind)
    left, top, _, _ = box_rect()
    x, y = left + target["x"] + min(12, target["w"] // 2), top + target["y"] + target["h"] // 2
    click(x, y)
    return f"clicked {kind} at ({x}, {y}); box at ({left}, {top}); {kind} rect {target}"


class Window:
    """A tests/target_window.py process."""

    def __init__(self, title):
        self.title = title
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "tests" / "target_window.py"), title],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.lines = queue.Queue()            # its answers, read on a thread so a hang can time out
        threading.Thread(target=lambda: [self.lines.put(l.strip()) for l in self.proc.stdout], daemon=True).start()
        self.id = int(self.answer("start").split()[1])

    def answer(self, what, timeout=15):
        try:
            return self.lines.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"target window {self.title!r} didn't answer {what!r} within {timeout} s")

    def do(self, command):
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()
        reply = self.answer(command)
        if reply != "ok":
            raise RuntimeError(f"target window: {command!r} -> {reply}")
        time.sleep(0.05)

    def rect(self):
        return B.rect(self.id)

    def close(self):
        try:
            self.do("quit")
        except (OSError, ValueError):
            pass
        self.proc.wait(timeout=5)


def start_app(extra_env=None, data=DATA):
    env = dict(os.environ)
    env.update(WN_VAULT=str(VAULT), WN_DATA=str(data), WN_TEST_STATE=str(STATE))
    env.update(extra_env or {})
    log = open(data / "test-run.log", "a")
    return subprocess.Popen([sys.executable, str(ROOT / "window_notes.py")], env=env, stdout=log, stderr=log)


def scenario(name):
    """Decorator: run a scenario, record pass/fail, never stop the run."""
    def run(fn):
        start = time.time()
        try:
            detail = fn()
            passed = detail is not False and not (isinstance(detail, str) and detail.startswith("FAIL"))
        except Exception as e:
            passed, detail = False, f"exception: {e!r}\n{traceback.format_exc(limit=3)}"
        if app.poll() is not None and name not in STOPS_APP:
            passed, detail = False, f"app died (exit {app.returncode}); {detail}"
        results.append((name, passed, f"{detail} ({time.time() - start:.1f}s)"))
        print(("PASS " if passed else "FAIL ") + name + ": " + str(detail), flush=True)
    return run


def expected_corner(win):
    """Where the box should sit: top-right of the window, below its title bar."""
    s = B.scale(win.id)
    left, top, right, _ = win.rect()
    return right - int(16 * s), top + int(B.TITLE_BAR * s)


def offset_error(win):
    want_right, want_top = expected_corner(win)
    left, top, right, _ = box_rect()
    return max(abs(right - want_right), abs(top - want_top))


# ---- the run -------------------------------------------------------------------
TARGET_NOTE.write_text(TARGET_TEXT, encoding="utf-8")
app = start_app()
errors_before = 0


@scenario("app starts and reports")
def _():
    s = wait_for(lambda: state().get("box_id"), timeout=15)
    return f"box id {s}" if s else "FAIL: no state report within 15 s"


target, other = Window("WN Target"), Window("WN Other")
time.sleep(4.5)                                   # let the start-up message finish


@scenario("note appears on its window")
def _():
    target.do("activate")
    if not wait_for(showing):
        return f"FAIL: not showing; state={state()}"
    err = offset_error(target)
    return f"in place (off by {err}px)" if err <= 3 else f"FAIL: box off by {err}px; box={box_rect()} win={target.rect()}"


@scenario("note follows the window")
def _():
    """After each move, how long until the box is back in its corner? Fails over 1 s."""
    times = []
    for i in range(1, 16):
        target.do(f"move {150 + i * 6} {120 + i * 4}")
        moved = time.time()
        if not wait_for(lambda: offset_error(target) <= 3, timeout=1.0, every=0.01):
            return f"FAIL: after move {i} the box was still {offset_error(target)}px off after 1 s"
        times.append(time.time() - moved)
    times.sort()
    return f"caught up after every move: typical {times[len(times) // 2] * 1000:.0f} ms, slowest {times[-1] * 1000:.0f} ms"


@scenario("hidden when another window is active")
def _():
    other.do("activate")
    return "hidden" if wait_for(hidden) else f"FAIL: still visible; state={state()}"


@scenario("back when the window is active again")
def _():
    target.do("activate")
    return "showing" if wait_for(showing) else "FAIL: didn't come back"


@scenario("title change hides and shows it")
def _():
    target.do("title Something else")
    if not wait_for(hidden):
        return "FAIL: still showing after the title stopped matching"
    target.do("title WN Target again")
    return "follows the title" if wait_for(showing) else "FAIL: didn't come back after the title matched again"


@scenario("hidden when minimized, back when restored")
def _():
    target.do("minimize")
    if not wait_for(hidden):
        return "FAIL: still visible while minimized"
    target.do("restore")
    return "ok" if wait_for(showing) else "FAIL: didn't come back after restore"


@scenario("note edited in place is redrawn")
def _():
    before = state()["size"][1]
    TARGET_NOTE.write_text(TARGET_TEXT + "\n".join(f"- line {i}" for i in range(6)) + "\n", encoding="utf-8")
    grew = wait_for(lambda: state()["size"][1] > before and showing(), timeout=5)
    return f"height {before} -> {state()['size'][1]}" if grew else "FAIL: box didn't change after editing the note"


@scenario("ticking a checkbox saves it and keeps focus")
def _():
    target.do("activate")
    wait_for(showing)
    time.sleep(0.5)
    click_in_box("checkbox")
    saved = wait_for(lambda: "- [x] Tick me" in TARGET_NOTE.read_text(encoding="utf-8"), timeout=3)
    time.sleep(0.3)
    front = B.foreground()
    kept = front and front["title"].startswith("WN Target")
    if not saved:
        return "FAIL: the checkbox wasn't saved"
    return "saved, focus kept" if kept else f"FAIL: saved, but focus moved to {front and front['title']!r}"


@scenario("hotkey creates a note for the active window")
def _():
    target.do("activate")
    time.sleep(0.5)
    before = set(NOTES.glob("*.md"))
    press_hotkey()
    new = wait_for(lambda: set(NOTES.glob("*.md")) - before, timeout=4)
    if not new:
        return "FAIL: no note was created (the hotkey may be blocked here)"
    note = new.pop()
    ok = "WN Target again" in note.read_text(encoding="utf-8")
    note.unlink()
    return f"created {note.name}" if ok else f"FAIL: note doesn't name the window: {note.read_text()!r}"


@scenario("damaged files in the notes folder are skipped")
def _():
    target.do("activate")
    time.sleep(2)                    # let any message from the previous step (opening Obsidian) arrive
    target.do("activate")            # ...or a "how do you want to open this?" dialog it caused
    wait_for(showing)
    errors = state()["error_count"]
    (NOTES / "binary.md").write_bytes(b"\x00\xff\x89PNG garbage")
    (NOTES / "latin1.md").write_bytes("---\nattach-title: \"WN Target*\"\n---\ncaf\xe9\n".encode("latin-1"))
    (NOTES / "unclosed.md").write_text("---\nattach-title: \"WN Target*\"\n", encoding="utf-8")
    (NOTES / "folder.md").mkdir()
    time.sleep(3)
    s = state()
    new = s["errors"][len(s["errors"]) - (s["error_count"] - errors):] if s["error_count"] > errors else []
    if new:
        return f"FAIL: errors logged: {new}"
    return "skipped quietly" if showing() and s["notes"] == ["Target note"] else f"FAIL: {why()}"


@scenario("too many notes still fit on screen")
def _():
    target.do("activate")
    for i in range(12):
        (NOTES / f"Many {i:02}.md").write_text('---\nattach-title: "WN Target*"\n---\n'
                                               + "\n".join(f"- [ ] task {j}" for j in range(20)), encoding="utf-8")
    wait_for(lambda: len(state()["notes"]) == 13, timeout=5)
    time.sleep(0.5)
    _, top, _, bottom = box_rect()
    sl, st, sr, sb = B.work_area(target.id)
    for f in NOTES.glob("Many *.md"):
        f.unlink()
    return f"box {top}-{bottom} within screen {st}-{sb}" if top >= st and bottom <= sb else \
        f"FAIL: box {top}-{bottom} runs past the screen {st}-{sb}"


@scenario("deleting the note hides it")
def _():
    target.do("activate")
    wait_for(lambda: len(state()["notes"]) == 1, timeout=5)
    TARGET_NOTE.unlink()
    return "hidden" if wait_for(hidden, timeout=5) else f"FAIL: still showing a deleted note: {why()}"


@scenario("notes folder deleted and recreated while running")
def _():
    target.do("activate")
    TARGET_NOTE.write_text(TARGET_TEXT, encoding="utf-8")
    wait_for(showing, timeout=5)
    shutil.rmtree(NOTES)
    if not wait_for(hidden, timeout=5):
        return f"FAIL: still showing after the folder was deleted: {why()}"
    NOTES.mkdir()
    TARGET_NOTE.write_text(TARGET_TEXT, encoding="utf-8")
    return "recovered" if wait_for(showing, timeout=6) else f"FAIL: didn't pick the note up again: {why()}"


@scenario("closing the window hides the note")
def _():
    global target
    target.close()
    ok = wait_for(hidden, timeout=4)
    target = Window("WN Target")
    return "hidden" if ok else "FAIL: box left behind after its window closed"


@scenario("rapid switching (100 times)")
def _():
    errors = state()["error_count"]
    for i in range(50):
        target.do("activate")
        other.do("activate")
    target.do("activate")
    ok = wait_for(showing, timeout=4)
    new_errors = state()["error_count"] - errors
    return f"FAIL: {new_errors} errors: {state()['errors']}" if new_errors else \
        ("settled correctly" if ok else "FAIL: didn't settle on the target")


@scenario("Edit without Obsidian doesn't break anything")
def _():
    target.do("activate")
    wait_for(showing)
    time.sleep(0.5)
    errors = state()["error_count"]
    where = click_in_box("edit")
    time.sleep(3)
    s = state()
    unexpected = [e for e in s["errors"] if e.startswith("Error:")]
    if unexpected:
        return f"FAIL: unexpected error {unexpected}"
    if s["error_count"] > errors:
        return f"handled: {s['errors'][-1][:90]}"
    log_tail = (DATA / "test-run.log").read_text()[-300:]
    # Nothing logged. That's only right if something on this machine opens obsidian:// links,
    # which the log shows as no "Couldn't open" line.
    return f"FAIL: no sign the click reached Edit. {where}. Log end: {log_tail!r}" \
        if "Couldn't open" in log_tail or not shutil.which("xdg-open") and sys.platform.startswith("linux") \
        else "opened (an app handles obsidian:// links here)"


@scenario("second copy exits")
def _():
    second = start_app()
    try:
        code = second.wait(timeout=15)
    except subprocess.TimeoutExpired:
        second.kill()
        return "FAIL: the second copy kept running"
    return f"exited ({code}); first copy still running: {app.poll() is None}"


# ---- runs with bad settings (the main app keeps running meanwhile) ------------------
@scenario("damaged config.toml doesn't stop it")
def _():
    data = WORK / "data-bad-config"
    data.mkdir()
    (data / "config.toml").write_bytes(b'vault = "unclosed\n\x00\xff')
    # a second copy would exit at once, so this needs the main copy stopped first
    app.terminate()
    app.wait(timeout=10)
    STATE.unlink(missing_ok=True)
    trial = start_app(data=data)
    ok = wait_for(lambda: state().get("box_id"), timeout=15)
    errors = state().get("errors", [])
    trial.terminate()
    trial.wait(timeout=10)
    if not ok:
        return f"FAIL: didn't start; log: {(data / 'test-run.log').read_text()[-400:]}"
    return "started with defaults and logged the problem" if any("config.toml" in e for e in errors) else \
        f"FAIL: started but didn't report the damaged file: {errors}"


@scenario("missing vault folder: clear exit, nothing created")
def _():
    missing = WORK / "no such vault"
    trial = start_app({"WN_VAULT": str(missing)}, data=WORK / "data")
    try:
        code = trial.wait(timeout=15)
    except subprocess.TimeoutExpired:
        trial.kill()
        return "FAIL: kept running with a missing vault"
    return "FAIL: created the missing vault folder" if missing.exists() else f"exited with code {code}"


# The bad-settings runs stopped the main app on purpose; restart it so the check in
# scenario() doesn't count that as a crash, then shut everything down.
app = start_app()
time.sleep(1)
for w in (target, other):
    w.close()
app.terminate()

time.sleep(1)
log_text = "".join(p.read_text(errors="replace") for p in WORK.glob("data*/test-run.log"))
bad = [line for line in log_text.splitlines()
       if any(word in line for word in ("Traceback", "Exception in Tkinter", "X protocol error", "Fatal Python"))]
results.append(("app log has no crashes or stray errors", not bad, f"{len(bad)} bad lines: {bad[:5]}" if bad else "clean"))
print(("PASS " if not bad else "FAIL ") + results[-1][0] + ": " + results[-1][2], flush=True)

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)} of {len(results)} scenarios passed on {B.NAME}.")
for name, _, detail in failed:
    print(f"  FAILED {name}: {detail}")
print("\nApp log (last part):\n" + (DATA / "test-run.log").read_text()[-3000:])
sys.exit(1 if failed else 0)
