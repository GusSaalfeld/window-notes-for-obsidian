"""Reading, matching and editing note files. No UI and no operating-system calls here,
so everything in this file works the same everywhere and is easy to test.

A note is a Markdown file whose front matter attaches it to windows:

    ---
    attach-app: "excel"          # program name (".exe" optional)
    attach-title: "*Budget*"     # window title
    attach-url: "github.com/*"   # browser tab address
    note-color: "blue"           # optional
    ---
    Body text...
"""
import fnmatch
import re
import urllib.parse

ATTACH_KEYS = ("attach-app", "attach-title", "attach-url")


# ---- reading --------------------------------------------------------------
def _unquote(value):
    """'"text"' -> 'text', undoing the escapes yaml_str() adds."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def _front_matter_end(rows):
    """Index of the closing '---' line, or None if the file has no front matter."""
    if not rows or rows[0].strip() != "---":
        return None
    return next((i for i in range(1, len(rows)) if rows[i].strip() == "---"), None)


def parse_note(path):
    """Returns a dict for an attached note, or None if the file isn't one:
        path, name, app, title, url, color,
        lines: [(line number in the file, text)] of the body, used to tick checkboxes.
    Only the few keys we use are read, so we don't need a YAML library."""
    try:
        text = path.read_text(encoding="utf-8-sig")   # -sig: ignore a byte-order mark some editors add
    except (OSError, UnicodeDecodeError):
        return None
    rows = text.split("\n")
    end = _front_matter_end(rows)
    if end is None:
        return None

    props = {}
    for row in rows[1:end]:
        m = re.match(r"^(attach-app|attach-title|attach-url|note-color)\s*:\s*(.*?)\s*$", row)
        if m:
            props[m.group(1)] = _unquote(m.group(2))
    if not any(props.get(k) for k in ATTACH_KEYS):
        return None

    lines = [(i, rows[i].rstrip("\r")) for i in range(end + 1, len(rows))]
    while lines and not lines[0][1].strip():    # trim blank lines at both ends
        lines.pop(0)
    while lines and not lines[-1][1].strip():
        lines.pop()
    return {"path": path, "name": path.stem, "lines": lines,
            "app": props.get("attach-app", ""), "title": props.get("attach-title", ""),
            "url": props.get("attach-url", ""), "color": props.get("note-color", "")}


# ---- matching -------------------------------------------------------------
def app_key(name):
    """'Excel.EXE' -> 'excel', so rules work with or without '.exe'."""
    return name.lower().removesuffix(".exe")


def norm_url(url):
    """'https://www.GitHub.com/x/' -> 'github.com/x'"""
    url = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url.strip().lower())
    return re.sub(r"^www\.", "", url).rstrip("/")


def matches(note, win):
    """True if every attach- rule the note has fits the window.
    `win` has app, title and url (url is '' when unknown)."""
    if note["app"] and not fnmatch.fnmatchcase(app_key(win["app"]), app_key(note["app"])):
        return False
    if note["title"] and not fnmatch.fnmatchcase(win["title"].lower(), note["title"].lower()):
        return False
    if note["url"]:
        url, rule = norm_url(win.get("url", "")), norm_url(note["url"])
        # "+ '/'" lets "github.com/*" match the bare "github.com" too
        if not url or not (fnmatch.fnmatchcase(url, rule) or fnmatch.fnmatchcase(url + "/", rule)):
            return False
    return True


# ---- writing --------------------------------------------------------------
def yaml_str(value):
    """A double-quoted YAML string; quoting keeps * and : from meaning something else."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def new_note_text(win):
    """Contents for a note attached to `win`: the URL for a browser tab, else app + title."""
    props = [f"attach-app: {yaml_str(win['app'])}"]
    if win.get("url"):
        page = re.split(r"[?#]", norm_url(win["url"]))[0].rstrip("/")
        props.append(f"attach-url: {yaml_str(page + '*')}")   # this page, any ?query, and pages under it
        hint = 'Change attach-url to "site.com/*" to show it on the whole site.'
    else:
        title = re.sub(r"[*?\[\]]", "?", win["title"])           # wildcard characters would change the meaning
        props.append(f"attach-title: {yaml_str(title)}")
        hint = 'Use * in attach-title (for example "*Budget*"), or delete it to show this on every window of the app.'
    return "---\n" + "\n".join(props) + "\n---\n\nWrite your note here.\n\n" + hint + "\n"


def note_filename(title):
    """A safe file name (without .md) from a window title."""
    name = re.sub(r'[<>:"/\\|?*#^\[\]]+', " ", title or "").strip()[:60].strip()
    return name or "Window note"


def _rewrite(path, change):
    """Read a note, let `change(rows)` edit its lines in place, and save it,
    keeping the file's own line endings."""
    with open(path, encoding="utf-8-sig", newline="") as f:   # newline="": keep \r\n as it is
        rows = f.read().split("\n")
    change(rows)
    path.write_text("\n".join(rows), encoding="utf-8", newline="")


