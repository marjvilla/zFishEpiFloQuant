"""Setup dialogs: what the operator is asked before the interactive review.

Jython-only.

Collects the session name, channels, and the layout MODE choice, plus
whatever stride-determining questions that mode needs before review.py can
walk a position sequence (T-vs-Z for a hyperstack; channel order + first
slice for a flat stack). It does not build a plan itself any more -- that is
review.py's job, done by looking at the real data, not by answering more
blind fields here. See zfquant/review.py.
"""

from __future__ import division

import os

from ij import IJ, WindowManager
# NonBlockingGenericDialog is a drop-in GenericDialog that is NOT modal, so the
# operator can scroll/navigate open image stacks while a setup dialog is still
# showing -- an ordinary GenericDialog blocks every other window while up.
from ij.gui import NonBlockingGenericDialog as GenericDialog
from javax.swing import JFileChooser

from zfquant import core
from zfquant import journal as journal_mod
from zfquant import manifest as manifest_mod


BF_ALIASES = ("bf", "brightfield", "bright field", "dic", "transmitted",
              "trans", "phase")

# One choice per manifest.LAYOUTS entry, built from the same descriptions
# review.py's panel titles are drawn from, so the two can't drift apart.
LAYOUT_CHOICES = [manifest_mod.LAYOUT_DESCRIPTIONS[layout]
                  for layout in manifest_mod.LAYOUTS]
LAYOUT_BY_CHOICE = dict(zip(LAYOUT_CHOICES, manifest_mod.LAYOUTS))

RESUME_RESUME = "resume"
RESUME_NEW = "new"
RESUME_CANCEL = "cancel"


class Config(object):

    def __init__(self):
        self.session_name = "Experiment"
        self.channel_names = []
        self.channel_indices = {}
        self.bf_name = "BF"
        self.layout = manifest_mod.LAYOUT_HYPERSTACK
        # Set from the finished review's (or a reloaded manifest's) actual
        # fish count in Zebrafish_Quant.py's main() -- not asked here, since
        # nobody can know the real count before looking at the data.
        self.fish_total = 1
        self.fish_dimension = "t"
        self.slice_order = None
        self.first_slice = 1
        self.operator = ""
        self.k = core.DEFAULT_K
        self.min_area = core.DEFAULT_MIN_AREA
        self.output_root = None


def prompt(default_output=None):
    """Run the setup flow. Returns a Config, or None if cancelled."""
    images = _open_images()
    if not images:
        IJ.error("No images are open.\n\nOpen your stacks in Fiji, then run "
                 "this again.")
        return None

    config = Config()

    dialog = GenericDialog("Zebrafish Quant - Setup")
    dialog.addMessage("%d image(s) open." % len(images))
    dialog.addStringField("Session name:", "Experiment", 22)
    dialog.addStringField("Operator (recorded in the CSV):", "", 22)

    dialog.addMessage("Channel names in order. Leave a slot blank to skip it.")
    defaults = ["BF", "RFP", "GFP", ""]
    for index in range(4):
        dialog.addStringField("Channel %d:" % (index + 1), defaults[index], 12)

    dialog.addMessage("How is your data organised? You'll confirm this "
                      "against the real images next.")
    dialog.addChoice("Layout:", LAYOUT_CHOICES, LAYOUT_CHOICES[0])

    dialog.addMessage("Segmentation")
    dialog.addNumericField("Threshold k (signal is mean_bg + k * sd_bg):",
                           core.DEFAULT_K, 1)
    dialog.addNumericField("Ignore specks smaller than (pixels):",
                           core.DEFAULT_MIN_AREA, 0)
    dialog.showDialog()
    if dialog.wasCanceled():
        return None

    config.session_name = dialog.getNextString().strip() or "Experiment"
    config.operator = dialog.getNextString().strip()

    names = [dialog.getNextString().strip() for _ in range(4)]
    config.channel_names = [n for n in names if n]
    config.channel_indices = dict(
        (n, i + 1) for i, n in enumerate(names) if n)
    if not config.channel_names:
        IJ.error("At least one channel name is required.")
        return None

    config.layout = LAYOUT_BY_CHOICE[dialog.getNextChoice()]
    config.k = _safe_float(dialog.getNextNumber(), core.DEFAULT_K)
    config.min_area = max(0, _safe_int(dialog.getNextNumber(),
                                       core.DEFAULT_MIN_AREA))

    bf_name = _infer_brightfield(config.channel_names)
    if bf_name is None:
        bf_name = _ask_brightfield(config.channel_names)
        if bf_name is None:
            return None
    config.bf_name = bf_name

    if not _layout_details(config):
        return None

    config.output_root = _choose_directory("Select the OUTPUT folder",
                                           default_output)
    if not config.output_root:
        return None
    return config


