# Window Notes for Obsidian

Sticky notes that appear on the window they belong to, stored as plain Markdown in your Obsidian vault.

Attach a note to an app, a document's window title, or a website. When you switch to that window, the note appears in its top-right corner and follows it around. Switch away and it hides. The notes are ordinary `.md` files, so you write and search them in Obsidian (or any editor) like the rest of your notes.

> **Status:** early version, Windows only. Feedback and issues are welcome.

## Why

Tools like Notezilla can attach notes to windows, but they keep your notes in their own database and are paid. Window Notes keeps each note as a Markdown file with a couple of properties at the top, so nothing is locked in.

## Install

1. Install [Python 3.11 or newer](https://www.python.org/downloads/windows/). Tick "Add python.exe to PATH" in the installer.
2. Download this repository (green **Code** button → **Download ZIP**) and unzip it anywhere.
3. Optional, for website notes: open a terminal in the folder and run `pip install -r requirements.txt`.
4. Double-click **Window Notes.bat**. On first run it asks for your Obsidian vault folder.

A short "Window Notes is running" message confirms it started. After that it stays out of sight until you're in a window that has a note.

To start it automatically, right-click any note and choose **Start with Windows**.

## Attaching a note

**Quickest:** click into any window and press **Ctrl+Alt+N**. A new note is created in your vault's `Window notes` folder, attached to that window, and opened in Obsidian.

**By hand:** put these properties at the top of any note in the `Window notes` folder (or a subfolder):

```yaml
---
attach-app: "excel.exe"       # the program
attach-title: "*Budget*"      # the window title
attach-url: "github.com/*"    # the browser tab's address
---
```

Use any combination; a note shows when all the properties it has match. Matching ignores upper/lower case, `*` matches anything, `?` matches one character, and `.exe` can be left off the program name.

| You want the note on… | Use |
|---|---|
| every window of an app | `attach-app: "code"` |
| one document | `attach-title: "Budget 2026.xlsx*"` |
| a whole website | `attach-url: "github.com/*"` |
| one page and anything under it | `attach-url: "github.com/me/repo*"` |

Website matching works in Chrome, Edge, Firefox, Brave, Vivaldi and Opera, and needs the optional `comtypes` package.

## Using the note box

- **Click a note's title** to collapse it to one line.
- **Click a checkbox** (`- [ ]`) to tick it. The change is saved to the file.
- **Click a link** (underlined): a `[[note]]` opens in Obsidian, a web link opens in your browser.
- **Edit** opens the note in Obsidian.
- **×** hides the note for that window until you restart Window Notes.
- **Right-click** for: color, opacity, open the notes folder, reload, Start with Windows, quit.

The box never takes keyboard focus, so you can keep typing in your app. Headings, bullets, numbered lists, checkboxes, links, quotes and callouts (`> [!tip] Title`) are shown in a simple readable form; edit the text itself in Obsidian.

### Color and opacity

- **Color** is set per note: right-click it → **Color**. It's saved in the note as `note-color: "blue"`, so you can also type it yourself, including any `"#rrggbb"` color. Choices: yellow (default), orange, pink, purple, blue, green, gray, dark.
- **Opacity** applies to all notes: right-click → **Opacity**. A see-through note turns solid while your mouse is over it, so it's always easy to read. It's saved in `config.toml`.

## Settings

`config.toml` sits next to `window_notes.py` and is created on first run. See [config.example.toml](config.example.toml):

- `vault`: your Obsidian vault, or any folder of Markdown notes
- `folder`: the folder inside it for attached notes (default `Window notes`)
- `hotkey`: the letter used with Ctrl+Alt for a new note (default `N`)
- `opacity`: how solid notes are when the mouse isn't over them, from `0.2` to `1.0` (default `1.0`)

The vault doesn't have to be an Obsidian vault; only the **Edit** button needs Obsidian.

## Troubleshooting

- **Nothing seems to happen:** that's normal until you're in a window with a note. Open Notepad after copying [examples/Example note.md](examples/Example%20note.md) into your `Window notes` folder.
- **Errors:** run **Window Notes (debug).bat** to see messages, or read `window-notes.log`.
- **Ctrl+Alt+N does nothing:** another app may already use it. Change `hotkey` in `config.toml`.
- **A note doesn't show on a website:** run `pip install comtypes`, and check the address with `*` at the end.

## Limits and plans

- Windows only for now. macOS and Linux are possible (Linux on Wayland needs a different approach for each desktop).
- The note box can't be moved or resized yet, and you can't edit text in it.
- Collapsed and hidden notes reset when you restart.

## License

[MIT](LICENSE)
