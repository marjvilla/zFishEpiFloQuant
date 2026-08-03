"""Append-only session state: the record of what was measured, and when.

Same portability constraint as core.py -- Jython 2.7 and CPython 3, no ImageJ,
no numpy.

The problem this solves
-----------------------
In the legacy script the CSV was the only durable state, and undo rewrote it
from the running process's in-memory history. Re-running with an existing
session name appended to that CSV, so a single "Go Back" click rewrote the file
from *this* run's history alone and silently deleted every row written by every
previous run. A Fiji crash lost all undo state outright.

Here the journal is the source of truth and is only ever appended to. The CSV
and the ROI archive are derived artifacts, rebuilt from it. Undo appends a
tombstone rather than removing anything, so a rollback cannot destroy a row this
process did not write, and the full history of the session survives a crash.
"""

from __future__ import division, print_function

import json
import os
import time


RECORD_SESSION = "session_start"
RECORD_COMMIT = "commit"
RECORD_TOMBSTONE = "tombstone"
RECORD_NOTE = "note"


class JournalError(Exception):
    pass


class SessionJournal(object):
    """A JSONL file where every line is one immutable event."""

    def __init__(self, path):
        self.path = path
        self._corrupt_lines = 0
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)

    # -- writing ----------------------------------------------------------

    def append(self, record):
        """Append one record, flushed all the way to disk.

        fsync matters here: the whole point is surviving a Fiji crash or a
        force-quit, and without it a commit can sit in the OS buffer and be lost
        exactly when it is needed.
        """
        record = dict(record)
        record.setdefault("at", _timestamp())
        line = json.dumps(record, sort_keys=True)
        if "\n" in line:
            raise JournalError("record serialised to a multi-line string")

        handle = open(self.path, "a")
        try:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        return record

    def start_session(self, session_name, channels, bf_name, operator=None,
                      extra=None):
        record = {"type": RECORD_SESSION,
                  "session": session_name,
                  "channels": list(channels),
                  "brightfield": bf_name,
                  "operator": operator or ""}
        if extra:
            record.update(extra)
        return self.append(record)

    def record_commit(self, row, roi_names=None, context=None):
        """Record one measured fish. Returns the assigned sequence number.

        The sequence is derived from the journal itself rather than from an
        in-memory counter, so it keeps rising across resumed sessions and
        FishID values from different runs can never collide -- which in the
        legacy tool caused a second run to overwrite the first run's
        presentation JPEGs.
        """
        seq = self.next_sequence()
        record = {"type": RECORD_COMMIT,
                  "seq": seq,
                  "row": row,
                  "roi_names": list(roi_names or []),
                  "context": dict(context or {})}
        self.append(record)
        return seq

    def record_undo(self, seq, reason="undo"):
        """Tombstone a previously committed fish.

        Nothing is deleted. The commit stays in the file and the tombstone
        records that it was withdrawn, so the session's real history -- including
        mistakes and corrections -- remains auditable.
        """
        if seq not in self._committed_sequences():
            raise JournalError("no commit with seq %r to undo" % (seq,))
        if seq in self._tombstoned_sequences():
            raise JournalError("seq %r is already tombstoned" % (seq,))
        self.append({"type": RECORD_TOMBSTONE, "target_seq": seq,
                     "reason": reason})
        return seq

    def note(self, message, **fields):
        record = {"type": RECORD_NOTE, "message": message}
        record.update(fields)
        return self.append(record)

    # -- reading ----------------------------------------------------------

    def read_all(self):
        """Every well-formed record, oldest first.

        A crash can leave a half-written final line. That line is skipped rather
        than raising, because refusing to open a session over one truncated
        record would strand every valid commit before it.
        """
        if not os.path.exists(self.path):
            return []
        records = []
        self._corrupt_lines = 0
        handle = open(self.path, "r")
        try:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    self._corrupt_lines += 1
        finally:
            handle.close()
        return records

    @property
    def corrupt_lines(self):
        """Malformed lines skipped by the last read_all(); surface this in the UI."""
        return self._corrupt_lines

    def commits(self):
        return [r for r in self.read_all() if r.get("type") == RECORD_COMMIT]

    def live_commits(self):
        """Commits that have not been tombstoned, in commit order."""
        dead = self._tombstoned_sequences()
        return [r for r in self.commits() if r.get("seq") not in dead]

    def live_rows(self):
        return [r.get("row", {}) for r in self.live_commits()]

    def last_live_commit(self):
        live = self.live_commits()
        return live[-1] if live else None

    def next_sequence(self):
        """One past the highest sequence ever issued, tombstoned or not.

        Deliberately not "count of live commits": reusing the sequence of an
        undone fish would make two different measurements share a FishID and an
        export filename.
        """
        highest = 0
        for record in self.commits():
            seq = record.get("seq")
            if isinstance(seq, int) and seq > highest:
                highest = seq
        return highest + 1

    def live_fish_count(self):
        return len(self.live_commits())

    def _committed_sequences(self):
        return set(r.get("seq") for r in self.commits())

    def _tombstoned_sequences(self):
        return set(r.get("target_seq") for r in self.read_all()
                   if r.get("type") == RECORD_TOMBSTONE)

    def session_records(self):
        return [r for r in self.read_all() if r.get("type") == RECORD_SESSION]

    def is_resumed(self):
        """True if this journal already held commits before the current run."""
        return len(self.session_records()) > 1


