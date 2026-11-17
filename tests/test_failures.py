"""Fail-state tests for notes.py and settings.py: damaged, odd or hand-edited files must never
crash the app or change the wrong thing. Run with:  python -m unittest"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import notes as N
import settings as S


def tmp(name="Test note.md", data=b""):
    """A file with these bytes in a fresh temporary folder."""
    path = Path(tempfile.mkdtemp()) / name
    path.write_bytes(data)
    return path


class DamagedNotes(unittest.TestCase):
    def test_files_that_are_not_attached_notes(self):
        for data in [b"", b"---", b"---\nattach-app: x\n", b"\x00\xff\xfe binary \x89PNG",
                     "---\nattach-app: caf\xe9\n---\n".encode("latin-1"),          # not UTF-8
                     b'---\nattach-app: ""\nattach-title: \n---\n', b"no front matter at all"]:
            with self.subTest(data=data[:20]):
                self.assertIsNone(N.parse_note(tmp(data=data)))

    def test_a_folder_named_like_a_note(self):
        folder = Path(tempfile.mkdtemp()) / "Looks like a note.md"
        folder.mkdir()
        self.assertIsNone(N.parse_note(folder))

    def test_missing_file(self):
        self.assertIsNone(N.parse_note(Path(tempfile.mkdtemp()) / "gone.md"))

    def test_huge_note_is_quick(self):
        body = ("- [ ] a fairly long line of text for a big note\n" * 100_000).encode()
        path = tmp(data=b'---\nattach-app: "x"\n---\n' + body)
        start = time.perf_counter()
        note = N.parse_note(path)
        self.assertLess(time.perf_counter() - start, 2.0)
        self.assertEqual(len(note["lines"]), 100_000)

    def test_odd_quoting_and_spacing(self):
        note = N.parse_note(tmp(data=b"---\n  attach-app:   'notepad'   \nattach-title:\"x\"\n---\n"))
        # an indented key is not a top-level property, so only attach-title counts
        self.assertEqual((note["app"], note["title"]), ("", "x"))


class Matching(unittest.TestCase):
    def test_brackets_are_plain_text(self):
        note = {"app": "", "title": "Docs [draft]*", "url": ""}
        self.assertTrue(N.matches(note, {"app": "x", "title": "Docs [draft] - Editor"}))
        self.assertFalse(N.matches(note, {"app": "x", "title": "Docs d - Editor"}))

    def test_unicode_titles(self):
        note = {"app": "", "title": "*ÉTÉ 🌞*", "url": ""}
        self.assertTrue(N.matches(note, {"app": "x", "title": "Plans été 🌞 2026"}))

    def test_new_note_matches_its_own_window(self):
        for title in ["What? *Really* [1] - App", "", "Ünïcødé 🎉", "C:\\path\\file.txt", 'Quote "this"']:
            with self.subTest(title=title):
                win = {"app": "app.exe", "title": title, "url": ""}
                note = N.parse_note(tmp(data=N.new_note_text(win).encode()))
                self.assertTrue(N.matches(note, win))

    def test_missing_url_never_matches_a_url_rule(self):
        self.assertFalse(N.matches({"app": "", "title": "", "url": "*"}, {"app": "x", "title": "y"}))


class FileNames(unittest.TestCase):
    def test_safe_names(self):
        cases = {"CON": "CON note", "nul": "nul note", "a/b\\c:d*e?f": "a b c d e f", "Trailing...": "Trailing",
                 "": "Window note", "???": "Window note", "tab\there\nnewline": "tab here newline"}
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(N.note_filename(title), expected)
        self.assertLessEqual(len(N.note_filename("x" * 500)), 60)

    def test_every_safe_name_can_be_created(self):
        folder = Path(tempfile.mkdtemp())
        for title in ["CON", "COM1", "x" * 300, "<>:\"/\\|?*", " . ", "emoji 🎉", "ends with dot."]:
            with self.subTest(title=title):
                path = folder / (N.note_filename(title) + ".md")
                path.write_text("x")
                self.assertTrue(path.exists())


class SafeEditing(unittest.TestCase):
    def test_checkbox_only_ticks_the_line_that_was_shown(self):
        path = tmp(data=b'---\nattach-app: "x"\n---\n- [ ] First\n- [ ] Second\n')
        # the user added a line above in Obsidian after the box was drawn
        path.write_bytes(b'---\nattach-app: "x"\n---\n- [ ] New\n- [ ] First\n- [ ] Second\n')
        self.assertFalse(N.toggle_checkbox(path, 3, expected="- [ ] First"))
        self.assertNotIn(b"[x]", path.read_bytes())
        self.assertTrue(N.toggle_checkbox(path, 4, expected="- [ ] First"))
        self.assertIn(b"- [x] First", path.read_bytes())

    def test_checkbox_line_out_of_range_or_not_a_checkbox(self):
        path = tmp(data=b'---\nattach-app: "x"\n---\nText\n')
        before = path.read_bytes()
        self.assertFalse(N.toggle_checkbox(path, 99))
        self.assertFalse(N.toggle_checkbox(path, 3))
        self.assertEqual(path.read_bytes(), before)

    def test_color_on_a_note_whose_front_matter_was_removed(self):
        path = tmp(data=b"Just text now\n")
        self.assertFalse(N.set_property(path, "note-color", "blue"))
        self.assertEqual(path.read_bytes(), b"Just text now\n")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can write read-only files")
    def test_read_only_note_raises_oserror(self):
        path = tmp(data=b'---\nattach-app: "x"\n---\n- [ ] Task\n')
        os.chmod(path, 0o444)
        try:
            with self.assertRaises(OSError):     # the app catches OSError and shows a message
                N.toggle_checkbox(path, 3)
        finally:
            os.chmod(path, 0o666)

    def test_property_values_that_need_escaping(self):
        path = tmp(data=b'---\nattach-app: "x"\n---\n')
        for value in ['say "hi"', "back\\slash", "#c8f7e1", "colon: here", "🎨"]:
            with self.subTest(value=value):
                N.set_property(path, "note-color", value)
                self.assertEqual(N.parse_note(path)["color"], value)


class Display(unittest.TestCase):
    def test_bad_colors_fall_back_to_yellow(self):
        for color in [None, "", "#12345", "#GGGGGG", "rgb(1,2,3)", "  BLUE  "]:
            with self.subTest(color=color):
                bg = N.palette(color)["bg"]
                self.assertEqual(bg, N.PALETTES["blue"] if color == "  BLUE  " else N.PALETTES["yellow"])

    def test_classify_never_fails(self):
        for line in ["", ">", "> >", "> [!]", "#", "#nospace", "- [", "- [ ]", "1)", "\t\t- [x]   x", "🎉"]:
            with self.subTest(line=line):
                kind, text, extra = N.classify(line)
                self.assertIsInstance(text, str)
                N.indent_of(line)


class Settings(unittest.TestCase):
    def config(self, text):
        return S.read_config(tmp("config.toml", text.encode("utf-8")))

    def test_missing_file_gives_defaults(self):
        cfg, problems = S.read_config(Path(tempfile.mkdtemp()) / "none.toml")
        self.assertEqual((cfg, problems), (S.DEFAULTS, []))

    def test_damaged_file_gives_defaults_and_says_so(self):
        for text in ["vault = ", "this is not toml", "[[[", 'vault = "unclosed']:
            with self.subTest(text=text):
                cfg, problems = self.config(text)
                self.assertEqual(cfg, S.DEFAULTS)
                self.assertEqual(len(problems), 1)
        cfg, problems = S.read_config(tmp("config.toml", b"\xff\xfe\x00binary"))
        self.assertEqual((cfg, len(problems)), (S.DEFAULTS, 1))

    def test_bad_values_are_ignored_one_by_one(self):
        cfg, problems = self.config('vault = "/v"\nhotkey = "Ctrl+N"\nopacity = "lots"\nfolder = "../escape"\n')
        self.assertEqual(cfg, dict(S.DEFAULTS, vault="/v"))
        self.assertEqual(len(problems), 3)

    def test_values_are_tidied(self):
        cfg, problems = self.config('hotkey = "m"\nopacity = 5\n')
        self.assertEqual((cfg["hotkey"], cfg["opacity"], problems), ("M", 1.0, []))
        self.assertEqual(self.config("opacity = 0")[0]["opacity"], 0.2)
        self.assertEqual(len(self.config('hotkey = "1"')[1]), 1)
        self.assertEqual(len(self.config('hotkey = "é"')[1]), 1)

    def test_byte_order_mark(self):
        cfg, problems = S.read_config(tmp("config.toml", '\ufeffvault = "/v"\n'.encode("utf-8")))
        self.assertEqual((cfg["vault"], problems), ("/v", []))

    def test_save_keeps_other_lines_and_escapes(self):
        path = tmp("config.toml", b'# my comment\nvault = "/v"\nhotkey = "N"\n')
        S.save_value(path, "opacity", 0.7)
        S.save_value(path, "vault", 'C:\\Users\\me\\Gus\' "Notes"')
        cfg, problems = S.read_config(path)
        self.assertEqual((cfg["vault"], cfg["opacity"], cfg["hotkey"], problems),
                         ('C:\\Users\\me\\Gus\' "Notes"', 0.7, "N", []))
        self.assertIn("# my comment", path.read_text())

    def test_save_over_a_damaged_file(self):
        path = tmp("config.toml", b"\xff\xfe\x00binary")
        S.save_value(path, "vault", "/v")
        self.assertEqual(S.read_config(path), (dict(S.DEFAULTS, vault="/v"), []))

    def test_folded_state_survives_damage(self):
        vault = Path(tempfile.mkdtemp())
        for data in [b"", b"not json", b"[]", b'{"folded": 5}', b'{"folded": [1, null, "ok.md"]}']:
            with self.subTest(data=data):
                folded = S.load_folded(tmp("state.json", data), vault)
                self.assertEqual(folded, {vault / "ok.md"} if b"ok.md" in data else set())

    def test_folded_round_trip_skips_paths_outside_the_vault(self):
        vault, path = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()) / "state.json"
        inside = {vault / "Window notes" / "A.md", vault / "Window notes" / "sub" / "B.md"}
        S.save_folded(path, vault, inside | {Path(tempfile.mkdtemp()) / "outside.md"})
        self.assertEqual(S.load_folded(path, vault), inside)
        self.assertEqual(json.loads(path.read_text())["folded"], ["Window notes/A.md", "Window notes/sub/B.md"])


if __name__ == "__main__":
    unittest.main()
