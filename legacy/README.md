# Legacy script — reconstructed copy

`Zebrafish_Quant_legacy.py` is the original single-file Jython tool that the
rebuild replaces. It is kept here so the lab has a known-working fallback while
the new tool is verified in Fiji.

## Important: this is a reconstruction, not the original file

I overwrote the original `Zebrafish_Quant.py` in the project root when writing
the new entry point, before moving it aside. This directory is not a byte-exact
restore — there was no git repository, no Time Machine backup, and no other copy
on this machine, so it was reconstructed from the full read taken during the
audit.

**Verified:** all 8 classes present (`StartupConfig`, `ZebrafishSession`,
`QuantEngine`, `Exporter`, `WandRoiListener`, `HotkeyListener`,
`WorkflowController`, `ControlPanel`), all 7 module-level functions, same
structure and ordering.

**Not verified:** exact byte equality. The reconstruction is 1831 lines against
the original's 1832 — a one-line difference, most likely a blank line or the
trailing newline, but it has not been possible to confirm which.

## What to do

If you have another copy anywhere — a Fiji `scripts/` folder on the lab machine,
an email attachment, Dropbox/OneDrive version history, a colleague's copy — diff
it against this file and keep theirs:

```bash
diff /path/to/your/copy.py legacy/Zebrafish_Quant_legacy.py
```

If no other copy exists, this one should be functional, but treat the first run
as a check rather than an assumption.

## Behaviour notes

This is the version the audit describes, with all its findings still present —
including the frame-maximum threshold, the unrecorded threshold value, and the
undo path that rewrites the CSV from process memory. Do not run it against a
session directory belonging to the new tool: its undo will rewrite that CSV from
its own in-memory history and drop rows it did not write.
