# -*- coding: utf-8 -*-
"""Tests for zfquant.journal -- the data-loss guarantees.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import division, print_function

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zfquant import journal   # noqa: E402


class JournalTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zfquant-test-")
        self.path = os.path.join(self.tmp, "session_journal.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def row(self, fish, value=1.0):
        return {"FileName": "img.tif", "FishID": "Fish %d" % fish,
                "GFP_Corrected": value}


class TestAppendOnly(JournalTestCase):

    def test_commits_are_read_back_in_order(self):
        j = journal.SessionJournal(self.path)
        j.record_commit(self.row(1))
        j.record_commit(self.row(2))
        j.record_commit(self.row(3))

        rows = j.live_rows()
        self.assertEqual([r["FishID"] for r in rows],
                         ["Fish 1", "Fish 2", "Fish 3"])

    def test_sequences_are_monotonic(self):
        j = journal.SessionJournal(self.path)
        self.assertEqual(j.record_commit(self.row(1)), 1)
        self.assertEqual(j.record_commit(self.row(2)), 2)
        self.assertEqual(j.next_sequence(), 3)

    def test_undo_tombstones_rather_than_deleting(self):
        j = journal.SessionJournal(self.path)
        j.record_commit(self.row(1))
        seq = j.record_commit(self.row(2))
        j.record_undo(seq)

        # Withdrawn from the live view...
        self.assertEqual([r["FishID"] for r in j.live_rows()], ["Fish 1"])
        # ...but still on disk, so the session's real history is auditable.
        self.assertEqual(len(j.commits()), 2)

    def test_undone_sequence_is_never_reused(self):
        """Reusing it would give two measurements the same FishID and the same
        export filename -- the legacy overwrite bug in a new costume."""
        j = journal.SessionJournal(self.path)
        j.record_commit(self.row(1))
        seq = j.record_commit(self.row(2))
        j.record_undo(seq)
        self.assertEqual(j.record_commit(self.row(3)), 3)

    def test_undo_rejects_unknown_or_repeated_sequences(self):
        j = journal.SessionJournal(self.path)
        seq = j.record_commit(self.row(1))
        self.assertRaises(journal.JournalError, j.record_undo, 99)
        j.record_undo(seq)
        self.assertRaises(journal.JournalError, j.record_undo, seq)


class TestCrashSafety(JournalTestCase):

    def test_truncated_final_line_is_skipped_not_fatal(self):
        """A crash mid-write must not strand every valid commit before it."""
        j = journal.SessionJournal(self.path)
        j.record_commit(self.row(1))
        j.record_commit(self.row(2))

        handle = open(self.path, "a")
        handle.write('{"type": "commit", "seq": 3, "row": {"Fish')  # cut off
        handle.close()

        rows = j.live_rows()
        self.assertEqual([r["FishID"] for r in rows], ["Fish 1", "Fish 2"])
        self.assertEqual(j.corrupt_lines, 1)

    def test_appending_after_a_corrupt_line_still_works(self):
        j = journal.SessionJournal(self.path)
        j.record_commit(self.row(1))
        handle = open(self.path, "a")
        handle.write("{not json\n")
        handle.close()

        j.record_commit(self.row(2))
        self.assertEqual([r["FishID"] for r in j.live_rows()],
                         ["Fish 1", "Fish 2"])

    def test_reopening_resumes_the_sequence(self):
        first = journal.SessionJournal(self.path)
        first.record_commit(self.row(1))
        first.record_commit(self.row(2))

        # Simulates Fiji being closed and the session reopened the next day.
        second = journal.SessionJournal(self.path)
        self.assertEqual(second.next_sequence(), 3)
        self.assertEqual(second.live_fish_count(), 2)


class TestUndoAcrossSessions(JournalTestCase):
    """The legacy bug this design exists to prevent (audit finding 3.5)."""

    def test_undo_cannot_destroy_a_previous_runs_rows(self):
        yesterday = journal.SessionJournal(self.path)
        yesterday.start_session("Exp", ["BF", "GFP"], "BF")
        yesterday.record_commit(self.row(1))
        yesterday.record_commit(self.row(2))

        # New process, same session name -- the exact situation in which the
        # legacy tool's "Go Back" rewrote the CSV from this run's memory only
        # and silently erased yesterday's rows.
        today = journal.SessionJournal(self.path)
        today.start_session("Exp", ["BF", "GFP"], "BF")
        seq = today.record_commit(self.row(3))
        today.record_undo(seq)

        surviving = [r["FishID"] for r in today.live_rows()]
        self.assertEqual(surviving, ["Fish 1", "Fish 2"])

    def test_resume_is_detectable(self):
        j = journal.SessionJournal(self.path)
        j.start_session("Exp", ["BF"], "BF")
        self.assertFalse(j.is_resumed())
        j.start_session("Exp", ["BF"], "BF")
        self.assertTrue(j.is_resumed())


class TestCsvRebuild(JournalTestCase):

    HEADER = ["FileName", "FishID", "GFP_Corrected"]

    def read_lines(self, path):
        """Read as bytes: text mode would translate CRLF away and hide whether
        the line endings we actually wrote are correct."""
        handle = open(path, "rb")
        try:
            content = handle.read().decode("utf-8")
        finally:
            handle.close()
        return content.split("\r\n")

    def test_csv_is_rebuilt_from_live_rows(self):
        j = journal.SessionJournal(self.path)
        j.record_commit(self.row(1, 100.0))
        seq = j.record_commit(self.row(2, 200.0))
        j.record_commit(self.row(3, 300.0))
        j.record_undo(seq)

        csv_path = os.path.join(self.tmp, "out.csv")
        written = journal.rebuild_csv(j, csv_path, self.HEADER)
        self.assertEqual(written, 2)

        lines = self.read_lines(csv_path)
        self.assertEqual(lines[0], "FileName,FishID,GFP_Corrected")
        self.assertEqual(lines[1], "img.tif,Fish 1,100.0")
        self.assertEqual(lines[2], "img.tif,Fish 3,300.0")
        self.assertEqual(lines[3], "")      # trailing CRLF, nothing after it

    def test_line_endings_are_crlf_exactly(self):
        """Not CRCRLF, which is what text mode would produce on Windows."""
        j = journal.SessionJournal(self.path)
        j.record_commit(self.row(1))
        csv_path = os.path.join(self.tmp, "out.csv")
        journal.rebuild_csv(j, csv_path, self.HEADER)

        handle = open(csv_path, "rb")
        try:
            raw = handle.read()
        finally:
            handle.close()
        self.assertNotIn(b"\r\r\n", raw)
        self.assertEqual(raw.count(b"\r\n"), 2)     # header + one row
        self.assertEqual(raw.count(b"\n"), 2)       # no stray bare newlines

    def test_rebuild_leaves_no_temp_file(self):
        j = journal.SessionJournal(self.path)
        j.record_commit(self.row(1))
        csv_path = os.path.join(self.tmp, "out.csv")
        journal.rebuild_csv(j, csv_path, self.HEADER)
        self.assertFalse(os.path.exists(csv_path + ".tmp"))

    def test_rebuild_overwrites_cleanly_on_repeat(self):
        j = journal.SessionJournal(self.path)
        j.record_commit(self.row(1))
        csv_path = os.path.join(self.tmp, "out.csv")
        journal.rebuild_csv(j, csv_path, self.HEADER)
        j.record_commit(self.row(2))
        journal.rebuild_csv(j, csv_path, self.HEADER)

        lines = [l for l in self.read_lines(csv_path) if l]
        self.assertEqual(len(lines), 3)     # header + 2 rows, not 4

    def test_missing_columns_become_empty_not_absent(self):
        j = journal.SessionJournal(self.path)
        j.record_commit({"FishID": "Fish 1"})       # no FileName, no GFP
        csv_path = os.path.join(self.tmp, "out.csv")
        journal.rebuild_csv(j, csv_path, self.HEADER)
        self.assertEqual(self.read_lines(csv_path)[1], ",Fish 1,")

    def test_fields_needing_quotes_are_quoted(self):
        j = journal.SessionJournal(self.path)
        j.record_commit({"FileName": 'weird,"name".tif', "FishID": "Fish 1",
                         "GFP_Corrected": 1.0})
        csv_path = os.path.join(self.tmp, "out.csv")
        journal.rebuild_csv(j, csv_path, self.HEADER)
        self.assertIn('"weird,""name"".tif"',
                      "\r\n".join(self.read_lines(csv_path)))

    def test_booleans_render_as_words(self):
        self.assertEqual(journal._csv_field(True), "TRUE")
        self.assertEqual(journal._csv_field(False), "FALSE")
        self.assertEqual(journal._csv_field(None), "")


class TestSessionPaths(JournalTestCase):

    def test_layout_and_creation(self):
        paths = journal.SessionPaths(self.tmp, "Exp1")
        self.assertFalse(paths.exists())
        paths.ensure()
        self.assertTrue(os.path.isdir(paths.roi_dir))
        self.assertTrue(os.path.isdir(paths.image_dir))
        self.assertTrue(paths.csv.endswith("Exp1_dataset.csv"))
        self.assertTrue(paths.roi_zip.endswith("Exp1_all_ROIs.zip"))

    def test_exists_detects_a_previous_run(self):
        paths = journal.SessionPaths(self.tmp, "Exp1").ensure()
        journal.SessionJournal(paths.journal).record_commit(self.row(1))
        self.assertTrue(journal.SessionPaths(self.tmp, "Exp1").exists())


if __name__ == "__main__":
    unittest.main()