def set_property(path, key, value):
    """Set one front-matter property; an empty value removes it."""
    def change(rows):
        end = _front_matter_end(rows)
        eol = "\r" if rows[0].endswith("\r") else ""
        for i in range(end - 1, 0, -1):                      # drop the old line(s)
            if re.match(rf"^{re.escape(key)}\s*:", rows[i]):
                del rows[i]
                end -= 1
        if value:
            rows.insert(end, f"{key}: {yaml_str(value)}{eol}")
    _rewrite(path, change)


def toggle_checkbox(path, lineno):
    """Flip '- [ ]' <-> '- [x]' on one line of the file."""
    def change(rows):
        m = re.match(r"^(\s*[-*+] \[)([ xX])(\].*)$", rows[lineno], re.S) if lineno < len(rows) else None
        if m:
            rows[lineno] = m.group(1) + (" " if m.group(2) in "xX" else "x") + m.group(3)
    _rewrite(path, change)


# ---- displaying -----------------------------------------------------------
_INLINE = [
    (re.compile(r"\[\[([^\]|]+)\|([^\]]+)\]\]"), r"\2"),   # [[note|alias]] -> alias
    (re.compile(r"\[\[([^\]]+)\]\]"), r"\1"),              # [[note]]       -> note
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),         # [text](url)    -> text
    (re.compile(r"(\*\*|__|`|==|~~)"), ""),                # bold, code, highlight, strikethrough markers
]


def plain(text):
    """Markdown inline formatting -> readable plain text."""
    for pattern, replacement in _INLINE:
        text = pattern.sub(replacement, text)
    return text


_LINK = re.compile(r"\[\[([^\]|#]+)[^\]]*\]\]"     # [[note]], [[note#heading|alias]]
                   r"|\[[^\]]+\]\(([^)\s]+)\)"      # [text](target)
                   r"|(https?://[^\s<>)\]]+)")      # bare web address


def first_link(text):
    """('note', name) or ('url', address) for the first link in a line, else None."""
    m = _LINK.search(text)
    if not m:
        return None
    if m.group(1):
        return ("note", m.group(1).strip())
    target = m.group(2) or m.group(3).rstrip(".,;:!?")      # "see https://x.com." -> no final period
    if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
        return ("url", target)
    return ("note", urllib.parse.unquote(target).split("#")[0].removesuffix(".md"))   # [text](Other%20note.md)


def classify(line):
    """How to show one body line: (kind, text, extra)
        kind: 'blank', 'heading', 'checkbox' (extra = done?), 'bullet' (extra = marker), 'text'
    Quotes and Obsidian callouts ('> [!tip] Title') are unwrapped first; extra indent is
    returned separately by indent_of()."""
    s = line.strip()
    if s.startswith(">"):
        s = re.sub(r"^(>\s?)+", "", s).strip()
        callout = re.match(r"^\[!(\w+)\][+-]?\s*(.*)$", s)
        if callout:
            return ("heading", callout.group(2) or callout.group(1).capitalize(), None)
    if not s:
        return ("blank", "", None)
    if m := re.match(r"^[-*+] \[([ xX])\] ?(.*)$", s):
        return ("checkbox", m.group(2), m.group(1) in "xX")
    if m := re.match(r"^#{1,6}\s+(.*)$", s):
        return ("heading", m.group(1), None)
    if m := re.match(r"^([-*+]|\d+[.)])\s+(.*)$", s):
        return ("bullet", m.group(2), "•" if m.group(1) in "-*+" else m.group(1))
    return ("text", s, None)


def indent_of(line):
    """Pixels to indent a line: nested list items, and a little for quotes."""
    px = (len(line) - len(line.lstrip())) // 2 * 14
    return px + (8 if line.lstrip().startswith(">") else 0)


# ---- colors ---------------------------------------------------------------
PALETTES = {"yellow": "#fff4b8", "orange": "#ffe2c2", "pink": "#ffdbe7", "purple": "#e9defc",
            "blue": "#d9ebff", "green": "#d9f2d9", "gray": "#ececec", "dark": "#2d2d30"}


def palette(color):
    """A color name or '#rrggbb' -> background plus matching text, muted, done and border
    colors. Light backgrounds get dark text and vice versa; unknown colors fall back to yellow."""
    bg = PALETTES.get((color or "yellow").strip().lower(), (color or "").strip())
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", bg):
        bg = PALETTES["yellow"]
    rgb = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    light = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2] > 140     # perceived brightness
    ink = (40, 34, 10) if light else (245, 245, 240)

    def mix(amount):   # a color `amount` of the way from the background to the text color
        return "#%02x%02x%02x" % tuple(round(c + (t - c) * amount) for c, t in zip(rgb, ink))

    return {"bg": bg, "fg": mix(1.0), "muted": mix(0.65), "done": mix(0.45), "border": mix(0.22)}


def obsidian_uri(vault_name, file):
    """Link that opens a note in Obsidian. `file` is a note name or a path from the vault root."""
    return ("obsidian://open?vault=" + urllib.parse.quote(vault_name)
            + "&file=" + urllib.parse.quote(file))
