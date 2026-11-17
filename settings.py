"""Reading and writing config.toml and state.json. No UI and no OS calls, so it's testable
anywhere. Every function here survives missing, damaged or hand-edited files: a bad value
falls back to its default and is reported, it never stops the app from starting."""
import json
import tomllib
from pathlib import Path

DEFAULTS = {"vault": "", "folder": "Window notes", "hotkey": "N", "opacity": 1.0}


def read_config(path):
    """Returns (settings, problems). `problems` is a list of messages about values that were
    ignored, e.g. a damaged file or an opacity of "lots". Missing settings get defaults."""
    cfg, problems = dict(DEFAULTS), []
    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return cfg, problems
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        return cfg, [f"Couldn't read {Path(path).name} ({e}); using default settings."]

    if isinstance(raw.get("vault"), str):
        cfg["vault"] = raw["vault"]
    if isinstance(raw.get("folder"), str) and raw["folder"].strip() and ".." not in Path(raw["folder"]).parts:
        cfg["folder"] = raw["folder"].strip()
    elif "folder" in raw:
        problems.append(f"Ignored folder = {raw['folder']!r}: it must be a folder name inside the vault.")
    hotkey = str(raw.get("hotkey", "N")).strip().upper()
    if len(hotkey) == 1 and hotkey.isascii() and hotkey.isalpha():
        cfg["hotkey"] = hotkey
    else:
        problems.append(f"Ignored hotkey = {raw.get('hotkey')!r}: it must be one letter, like \"N\".")
    try:
        cfg["opacity"] = min(1.0, max(0.2, float(raw.get("opacity", 1.0))))
    except (TypeError, ValueError):
        problems.append(f"Ignored opacity = {raw.get('opacity')!r}: it must be a number from 0.2 to 1.0.")
    return cfg, problems


def save_value(path, key, value):
    """Set one `key = value` line in config.toml, keeping comments and other lines.
    json.dumps gives a valid TOML string or number (it escapes quotes and backslashes)."""
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        lines = ["# Window Notes settings"]
    new = f"{key} = {json.dumps(value)}"
    for i, line in enumerate(lines):
        if line.split("=")[0].strip() == key:
            lines[i] = new
            break
    else:
        lines.append(new)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_folded(path, vault):
    """Note paths that were folded last time. state.json stores them relative to the vault."""
    try:
        folded = json.loads(Path(path).read_text(encoding="utf-8")).get("folded", [])
        return {Path(vault) / p for p in folded if isinstance(p, str)}
    except (OSError, ValueError, AttributeError, TypeError):   # missing, not JSON, or the wrong shape
        return set()


def save_folded(path, vault, folded):
    """Raises OSError if the file can't be written; the caller logs it."""
    vault = Path(vault)
    paths = sorted(p.relative_to(vault).as_posix() for p in folded if p.is_relative_to(vault))
    Path(path).write_text(json.dumps({"folded": paths}, indent=1), encoding="utf-8")
