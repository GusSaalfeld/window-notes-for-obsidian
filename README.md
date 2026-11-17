# Window Notes

<img src="docs/banner.png" alt="Window Notes for Obsidian: pin a note to any window. It appears when you open that window and disappears when you switch away. A spreadsheet window with the budget note pinned in its top-right corner." width="840">

Window Notes pins a note to the app or website it belongs to. Open your budget spreadsheet and your budget note appears in the corner of its window. Switch to another app and the note disappears.

Each note is an ordinary Markdown file in your Obsidian vault. You write it in Obsidian, and a line or two at the top of the file says which window it belongs to.

Window Notes runs on Windows and Linux (X11). This is an early version, so if something doesn't work, please [open an issue](../../issues).

## Download

Get the latest version from the [Releases](../../releases) page. Nothing else needs to be installed.

**Windows:** download `WindowNotes.exe` and double-click it. The program isn't signed, so Windows may warn that it "protected your PC". Click **More info**, then **Run anyway**.

**Linux:** download `window-notes-linux-x86_64.tar.gz`, then run:

```sh
tar -xzf window-notes-linux-x86_64.tar.gz
./window-notes &
```

The downloaded versions keep their settings in `%APPDATA%\Window Notes` on Windows and `~/.config/window-notes` on Linux.

### Run from source instead

You need Python 3.11 or newer.

- **Windows:** install Python from [python.org](https://www.python.org/) and tick **Add python.exe to PATH**. Download this repository and double-click `Window Notes.bat`. To attach notes to websites, also run `pip install -r requirements.txt`.
- **Linux:** run `sudo apt install python3-tk python3-xlib` (Ubuntu/Debian), download this repository, and run `python3 window_notes.py &` in its folder.

When run from source, Window Notes keeps its settings in `config.toml`, next to `window_notes.py`.

## Getting started

The first time Window Notes starts, it asks for your vault folder, then shows a small message in the corner of the screen to confirm it's running. From then on, you'll only see it in windows that have a note.

To start Window Notes when you log in, right-click any note and choose **Start with Windows** (on Linux, **Start at login**).

## Attaching a note

The quick way: go to the window, press **Ctrl+Alt+N**, and write the note in Obsidian. It's saved in the `Window notes` folder of your vault.

You can also attach any note in that folder by hand. Put these lines at the very top of the file:

```yaml
---
attach-app: "excel"
attach-title: "*Budget*"
---
```

A note can match a window in three ways, and can combine them:

| Line           | Matches                                   | Example                          |
| -------------- | ----------------------------------------- | -------------------------------- |
| `attach-app`   | The program                               | `"excel"`, `"code"`, `"firefox"` |
| `attach-title` | The window title                          | `"*Budget 2026*"`                |
| `attach-url`   | The browser tab's address (Windows only)  | `"github.com/*"`                 |

`*` matches any text, and matching ignores capitals. On Windows, the program name works with or without `.exe`. To find a program's name, press Ctrl+Alt+N in it: the new note will have the name filled in.

## Using a note

- **Click the title** to fold the note to a single line. It stays folded after a restart.
- **Click a checkbox** to tick it. The change is saved to the file.
- **Click an underlined line** to follow its link. `[[Other note]]` opens in Obsidian; web links open in your browser.
- **Edit** opens the note in Obsidian.
- **×** hides the note on that window. Hidden notes return when Window Notes restarts, so a note can never go missing for good.
- **Right-click** for color, opacity and other options.

A note never takes keyboard focus from the app you're typing in.

Headings, lists, checkboxes, links and callouts such as `> [!tip] Remember` are shown as plain, readable text. Notes longer than 20 lines show a link to open the rest in Obsidian.

**Color.** Right-click a note and pick one of eight colors. This adds a line such as `note-color: "blue"` to the note. You can also type this line yourself, including a hex color like `"#c8f7e1"`.

**Opacity.** Right-click and choose a level from 100% down to 30%. The setting applies to every note, and a note turns solid while your mouse is over it.

## Settings

Settings live in `config.toml`, in the settings folder described under [Download](#download). The file is created on first run, and `config.example.toml` explains each setting.

| Setting   | What it does                                                                 | Default        |
| --------- | ---------------------------------------------------------------------------- | -------------- |
| `vault`   | Your Obsidian vault, or any folder of Markdown files. Only the Edit button needs Obsidian. | —              |
| `folder`  | Where attached notes live inside the vault. Subfolders count too.            | `Window notes` |
| `hotkey`  | The letter used with Ctrl+Alt to create a new note.                          | `N`            |
| `opacity` | From `0.2` to `1.0`.                                                         | `1.0`          |

## Troubleshooting

**Nothing seems to happen.** That's normal until you're in a window that has a note. To see one, copy `examples/Example note.md` into your `Window notes` folder, then open Notepad (Windows).

**Ctrl+Alt+N does nothing.** Another program may already use that shortcut. Choose a different letter for `hotkey` in `config.toml`.

**A website note doesn't appear.** Run `pip install comtypes`, and check that the address in `attach-url` ends with `*`.

**You want to see error messages.** Read `window-notes.log` in the settings folder. When running from source on Windows, `Window Notes (debug).bat` shows errors as they happen; on Linux, start Window Notes from a terminal without the `&`.

## Limitations

- **Linux:** X11 only. In a Wayland session, notes work with apps that run through XWayland, but not with native Wayland apps. `attach-url` isn't supported yet, so match browsers by window title instead.
- **macOS:** not supported yet.
- Notes can't be moved or resized, and you can't edit text directly in a note.

## For developers

| File                 | What it does                                                         |
| -------------------- | -------------------------------------------------------------------- |
| `window_notes.py`    | Settings, the note window (Tkinter) and the main loop                |
| `notes.py`           | Reading, matching and editing note files. No UI and no OS calls.     |
| `backend_windows.py` | Finding windows, window events and the hotkey on Windows (ctypes)    |
| `backend_linux.py`   | The same for X11 (python-xlib)                                       |

Adding macOS support would mean writing one more `backend_*.py` with the same functions.

Run the tests with `python -m unittest`. GitHub runs them on Windows, Linux and macOS on every push. Pushing a tag such as `v0.3.0` builds the downloads and publishes a release. The banner image is `docs/banner.html`, captured at 1120×625 at double resolution.

## License

MIT