# ---------------------------------------------------------------------------
#  Derived artifacts
# ---------------------------------------------------------------------------

def rebuild_csv(journal, csv_path, header):
    """Regenerate the session CSV from the journal's live rows.

    Written to a temporary file and moved into place, so a crash midway leaves
    the previous CSV intact rather than a truncated one. Because the rows come
    from the journal and not from process memory, rebuilding after an undo
    preserves every row from every earlier run of this session.
    """
    rows = journal.live_rows()
    temp_path = csv_path + ".tmp"

    lines = [_csv_line(header)]
    for row in rows:
        lines.append(_csv_line([row.get(column, "") for column in header]))

    # Binary mode on purpose. CSV wants CRLF, and in text mode Windows would
    # translate our "\n" into a second "\r", producing "\r\r\n" and a blank line
    # between every row when the file is opened. Binary mode writes exactly the
    # bytes given, identically on macOS, Linux and Windows, under both Jython
    # 2.7 and CPython 3.
    payload = "".join(lines).encode("utf-8", "replace")

    handle = open(temp_path, "wb")
    try:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()

    _replace(temp_path, csv_path)
    return len(rows)


def _csv_line(values):
    return ",".join(_csv_field(v) for v in values) + "\r\n"


def _csv_field(value):
    """Minimal RFC 4180 quoting.

    Hand-rolled rather than using the csv module because csv needs binary mode
    under Python 2 and text mode under Python 3, and this module has to behave
    identically in both. The field set here is numbers and short identifiers, so
    the full quoting rules are not needed -- only correctness for the delimiter,
    quote and newline cases.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    text = value if isinstance(value, str) else _to_text(value)
    if any(ch in text for ch in (',', '"', '\n', '\r')):
        return '"' + text.replace('"', '""') + '"'
    return text


def _to_text(value):
    try:
        return str(value)
    except UnicodeEncodeError:
        # Jython 2.7 raises here on non-ASCII; the legacy script hit the same
        # trap with a checkmark character reaching a Swing label.
        return value.encode("ascii", "replace").decode("ascii")


def _replace(source, destination):
    """os.rename that also works on Windows, where rename onto an existing
    file raises. Fiji runs on plenty of Windows lab machines."""
    if os.path.exists(destination):
        try:
            os.remove(destination)
        except OSError:
            pass
    os.rename(source, destination)


def _timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


# ---------------------------------------------------------------------------
#  Session directory layout
# ---------------------------------------------------------------------------

class SessionPaths(object):
    """Where everything for one session lives."""

    def __init__(self, output_root, session_name):
        self.session_name = session_name
        self.root = os.path.join(output_root, session_name)
        self.roi_dir = os.path.join(self.root, "ROIs")
        self.image_dir = os.path.join(self.root, "Audit_Images")
        self.journal = os.path.join(self.root, session_name + "_journal.jsonl")
        self.csv = os.path.join(self.root, session_name + "_dataset.csv")
        self.manifest = os.path.join(self.root, session_name + "_manifest.json")
        self.roi_zip = os.path.join(self.roi_dir, session_name + "_all_ROIs.zip")

    def ensure(self):
        for directory in (self.root, self.roi_dir, self.image_dir):
            if not os.path.isdir(directory):
                os.makedirs(directory)
        return self

    def exists(self):
        """True if this session name already has data on disk.

        The caller must ask the operator what to do about it -- resume, rename,
        or start fresh -- rather than silently appending, which is how the
        legacy tool ended up overwriting a previous day's exports.
        """
        return os.path.exists(self.journal) or os.path.exists(self.csv)
