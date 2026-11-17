"""Tests for notes.py. Run from the project folder:  python -m unittest"""
import tempfile
import unittest
from pathlib import Path

import notes as N


def write(text):
    """A temporary note file with this text."""
    path = Path(tempfile.mkdtemp()) / "Test note.md"
    path.write_text(text, encoding="utf-8", newline="")   # newline="": write line endings exactly as given
    return path


class ParseAndMatch(unittest.TestCase):
    def test_attached_note(self):
        note = N.parse_note(write('---\nattach-app: "notepad.exe"\nnote-color: blue\n---\n\nHello\n'))
        self.assertEqual((note["app"], note["color"], note["name"]), ("notepad.exe", "blue", "Test note"))
        self.assertEqual(note["lines"], [(5, "Hello")])

    def test_not_attached(self):
        self.assertIsNone(N.parse_note(write("---\ntags: x\n---\nHello\n")))
        self.assertIsNone(N.parse_note(write("No front matter\n")))

    def test_byte_order_mark_and_crlf(self):
        note = N.parse_note(write('﻿---\r\nattach-title: "*Budget*"\r\n---\r\nHi\r\n'))
        self.assertEqual(note["title"], "*Budget*")

    def test_matching(self):
        note = {"app": "excel", "title": "*budget*", "url": ""}
        self.assertTrue(N.matches(note, {"app": "EXCEL.EXE", "title": "Budget 2026.xlsx - Excel"}))
        self.assertFalse(N.matches(note, {"app": "excel.exe", "title": "Taxes.xlsx"}))

    def test_url_matching(self):
        note = {"app": "", "title": "", "url": "github.com/*"}
        self.assertTrue(N.matches(note, {"app": "chrome", "title": "", "url": "https://www.github.com/a/b"}))
        self.assertTrue(N.matches(note, {"app": "chrome", "title": "", "url": "github.com"}))
        self.assertFalse(N.matches(note, {"app": "notepad", "title": "", "url": ""}))


class Editing(unittest.TestCase):
    def test_set_and_remove_property(self):
        path = write('---\nattach-app: "notepad"\n---\nBody\n')
        N.set_property(path, "note-color", "pink")
        self.assertEqual(N.parse_note(path)["color"], "pink")
        N.set_property(path, "note-color", "")
        self.assertEqual(N.parse_note(path)["color"], "")
        self.assertIn("Body", path.read_text(encoding="utf-8"))

    def test_toggle_checkbox_keeps_crlf(self):
        path = write('---\r\nattach-app: "x"\r\n---\r\n- [ ] Task\r\n')
        N.toggle_checkbox(path, 3)
        self.assertEqual(path.read_bytes().decode().splitlines()[3], "- [x] Task")
        self.assertIn(b"\r\n", path.read_bytes())

    def test_new_note_round_trip(self):
        win = {"app": "chrome.exe", "title": "Docs [draft]", "url": "https://example.com/a?b=1"}
        note = N.parse_note(write(N.new_note_text(win)))
        self.assertEqual(note["url"], "example.com/a*")
        self.assertTrue(N.matches(note, win))
        win["url"] = ""
        note = N.parse_note(write(N.new_note_text(win)))
        self.assertTrue(N.matches(note, win))      # [ ] in titles are made into ? wildcards


class Display(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(N.classify("- [x] Done"), ("checkbox", "Done", True))
        self.assertEqual(N.classify("## Head"), ("heading", "Head", None))
        self.assertEqual(N.classify("> [!tip] Remember"), ("heading", "Remember", None))
        self.assertEqual(N.classify("2. Two"), ("bullet", "Two", "2."))
        self.assertEqual(N.classify("   "), ("blank", "", None))

    def test_links(self):
        self.assertEqual(N.first_link("See [[Budget#Q1|it]]"), ("note", "Budget"))
        self.assertEqual(N.first_link("at https://x.com/a."), ("url", "https://x.com/a"))
        self.assertEqual(N.first_link("[o](Other%20note.md)"), ("note", "Other note"))
        self.assertEqual(N.plain("**b** [[a|alias]] [t](u)"), "b alias t")

    def test_palette(self):
        self.assertEqual(N.palette("nonsense")["bg"], N.PALETTES["yellow"])
        self.assertGreater(N.palette("dark")["fg"], "#e")      # light text on a dark note


if __name__ == "__main__":
    unittest.main()
