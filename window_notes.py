"""Window Notes for Obsidian  -  https://github.com/GusSaalfeld/window-notes-for-obsidian  (MIT License)

Shows Markdown notes from a vault folder on top of the window they're attached to.
See README.md for how to attach notes; notes.py for the file format.

How the code is split:
    window_notes.py      settings, the note box (Tkinter) and the main loop   <- you are here
    notes.py             reading, matching and editing note files (no UI, no OS calls)
    backend_windows.py   everything that differs per operating system: which window is
    backend_linux.py     active, where it is, window events, the hotkey, start at login

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

if sys.platform == "win32":
    import backend_windows as B
elif sys.platform.startswith("linux"):
    import backend_linux as B
else:
    sys.exit("Window Notes runs on Windows and Linux for now.")

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.toml"

QUEUE_MS = 30          # how often the UI picks up events
POLL_MS = 250          # how often to re-check the active window (title changes)
RESCAN_MS = 2000       # how often to re-read the notes folder
BOX_WIDTH = 300        # note box width at 100% scaling
MAX_BODY_LINES = 20    # longer notes get a "N more lines" link


# ---- logging: pythonw has no console, so messages go to a file ---------------
if sys.stdout is None or sys.stderr is None:
    sys.stdout = sys.stderr = open(HERE / "window-notes.log", "a", encoding="utf-8", buffering=1)


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


# ---- settings (config.toml next to this file) ----------------------------------
def load_config():
    """Returns the settings dict. On first run, asks for the vault folder and saves it.
    Environment variables WN_VAULT and WN_FOLDER override the file (handy for testing)."""
    cfg = {}
    if CONFIG.exists():
        import tomllib
        with open(CONFIG, "rb") as f:
            cfg = tomllib.load(f)
    if not (os.environ.get("WN_VAULT") or cfg.get("vault")):
        from tkinter import filedialog
        picker = tk.Tk()
        picker.withdraw()
        vault = filedialog.askdirectory(title="Window Notes: choose your Obsidian vault (or any notes folder)")
        picker.destroy()
        if not vault:
            sys.exit("No notes folder chosen.")
        save_config_value("vault", vault)
        cfg["vault"] = vault
    try:
        opacity = min(1.0, max(0.2, float(cfg.get("opacity", 1.0))))
    except (TypeError, ValueError):
        opacity = 1.0
    return {
        "vault": Path(os.environ.get("WN_VAULT") or cfg["vault"]),
        "folder": os.environ.get("WN_FOLDER") or cfg.get("folder", "Window notes"),
        "hotkey": (str(cfg.get("hotkey") or "N")).upper()[:1],
        "opacity": opacity,
    }


def save_config_value(key, value):
    """Set one `key = value` line in config.toml, keeping comments and other lines.
    json.dumps gives a valid TOML string or number (it handles quotes and backslashes)."""
    lines = CONFIG.read_text(encoding="utf-8").splitlines() if CONFIG.exists() else ["# Window Notes settings"]
    new = f"{key} = {json.dumps(value)}"
    for i, line in enumerate(lines):
        if line.split("=")[0].strip() == key:
            lines[i] = new
            break
    else:
        lines.append(new)
    CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
        self.dir.mkdir(parents=True, exist_ok=True)

        self.notes, self.stamp = [], None   # parsed notes, and file names + times they came from
        self.target = None                  # the window the box belongs to: {id, app, title, pid, url}
        self.shown = None                   # what the box shows now; redraw when this changes
        self.size = (BOX_WIDTH, 50)         # box size in pixels, set by draw()
        self.dismissed = set()              # (window id, note path) hidden with × this session
        self.collapsed = set()              # note paths collapsed to their title
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
                    log(f"Ctrl+Alt+{self.hotkey} is already used by another app; the new-note hotkey is off.")
            self.ticks += 1
            if self.ticks % (POLL_MS // QUEUE_MS) == 0:
                update = True
            if self.ticks % (RESCAN_MS // QUEUE_MS) == 0:
                self.rescan()
            if update:
                self.update_box()
        except Exception as e:   # one bad note or window mustn't stop the app
            log("Error:", repr(e))
        self.root.after(QUEUE_MS, self.tick)

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
            self.draw(hits, B.scale(win["id"]))
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

    def welcome(self, seconds=4):
        """A short "running" message in the bottom-right corner of the screen."""
        s = B.scale()
        width = int(BOX_WIDTH * s)
        self.clear()
        card = tk.Frame(self.box, bg=self.BG, padx=12, pady=9)
        card.pack(fill="x")
        n = len(self.notes)
        tk.Label(card, text="Window Notes is running", bg=self.BG, fg=self.FG,
                 font=font(10, bold=True), anchor="w").pack(fill="x")
        tk.Label(card, text=f"{n} attached note{'' if n == 1 else 's'}. "
                            f"Press Ctrl+Alt+{self.hotkey} in any window to attach one.",
                 bg=self.BG, fg=self.MUTED, font=font(9), anchor="w", justify="left",
                 wraplength=width - 24).pack(fill="x", pady=(2, 0))
        self.fit(width)
        _, _, sr, sb = B.work_area()
        m = int(16 * s)
        self.overlay.move(sr - self.size[0] - m, sb - self.size[1] - m)
        self.overlay.show()
        self.shown = "welcome"
        self.toast_until = time.time() + seconds

    def draw(self, hits, s):
        """Build the box for these notes. `s` is the display scale (1.0 = 100%)."""
        width = int(BOX_WIDTH * s)
        self.clear(hits[0]["color"])
        for i, note in enumerate(hits):
            self.use_palette(note["color"])
            if i:
                tk.Frame(self.box, bg=self.BORDER, height=1).pack(fill="x")
            card = tk.Frame(self.box, bg=self.BG, padx=10, pady=7)
            card.pack(fill="x")
            is_open = note["path"] not in self.collapsed

            # header: ▾ title ............ Edit  ×
            head = tk.Frame(card, bg=self.BG)
            head.pack(fill="x")
            self.label(head, ("▾ " if is_open else "▸ ") + note["name"], font=font(10, bold=True),
                       wraplength=width - 90, on_click=lambda n=note: self.toggle_collapse(n)
                       ).pack(side="left", fill="x", expand=True)
            self.label(head, "×", fg=self.MUTED, font=font(12),
                       on_click=lambda n=note, w=self.target["id"]: self.dismiss(w, n)).pack(side="right")
            self.label(head, "Edit", fg=self.MUTED, font=font(9, "underline"),
                       on_click=lambda n=note: self.open_note(n["path"])).pack(side="right", padx=(0, 8))

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
                            on_click=lambda ln=lineno: self.check(note, ln))
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
            self.label(body, **opts).pack(fill="x", padx=(indent, 0))

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
            lab.bind("<Button-1>", lambda e: on_click())
        return lab

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
        menu.add_command(label="Open notes folder", command=lambda: B.open_target(self.dir))
        menu.add_command(label="Reload notes", command=lambda: self.rescan(force=True))
        menu.add_checkbutton(label=B.AUTOSTART_LABEL, variable=self.autostart, command=self.set_autostart)
        menu.add_separator()
        menu.add_command(label="Quit Window Notes", command=self.quit)
        self._menu = menu   # keep the menu and its Tk variables alive while it's open
        menu.tk_popup(event.x_root, event.y_root)

    # -- actions ------------------------------------------------------------------
    def open_note(self, path):
        B.open_target(N.obsidian_uri(self.vault.name, path.relative_to(self.vault).with_suffix("").as_posix()))

    def open_link(self, kind, target):
        B.open_target(target if kind == "url" else N.obsidian_uri(self.vault.name, target))

    def edit_file(self, change, what):
        """Run a file change, then redraw from the saved file."""
        try:
            change()
        except OSError as e:
            log(f"Couldn't save the {what}:", e)
        self.rescan(force=True)
        self.update_box()

    def check(self, note, lineno):
        self.edit_file(lambda: N.toggle_checkbox(note["path"], lineno), "checkbox")

    def set_color(self, note, name):
        self.edit_file(lambda: N.set_property(note["path"], "note-color", "" if name == "yellow" else name), "color")

    def set_opacity(self, value):
        self.opacity = value
        self.root.attributes("-alpha", value)
        save_config_value("opacity", value)

    def toggle_collapse(self, note):
        self.collapsed ^= {note["path"]}
        self.update_box()

    def dismiss(self, wid, note):
        self.dismissed.add((wid, note["path"]))
        self.update_box()

    def set_autostart(self):
        try:
            B.set_autostart(self.autostart.get(), Path(__file__).resolve())
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
        path.write_text(N.new_note_text(win), encoding="utf-8")
        log(f"New note: {path.name}")
        self.rescan(force=True)
        self.open_note(path)

    def quit(self):
        self.events.stop()
        self.root.destroy()


def main():
    if not B.single_instance():
        log("Window Notes is already running.")
        B.message("Window Notes is already running.\n\nNotes appear on the windows they're attached to. "
                  "Press Ctrl+Alt+N in any window to attach one. Right-click a note to quit.")
        return
    App(load_config()).root.mainloop()


if __name__ == "__main__":
    main()