def _layout_details(config):
    """The preliminary questions review.py needs before it can compute a
    position sequence to walk. Manual needs none -- it walks open windows
    directly, no stride to determine."""
    if config.layout == manifest_mod.LAYOUT_HYPERSTACK:
        dialog = GenericDialog("Hyperstack layout")
        dialog.addMessage("Which dimension separates the fish?")
        dialog.addChoice("One fish per:", ["Frame (T)", "Slice (Z)"],
                         "Frame (T)")
        dialog.showDialog()
        if dialog.wasCanceled():
            return False
        config.fish_dimension = "t" if dialog.getNextChoice().startswith("F") \
            else "z"
        return True

    # LAYOUT_FLAT_STACK and LAYOUT_PER_WINDOW (Manual) ask nothing up front.
    # Flat stack used to ask for channel order (typed as text) and first
    # slice (typed as a number) here, blind -- both are now set interactively
    # in review.py instead: navigate to the real first slice and click a
    # button, then click channel buttons in the order they actually appear,
    # with the stack itself visible the whole time.
    return True


def ask_about_existing_session(paths):
    """A session directory already exists. Ask, rather than assume.

    The legacy tool appended to the CSV but reset everything else, so re-running
    with the same name overwrote the previous run's ROI archive and exports, and
    one undo click erased its rows.
    """
    if not paths.exists():
        return RESUME_NEW

    existing = journal_mod.SessionJournal(paths.journal)
    count = existing.live_fish_count()
    choice = _three_way(
        "A session named '%s' already exists here with %d fish saved.\n\n"
        "Resume it (new fish are added after the existing ones), or pick a "
        "different name?" % (paths.session_name, count),
        "Existing session", ["Resume", "Choose another name", "Cancel"])
    if choice == 0:
        return RESUME_RESUME
    if choice == 1:
        return RESUME_NEW
    return RESUME_CANCEL


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _open_images():
    ids = WindowManager.getIDList()
    if ids is None:
        return []
    return [WindowManager.getImage(i) for i in ids
            if WindowManager.getImage(i) is not None]


def _infer_brightfield(names):
    matches = [n for n in names if n.strip().lower() in BF_ALIASES]
    return matches[0] if len(matches) == 1 else None


def _ask_brightfield(names):
    dialog = GenericDialog("Brightfield channel")
    dialog.addChoice("Which channel is brightfield (the eye)?", names, names[0])
    dialog.showDialog()
    if dialog.wasCanceled():
        return None
    return dialog.getNextChoice()


def _choose_directory(title, default=None):
    chooser = JFileChooser()
    chooser.setDialogTitle(title)
    chooser.setFileSelectionMode(JFileChooser.DIRECTORIES_ONLY)
    if default and os.path.isdir(default):
        from java.io import File
        chooser.setCurrentDirectory(File(default))
    if chooser.showOpenDialog(None) == JFileChooser.APPROVE_OPTION:
        return chooser.getSelectedFile().getAbsolutePath()
    return None


def _three_way(message, title, options):
    from javax.swing import JOptionPane
    return JOptionPane.showOptionDialog(
        None, message, "Zebrafish Quant - " + title,
        JOptionPane.DEFAULT_OPTION, JOptionPane.QUESTION_MESSAGE,
        None, options, options[0])


def _safe_int(value, fallback):
    """GenericDialog returns NaN for an empty numeric field.

    The legacy script called int() on it directly, which raised, and the
    top-level handler logged a traceback -- so setup simply vanished with no
    message.
    """
    try:
        if value != value:      # NaN
            return fallback
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _safe_float(value, fallback):
    try:
        if value != value:
            return fallback
        return float(value)
    except (TypeError, ValueError):
        return fallback
