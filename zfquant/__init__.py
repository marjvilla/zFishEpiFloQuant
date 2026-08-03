"""Zebrafish fluorescence quantification.

Package layout, and the one rule that keeps it maintainable:

    core.py       pure measurement + threshold logic     no ImageJ, no numpy
    manifest.py   fish -> plane mapping and overrides    no ImageJ
    journal.py    append-only session state              no ImageJ
    fiji_io.py    EVERY ImageJ/Java call in the tool
    ui.py         the Swing control panel

Modules above the fiji_io line must import and run under both Jython 2.7
(inside Fiji) and CPython 3 (for the test suite). Keeping the numeric logic on
the portable side is what makes the math testable, and what would keep a future
port to another viewer confined to the UI shell.
"""
