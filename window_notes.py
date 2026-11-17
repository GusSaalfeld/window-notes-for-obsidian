"""Window Notes for Obsidian  -  https://github.com/GusSaalfeld/window-notes-for-obsidian  (MIT License)

Shows Markdown notes from a vault folder on top of the window they're attached to.
See README.md for how to attach notes; notes.py for the file format.

How the code is split:
    window_notes.py      settings, the note box (Tkinter) and the main loop   <- you are here
    notes.py             reading, matching and editing note files (no UI, no OS calls)
    backend_windows.py   everything that differs per operating system: which window is
    backend_linux.py     active, where it is, window events, the hotkey, start at login.
    backend_macos.py     Each defines the same names; window_notes.py imports one as B.

How it runs:
    - The backend's Events thread hears "the active window changed", "the watched window
      moved" and the hotkey. Moves are handled right there (so the box follows a dragged
      window without lag); everything else is queued for the Tk thread.
    - The Tk thread checks the queue every QUEUE_MS, and every POLL_MS also re-checks the
      active window (catches title changes), and every RESCAN_MS re-reads the notes folder.
"""
import json
import os
import queue
import sys
import time
import tkinter as tk
from pathlib import Path

import notes as N
import settings as S

if sys.platform == "win32":
    import backend_windows as B
elif sys.platform == "darwin":
    import backend_macos as B
elif sys.platform.startswith("linux"):
    import backend_linux as B
else:
    sys.exit(f"Window Notes doesn't run on {sys.platform} yet.")

# Where settings, the log and saved state live. Run from source: next to this file.
# Run as a downloaded program (built with PyInstaller): in the user's settings folder,
# because the program itself may sit somewhere read-only like Program Files.
FROZEN = getattr(sys, "frozen", False)
HERE = Path(__file__).resolve().parent
DATA = Path(os.environ["WN_DATA"]) if os.environ.get("WN_DATA") else B.data_dir() if FROZEN else HERE  # WN_DATA: tests
DATA.mkdir(parents=True, exist_ok=True)
CONFIG = DATA / "config.toml"
STATE = DATA / "state.json"     # remembers which notes are folded

QUEUE_MS = 30          # how often the UI picks up events
POLL_MS = 250          # how often to re-check the active window (title changes)
RESCAN_MS = 2000       # how often to re-read the notes folder
BOX_WIDTH = 300        # note box width at 100% scaling
MAX_BODY_LINES = 20    # longer notes get a "N more lines" link


# ---- logging: pythonw has no console, so messages go to a file ---------------
if sys.stdout is None or sys.stderr is None:
    sys.stdout = sys.stderr = open(DATA / "window-notes.log", "a", encoding="utf-8", buffering=1)
ERRORS = []     # problems logged this run (reported to the test harness)


def log(*parts, problem=False):
    text = " ".join(str(p) for p in parts)
    if problem:
        ERRORS.append(text)
    print(time.strftime("%H:%M:%S"), text, flush=True)


# ---- settings --------------------------------------------------------------------
def ask_for_vault(reason=""):
    """Folder picker for the vault; returns a Path, or None if cancelled."""
    from tkinter import filedialog, messagebox
    picker = tk.Tk()
    picker.withdraw()
    if reason:
        messagebox.showinfo("Window Notes", reason, parent=picker)
    chosen = filedialog.askdirectory(title="Window Notes: choose your Obsidian vault (or any notes folder)",
                                     parent=picker)
    picker.destroy()
    return Path(chosen) if chosen else None


def load_config():
    """Settings from config.toml (see settings.py), with WN_VAULT / WN_FOLDER from the
    environment winning (handy for testing). Asks for the vault on first run, or when the
    saved vault folder no longer exists, rather than quietly creating an empty one."""
    cfg, problems = S.read_config(CONFIG)
    for p in problems:
        log(p, problem=True)
    if os.environ.get("WN_VAULT"):
        cfg["vault"] = os.environ["WN_VAULT"]
    if os.environ.get("WN_FOLDER"):
        cfg["folder"] = os.environ["WN_FOLDER"]

    vault = Path(cfg["vault"]) if cfg["vault"] else None
    if vault is None or not vault.is_dir():
        if os.environ.get("WN_VAULT"):          # tests and scripts: fail clearly, never prompt
            sys.exit(f"WN_VAULT folder doesn't exist: {vault}")
        reason = "" if vault is None else f"Your notes folder wasn't found:\n{vault}\n\nPlease choose it again."
        vault = ask_for_vault(reason)
        if vault is None:
            sys.exit("No notes folder chosen.")
        save_config_value("vault", str(vault))
    cfg["vault"] = vault
    return cfg


def save_config_value(key, value):
    try:
        S.save_value(CONFIG, key, value)
    except OSError as e:
        log(f"Couldn't save {key} to {CONFIG}:", e, problem=True)


def launch_command():
    """How to start this app again (for start at login): the program itself when it's a
    downloaded build, else Python with this script."""
    if FROZEN:
        return [sys.executable]
    python = Path(sys.executable)
    if sys.platform == "win32" and python.with_name("pythonw.exe").exists():
        python = python.with_name("pythonw.exe")    # pythonw: no console window
    return [str(python), str(Path(__file__).resolve())]


def font(size, *styles, bold=False):
    """A Tk font tuple using the platform's UI font."""
    if bold and B.FONT_BOLD:
        return (B.FONT_BOLD, size, *styles)
    return (B.FONT, size, *styles, *(("bold",) if bold else ()))


# ---- the app ------------------------------------------------------------------
class App:
    def __init__(self, cfg):
        self.vault, self.hotkey, self.opacity = cfg["vault"], cfg["hotkey"], cfg["opacity"]
        self.dir = self.vault / cfg["folder"]
        try:
            self.dir.mkdir(exist_ok=True)       # only the notes folder; the vault itself must exist
        except OSError as e:
            log(f"Couldn't create {self.dir}:", e, problem=True)
        self.report_path = os.environ.get("WN_TEST_STATE")   # test harness only: where to report state

        self.notes, self.stamp = [], None   # parsed notes, and file names + times they came from
        self.target = None                  # the window the box belongs to: {id, app, title, pid, url}
        self.shown = None                   # what the box shows now; redraw when this changes
        self.size = (BOX_WIDTH, 50)         # box size in pixels, set by draw()
        # Hidden (×) notes are tied to one open window, so they're forgotten on restart: a note
        # that stayed hidden for good would look lost. Folding keeps the title visible, so it's saved.
        self.dismissed = set()              # (window id, note path) hidden with × this session
        self.collapsed = S.load_folded(STATE, self.vault)   # note paths folded to their title
        self.toast_until = 0                # the start-up message stays until this time
        self.q = queue.Queue()              # messages from the Events thread
        self.urls = B.UrlReader(log) if B.SUPPORTS_URL else None

        # The note box: a borderless always-on-top window. root's background is the
        # 1px border; self.box inside it holds the notes.
        self.root = tk.Tk()
        self.root.title("Window Notes")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.geometry("+-3000+-3000")
        self.box = tk.Frame(self.root)
        self.box.pack(padx=1, pady=1, fill="both")
        self.overlay = B.Overlay(self.root)
        self.hide()

        # See-through when idle, solid while the mouse is over it.
        self.root.attributes("-alpha", self.opacity)
        self.root.bind("<Enter>", lambda e: self.root.attributes("-alpha", 1.0))
        self.root.bind("<Leave>", lambda e: self.root.attributes("-alpha", self.opacity))
        self.autostart = tk.BooleanVar(value=B.autostart_enabled())

        self.events = B.Events(self.hotkey,
                               on_foreground=lambda: self.q.put("foreground"),
                               on_moved=self.on_moved,
                               on_hotkey=lambda: self.q.put("new-note"),
                               on_hotkey_failed=lambda: self.q.put("hotkey-failed"))
        self.events.start()
        self.events.ready.wait(2)

        self.rescan()
        self.ticks = 0
        self.root.after(QUEUE_MS, self.tick)
        log(f"Window Notes running on {B.NAME}. Notes folder: {self.dir}")
        if B.NOTICE:
            log(B.NOTICE)
        self.welcome()

    # -- colors: draw() switches these per note -------------------------------------
    def use_palette(self, color):
        p = N.palette(color)
        self.BG, self.FG, self.MUTED, self.DONE, self.BORDER = p["bg"], p["fg"], p["muted"], p["done"], p["border"]

    # -- main loop ----------------------------------------------------------------
    def tick(self):
        try:
            update = False
            while not self.q.empty():
                msg = self.q.get()
                if msg == "foreground":
                    update = True
                elif msg == "new-note":
                    self.new_note()
                elif msg == "hotkey-failed":
                    log(f"Ctrl+Alt+{self.hotkey} is already used by another app; the new-note hotkey is off.",
                        problem=True)
            self.ticks += 1
            if self.ticks % (POLL_MS // QUEUE_MS) == 0:
                update = True
            if self.ticks % (RESCAN_MS // QUEUE_MS) == 0:
                self.rescan()
            if update:
                self.update_box()
        except Exception as e:   # one bad note or window mustn't stop the app
            log("Error:", repr(e), problem=True)
        self.report()
        self.root.after(QUEUE_MS, self.tick)

    def report(self):
        """Test harness only (WN_TEST_STATE is set): write what the app believes it's showing,
        so a test can compare it with what's really on screen."""
        if not self.report_path:
            return
        # positions relative to the box (winfo_x/y add up the offsets inside it); they're only
        # right once Tk has laid the labels out, so a change in them also triggers a new report
        clickable = []
        for lab, kind, line in getattr(self, "clickables", []):
            if lab.winfo_exists():
                x, y, w = lab.winfo_x(), lab.winfo_y(), lab.master
                while w is not self.root:
                    x, y, w = x + w.winfo_x(), y + w.winfo_y(), w.master
                clickable.append({"kind": kind, "line": line, "x": x, "y": y,
                                  "w": lab.winfo_width(), "h": lab.winfo_height()})
        key = (str(self.shown), len(ERRORS), json.dumps(clickable))
        if getattr(self, "_reported", None) == key:
            return
        self._reported = key
        state = {"box_id": self.overlay.id, "showing": self.shown is not None and self.shown != "welcome",
                 "target": self.target and self.target["title"], "notes": getattr(self, "drawn", []),
                 "size": self.size, "clickable": clickable, "errors": ERRORS[-10:], "error_count": len(ERRORS)}
        tmp = Path(self.report_path + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, self.report_path)        # atomic: the test never reads half a file

    def on_moved(self, wid):
        """Events thread: the watched window moved or was minimized. Only OS calls here,
        no Tk (Tk isn't thread-safe)."""
        t = self.target
        if not t or wid != t["id"] or self.shown is None:
            return
        if B.minimized(wid):
            self.overlay.hide(from_thread=True)
            self.shown = None
        else:
            self.overlay.move(*self.position(t), from_thread=True)

    def rescan(self, force=False):
        """Re-read the notes folder if any file was added, removed or changed."""
        try:
            files = sorted(p for p in self.dir.rglob("*.md") if ".trash" not in p.parts)
            stamp = [(str(f), f.stat().st_mtime_ns) for f in files]
        except OSError:
            return
        if force or stamp != self.stamp:
            self.stamp = stamp
            self.notes = [n for n in map(N.parse_note, files) if n]
            self.shown = None     # forces a redraw

    def current_window(self):
        """The active window, with its URL if it's a browser. Our own box doesn't count:
        clicking it keeps the note for the window you were in."""
        win = B.foreground(self.target)
        if win and self.overlay.is_own(win):
            return self.target
        if win:
            win["url"] = self.urls.url(win) if self.urls else ""
        return win

    def update_box(self):
        """Show the notes for the active window, or hide the box."""
        if time.time() < self.toast_until:
            return
        win = self.target = self.current_window()
        if not win or not B.exists(win["id"]) or B.minimized(win["id"]):
            return self.hide()
        self.events.watch(win)
        hits = [n for n in self.notes if N.matches(n, win) and (win["id"], n["path"]) not in self.dismissed]
        if not hits:
            return self.hide()
        key = (win["id"], [n["path"] for n in hits], self.stamp, sorted(map(str, self.collapsed)))
        if key != self.shown:
            s = B.scale(win["id"])
            _, top, _, bottom = B.work_area(win["id"])
            self.draw(hits, s, max_height=bottom - top - int(16 * s))
            self.shown = key
        self.overlay.move(*self.position(win))
        self.overlay.show()

    # -- placing the box ------------------------------------------------------------
    def position(self, win):
        """Top-right corner of the window, just below its title bar, kept on screen."""
        left, top, right, bottom = B.rect(win["id"])
        s = B.scale(win["id"])
        w, h = self.size
        x, y = right - w - int(16 * s), top + int(B.TITLE_BAR * s)
        sl, st, sr, sb = B.work_area(win["id"])
        m = int(8 * s)
        x = max(sl + m, min(max(x, left), sr - w - m))
        y = max(st + m, min(y, sb - h - m))
        return x, y

    def hide(self):
        self.overlay.hide()
        self.shown = None

    # -- drawing ------------------------------------------------------------------
    def clear(self, color=""):
        for child in self.box.winfo_children():
            child.destroy()
        self.use_palette(color)
        self.root.configure(bg=self.BORDER)
        self.box.configure(bg=self.BG)

    def fit(self, width):
        """Fix the box width and size the window to its contents."""
        tk.Frame(self.box, bg=self.BG, width=width, height=0).pack()
        self.root.update_idletasks()
        self.size = (width + 2, self.root.winfo_reqheight())
        self.root.geometry(f"{self.size[0]}x{self.size[1]}")

    def welcome(self):
        n = len(self.notes)
        self.toast("Window Notes is running", f"{n} attached note{'' if n == 1 else 's'}. "
                   f"Press Ctrl+Alt+{self.hotkey} in any window to attach one.")

    def toast(self, title, text, seconds=4):
        """A short message in the bottom-right corner of the screen. Used instead of a dialog
        box, which would take focus and stop the app until someone clicked it."""
        s = B.scale()
        width = int(BOX_WIDTH * s)
        self.clear()
        card = tk.Frame(self.box, bg=self.BG, padx=12, pady=9)
        card.pack(fill="x")
        tk.Label(card, text=title, bg=self.BG, fg=self.FG, font=font(10, bold=True), anchor="w").pack(fill="x")
        tk.Label(card, text=text, bg=self.BG, fg=self.MUTED, font=font(9), anchor="w", justify="left",
                 wraplength=width - 24).pack(fill="x", pady=(2, 0))
        self.fit(width)
        _, _, sr, sb = B.work_area()
        m = int(16 * s)
        self.overlay.move(sr - self.size[0] - m, sb - self.size[1] - m)
        self.overlay.show()
        self.shown = "welcome"
        self.toast_until = time.time() + seconds

    def draw(self, hits, s, max_height=10**6):
        """Build the box for these notes. `s` is the display scale (1.0 = 100%). If it comes
        out taller than `max_height`, the bottom notes are folded (on screen only) until it fits,
        so no note ends up off the bottom of the screen."""
        squeezed = set()
        while True:
            self._draw(hits, s, squeezed)
            still_open = [n["path"] for n in hits if n["path"] not in self.collapsed | squeezed]
            if self.size[1] <= max_height or not still_open:
                return
            squeezed.add(still_open[-1])

    def _draw(self, hits, s, squeezed):
        width = int(BOX_WIDTH * s)
        self.clear(hits[0]["color"])
        self.clickables, self.drawn = [], [n["name"] for n in hits]   # for the test report
        for i, note in enumerate(hits):
            self.use_palette(note["color"])
            if i:
                tk.Frame(self.box, bg=self.BORDER, height=1).pack(fill="x")
            card = tk.Frame(self.box, bg=self.BG, padx=10, pady=7)
            card.pack(fill="x")
            is_open = note["path"] not in self.collapsed | squeezed

            # header: ▾ title ............ Edit  ×
            head = tk.Frame(card, bg=self.BG)
            head.pack(fill="x")
            self.label(head, ("▾ " if is_open else "▸ ") + note["name"], font=font(10, bold=True),
                       wraplength=width - 90, on_click=lambda n=note: self.toggle_collapse(n)
                       ).pack(side="left", fill="x", expand=True)
            self.label(head, "×", fg=self.MUTED, font=font(12),
                       on_click=lambda n=note, w=self.target["id"]: self.dismiss(w, n)).pack(side="right")
            edit = self.label(head, "Edit", fg=self.MUTED, font=font(9, "underline"),
                              on_click=lambda n=note: self.open_note(n["path"]))
            edit.pack(side="right", padx=(0, 8))
            self.clickables.append((edit, "edit", None))

            if is_open:
                self.draw_body(card, note, width - 20)
            self.bind_menu(card, note)
        self.box.bind("<Button-3>", self.popup)
        self.fit(width)

    def draw_body(self, card, note, wrap):
        body = tk.Frame(card, bg=self.BG)
        body.pack(fill="x", pady=(3, 0))
        for lineno, line in note["lines"][:MAX_BODY_LINES]:
            kind, text, extra = N.classify(line)
            if kind == "blank":
                tk.Frame(body, bg=self.BG, height=5).pack(fill="x")
                continue
            indent = N.indent_of(line)
            opts = {"font": font(9), "wraplength": wrap - indent}
            link = N.first_link(text) if kind != "checkbox" else None
            if kind == "checkbox":          # click to tick; saved to the file
                opts.update(text=("☑  " if extra else "☐  ") + N.plain(text),
                            on_click=lambda ln=lineno, shown=line: self.check(note, ln, shown))
                if extra:
                    opts.update(fg=self.DONE, font=font(9, "overstrike"))
            elif kind == "heading":
                opts.update(text=N.plain(text), font=font(9, bold=True))
            elif kind == "bullet":
                opts.update(text=f"{extra}  " + N.plain(text))
            else:
                opts.update(text=N.plain(text))
            if link:                        # click opens [[note]] in Obsidian, web links in the browser
                opts.update(on_click=lambda l=link: self.open_link(*l))
                opts["font"] = opts["font"] + ("underline",)
            lab = self.label(body, **opts)
            lab.pack(fill="x", padx=(indent, 0))
            if kind == "checkbox":
                self.clickables.append((lab, "checkbox", lineno))

        hidden = len(note["lines"]) - MAX_BODY_LINES
        if hidden > 0:
            self.label(body, f"{hidden} more lines: open in Obsidian", fg=self.MUTED,
                       font=font(8, "underline"), on_click=lambda: self.open_note(note["path"])
                       ).pack(fill="x", pady=(3, 0))

    def label(self, parent, text="", fg=None, on_click=None, **kw):
        """A left-aligned label in the current colors; clickable if on_click is given."""
        lab = tk.Label(parent, text=text, bg=self.BG, fg=fg or self.FG, anchor="w", justify="left", **kw)
        if on_click:
            lab.configure(cursor="hand2")
            lab.bind("<Button-1>", lambda e: self.clicked(on_click))
        return lab

    def clicked(self, action):
        """Run a click action, then let the backend hand focus back to the window the
        note belongs to (only needed on macOS, where clicking activates our app)."""
        target = self.target
        try:
            action()
        except Exception as e:      # Tk would print this to a console nobody sees
            log("Error:", repr(e), problem=True)
        self.overlay.after_click(target)

    # -- right-click menu -----------------------------------------------------------
    def bind_menu(self, widget, note=None):
        widget.bind("<Button-3>", lambda e: self.popup(e, note))
        for child in widget.winfo_children():
            self.bind_menu(child, note)

    def popup(self, event, note=None):
        menu = tk.Menu(self.root, tearoff=0)
        if note:   # color of the note that was right-clicked
            colors = tk.Menu(menu, tearoff=0)
            self._color = tk.StringVar(value=(note["color"] or "yellow").lower())
            for name in N.PALETTES:
                colors.add_radiobutton(label=name.capitalize(), value=name, variable=self._color,
                                       command=lambda c=name: self.set_color(note, c))
            menu.add_cascade(label=f"Color of “{note['name'][:30]}”", menu=colors)
        levels = tk.Menu(menu, tearoff=0)
        self._opacity = tk.IntVar(value=round(self.opacity * 100))
        for pct in (100, 85, 70, 50, 30):
            levels.add_radiobutton(label=f"{pct}%" + ("  (solid)" if pct == 100 else ""), value=pct,
                                   variable=self._opacity, command=lambda p=pct: self.set_opacity(p / 100))
        menu.add_cascade(label="Opacity (all notes)", menu=levels)
        menu.add_separator()
        menu.add_command(label="Open notes folder", command=lambda: self.open(self.dir, "the notes folder"))
        menu.add_command(label="Reload notes", command=lambda: self.rescan(force=True))
        menu.add_checkbutton(label=B.AUTOSTART_LABEL, variable=self.autostart, command=self.set_autostart)
        menu.add_separator()
        menu.add_command(label="Quit Window Notes", command=self.quit)
        self._menu = menu   # keep the menu and its Tk variables alive while it's open
        target = self.target
        menu.tk_popup(event.x_root, event.y_root)
        self.overlay.after_click(target)

    # -- actions ------------------------------------------------------------------
    def open(self, target, what):
        """Open a link or the notes folder; say so in a toast if nothing can open it
        (for example obsidian:// links when Obsidian isn't installed)."""
        try:
            B.open_target(target)
        except OSError as e:
            log(f"Couldn't open {target}:", e, problem=True)
            self.toast(f"Couldn't open {what}", "Is Obsidian installed? Window Notes opens notes with "
                       "obsidian:// links." if str(target).startswith("obsidian:") else str(e))

    def open_note(self, path):
        self.open(N.obsidian_uri(self.vault.name, path.relative_to(self.vault).with_suffix("").as_posix()),
                  "the note")

    def open_link(self, kind, target):
        self.open(target if kind == "url" else N.obsidian_uri(self.vault.name, target), "the link")

    def edit_file(self, change, what):
        """Run a file change, then redraw from the saved file."""
        try:
            if change() is False:
                log(f"The note changed on disk, so the {what} wasn't saved. Showing the new version.")
        except OSError as e:
            log(f"Couldn't save the {what}:", e, problem=True)
            self.toast(f"Couldn't save the {what}", str(e))
        self.rescan(force=True)
        self.update_box()

    def check(self, note, lineno, shown):
        self.edit_file(lambda: N.toggle_checkbox(note["path"], lineno, expected=shown), "checkbox")

    def set_color(self, note, name):
        self.edit_file(lambda: N.set_property(note["path"], "note-color", "" if name == "yellow" else name), "color")

    def set_opacity(self, value):
        self.opacity = value
        self.root.attributes("-alpha", value)
        save_config_value("opacity", value)

    def toggle_collapse(self, note):
        self.collapsed ^= {note["path"]}
        try:
            S.save_folded(STATE, self.vault, self.collapsed)
        except OSError as e:
            log("Couldn't save folded notes:", e, problem=True)
        self.update_box()

    def dismiss(self, wid, note):
        self.dismissed.add((wid, note["path"]))
        self.update_box()

    def set_autostart(self):
        try:
            B.set_autostart(self.autostart.get(), launch_command())
        except OSError as e:
            log("Couldn't change start at login:", e)
        self.autostart.set(B.autostart_enabled())

    def new_note(self):
        """Hotkey: create a note attached to the active window and open it in Obsidian."""
        win = self.target or self.current_window()
        if not win:
            return
        name = N.note_filename(win["title"])
        path, i = self.dir / f"{name}.md", 2
        while path.exists():
            path, i = self.dir / f"{name} {i}.md", i + 1
        try:
            self.dir.mkdir(exist_ok=True)       # in case it was deleted while running
            path.write_text(N.new_note_text(win), encoding="utf-8")
        except OSError as e:
            log("Couldn't create the note:", e, problem=True)
            return self.toast("Couldn't create the note", str(e))
        log(f"New note: {path.name}")
        self.rescan(force=True)
        self.open_note(path)

    def quit(self):
        self.events.stop()
        self.root.destroy()


def main():
    if not B.single_instance():
        log("Window Notes is already running.")
        if not os.environ.get("WN_TEST_STATE"):     # a dialog would block automated tests
            B.message("Window Notes is already running.\n\nNotes appear on the windows they're attached to. "
                      "Press Ctrl+Alt+N in any window to attach one. Right-click a note to quit.")
        return
    App(load_config()).root.mainloop()


if __name__ == "__main__":
    main()
