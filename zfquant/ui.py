"""The floating control panel.

Jython-only. Three rules, each fixing a specific audit finding:

  * Keys are bound through InputMap/ActionMap at WHEN_IN_FOCUSED_WINDOW and
    every button is non-focusable. In the legacy panel the listener sat on the
    JFrame while the buttons were focusable, so a focused JButton swallowed
    Space as a click -- after clicking "Go Back", pressing Space rolled back
    another fish.
  * The layout is built once and only label TEXT changes afterwards. The legacy
    panel tore down and repacked itself on every keystroke, which is why it
    jittered and dropped focus.
  * All text is plain ASCII. Non-ASCII in a Swing component can throw
    UnicodeDecodeError under Jython 2.7 even with a UTF-8 source header.
"""

from __future__ import division

import traceback

from java.awt import (BorderLayout, Color, Dimension, Font, GridLayout,
                      KeyEventDispatcher, KeyboardFocusManager)
from java.awt.event import ActionListener, KeyEvent, WindowAdapter
from javax.swing import (BorderFactory, BoxLayout, JButton, JFrame, JLabel,
                         JOptionPane, JPanel, SwingUtilities, UIManager)
from javax.swing.border import EmptyBorder
from javax.swing.text import JTextComponent

from zfquant import workflow


PANEL_WIDTH = 340

# Digit keys are accelerators, not the primary interface -- the checklist rows
# are. The legacy design led with hotkeys because that is how it grew.
CHANNEL_KEYS = "123456789"


def apply_look_and_feel():
    """Use FlatLaf when the running Fiji ships it; fall back silently.

    Cosmetic only, but it is most of the visual gap between a Swing panel and a
    modern desktop app, and it costs nothing when unavailable.
    """
    for candidate in ("com.formdev.flatlaf.FlatLightLaf",
                      "javax.swing.plaf.nimbus.NimbusLookAndFeel"):
        try:
            UIManager.setLookAndFeel(candidate)
            return candidate
        except Exception:
            continue
    return None


class _GlobalKeyDispatcher(KeyEventDispatcher):
    """Intercepts our action keys application-wide, before Fiji's own
    per-window handling ever sees them.

    A Swing InputMap only fires while the JFrame holding it is the focused
    window -- useless here, since the operator spends most of their time
    with focus on the image or working-copy window while drawing, not on the
    control panel. Worse, Fiji binds plain digit keys 1-9 to switch between
    open image windows at the application level (the legacy tool's single
    worst hotkey bug was an unconsumed digit key both arming a channel AND
    triggering that window-switch). A KeyEventDispatcher registered on the
    KeyboardFocusManager sees every key event before any window's own
    handling does, everywhere in the application, so consuming it here is
    enough to stop both problems regardless of which window has focus.

    Skips interception when focus is in a text-editable component (a
    GenericDialog field, the Threshold window's numeric fields, ...) so
    normal typing there is never hijacked.
    """

    def __init__(self, bindings):
        self._bindings = bindings   # {KeyEvent.VK_X: callable}

    def dispatchKeyEvent(self, event):
        if event.getID() != KeyEvent.KEY_PRESSED:
            return False
        if isinstance(event.getComponent(), JTextComponent):
            return False
        callback = self._bindings.get(event.getKeyCode())
        if callback is None:
            return False
        event.consume()
        try:
            callback()
        except Exception:
            from zfquant import fiji_io
            fiji_io.log(traceback.format_exc())
        return True


class _Click(ActionListener):

    def __init__(self, callback):
        self.callback = callback

    def actionPerformed(self, event):
        try:
            self.callback()
        except Exception:
            from zfquant import fiji_io
            fiji_io.log(traceback.format_exc())


class ControlPanel(object):

    def __init__(self, controller):
        self.controller = controller
        controller.panel = self
        self.frame = None

        self.mode_label = None
        self.fish_label = None
        self.plane_label = None
        self.progress_label = None
        self.warning_label = None
        self.status_label = None

        self._channel_rows = {}      # name -> (button, state JLabel)
        self._channel_order = []     # to detect when a rebuild is really needed
        self._key_dispatcher = None
        self._build()

    # -- construction -----------------------------------------------------

    def _build(self):
        apply_look_and_feel()

        frame = JFrame("Zebrafish Quant")
        frame.setDefaultCloseOperation(JFrame.DO_NOTHING_ON_CLOSE)
        frame.setAlwaysOnTop(True)

        root = JPanel()
        root.setLayout(BoxLayout(root, BoxLayout.Y_AXIS))
        root.setBorder(EmptyBorder(10, 10, 10, 10))

        self.mode_label = self._label("", bold=True, size=13)
        self.fish_label = self._label("")
        self.plane_label = self._label("")
        self.progress_label = self._label("")

        self.warning_label = self._label("", bold=True)
        self.warning_label.setForeground(Color(0xB0, 0x60, 0x00))

        self.status_label = self._label("Ready.")
        self.status_label.setPreferredSize(Dimension(PANEL_WIDTH, 48))
        self.status_label.setVerticalAlignment(JLabel.TOP)

        for component in (self.mode_label, self.fish_label, self.plane_label,
                          self.progress_label, self.warning_label):
            root.add(component)
        root.add(self._spacer())
        root.add(self.status_label)
        root.add(self._spacer())

        root.add(self._heading("Channels"))
        self.channel_panel = JPanel(GridLayout(0, 1, 4, 4))
        self.channel_panel.setBorder(
            BorderFactory.createLineBorder(Color(0xD0, 0xD0, 0xD0)))
        root.add(self.channel_panel)
        self._rebuild_channel_rows()

        root.add(self._spacer())
        root.add(self._heading("This fish"))
        root.add(self._button_grid([
            ("Accept  (Space)", self.controller.accept),
            ("Use threshold sliders  (T)", self.controller.capture_threshold),
            ("Skip channel  (S)", self.controller.skip),
            ("Undo  (Backspace)", self._confirm_undo),
            ("Save fish  (Enter)", self.controller.commit),
        ]))

        root.add(self._spacer())
        root.add(self._heading("Corrections"))
        root.add(self._button_grid([
            ("Measure the plane I am on  (P)", self.controller.use_current_plane),
            ("Apply that fix to later fish", self.controller.promote_override),
        ]))

        root.add(self._spacer())
        root.add(self._button_grid([
            ("Add another fish", self.controller.add_fish),
            ("Finish session", self._confirm_finish),
        ]))

        frame.getContentPane().add(root, BorderLayout.CENTER)
        self._bind_keys()

        frame.addWindowListener(_CloseGuard(self))
        frame.pack()
        # Sized once. Everything after this changes label text only.
        frame.setSize(PANEL_WIDTH + 40, frame.getHeight())
        frame.setLocation(40, 80)
        frame.setVisible(True)
        self.frame = frame
        self.refresh()

    def _label(self, text, bold=False, size=12):
        label = JLabel(text)
        style = Font.BOLD if bold else Font.PLAIN
        label.setFont(Font("SansSerif", style, size))
        return label

    def _heading(self, text):
        label = self._label(text, bold=True, size=11)
        label.setForeground(Color(0x60, 0x60, 0x60))
        return label

    def _spacer(self):
        panel = JPanel()
        panel.setPreferredSize(Dimension(1, 8))
        panel.setMaximumSize(Dimension(PANEL_WIDTH, 8))
        return panel

    def _button_grid(self, entries):
        panel = JPanel(GridLayout(0, 1, 4, 4))
        for text, callback in entries:
            panel.add(self._button(text, callback))
        return panel

    def _button(self, text, callback):
        button = JButton(text)
        # The whole point: a focusable button eats Space as a click.
        button.setFocusable(False)
        button.addActionListener(_Click(callback))
        return button

    # -- channel checklist ------------------------------------------------

    def _rebuild_channel_rows(self):
        """Build one row per channel. Called only when the channel list changes,
        never on a routine refresh."""
        session = self.controller.s
        self.channel_panel.removeAll()
        self._channel_rows = {}

        for index, name in enumerate(session.channel_names):
            row = JPanel(BorderLayout(6, 0))
            key = CHANNEL_KEYS[index] if index < len(CHANNEL_KEYS) else "-"
            suffix = " (Eye)" if name == session.bf_name else ""
            button = self._button("[%s]  %s%s" % (key, name, suffix),
                                  self._arm_callback(name))
            state = self._label("", size=11)
            state.setPreferredSize(Dimension(70, 20))
            row.add(button, BorderLayout.CENTER)
            row.add(state, BorderLayout.EAST)
            self.channel_panel.add(row)
            self._channel_rows[name] = (button, state)

        self._channel_order = list(session.channel_names)

    def _arm_callback(self, name):
        def callback():
            self.controller.arm(name)
        return callback

    # -- key bindings -----------------------------------------------------

    def _bind_keys(self):
        """Global interception -- see _GlobalKeyDispatcher for why this has
        to be application-wide rather than a Swing InputMap."""
        bindings = {
            KeyEvent.VK_SPACE: self.controller.accept,
            KeyEvent.VK_ENTER: self.controller.commit,
            KeyEvent.VK_S: self.controller.skip,
            KeyEvent.VK_P: self.controller.use_current_plane,
            KeyEvent.VK_T: self.controller.capture_threshold,
            KeyEvent.VK_BACK_SPACE: self._confirm_undo,
        }

        digit_codes = [KeyEvent.VK_1, KeyEvent.VK_2, KeyEvent.VK_3,
                       KeyEvent.VK_4, KeyEvent.VK_5, KeyEvent.VK_6,
                       KeyEvent.VK_7, KeyEvent.VK_8, KeyEvent.VK_9]
        session = self.controller.s
        for index, name in enumerate(session.channel_names):
            if index >= len(digit_codes):
                break
            bindings[digit_codes[index]] = self._arm_callback(name)

        self._key_dispatcher = _GlobalKeyDispatcher(bindings)
        KeyboardFocusManager.getCurrentKeyboardFocusManager() \
            .addKeyEventDispatcher(self._key_dispatcher)

    # -- updates ----------------------------------------------------------

    def set_status(self, text):
        self._later(lambda: self.status_label.setText(_html(text)))

    def set_warning(self, text):
        self._later(lambda: self.warning_label.setText(_html(text)))

    def refresh(self):
        self._later(self._refresh_now)

    def _refresh_now(self):
        session = self.controller.s

        if self._channel_order != list(session.channel_names):
            self._rebuild_channel_rows()
            self.channel_panel.revalidate()

        mode = "Session: %s" % session.paths.session_name
        if session.finished:
            mode += "   [FINISHED]"
        self.mode_label.setText(mode)

        self.fish_label.setText(
            "%s   (%d of %d saved)" % (session.fish_label(),
                                       session.committed_count(),
                                       session.fish_target))

        self.plane_label.setText(
            self.controller.describe_armed() or "No channel armed")

        resolved, total = session.progress()
        self.progress_label.setText("Resolved: %d of %d channels"
                                    % (resolved, total))

        for name, (button, state) in self._channel_rows.items():
            value = session.state_of(name)
            state.setText(value)
            state.setForeground(_STATE_COLORS.get(value, Color.GRAY))

    def _later(self, callback):
        def run():
            try:
                callback()
            except Exception:
                from zfquant import fiji_io
                fiji_io.log(traceback.format_exc())
        SwingUtilities.invokeLater(run)

    # -- confirmations ----------------------------------------------------

    def _confirm_undo(self):
        controller = self.controller
        if controller._fish_undo:
            # Undoing an accepted eye/background/signal/skip within the
            # current, not-yet-saved fish touches nothing on disk beyond what
            # that very action just wrote -- no confirmation needed, this
            # should feel like a normal Undo button.
            controller.undo()
            return

        last = controller.s.journal.last_live_commit()
        if last is None:
            controller.status("Nothing to undo.")
            return
        label = last.get("row", {}).get("FishID", "the last fish")
        if self._ask("Withdraw %s?\n\nIts row leaves the CSV and its ROIs leave "
                     "the archive. The journal keeps a record either way."
                     % label, "Undo"):
            controller.undo()

    def _confirm_finish(self):
        if self._ask("Finish this session?\n\n%d fish saved."
                     % self.controller.s.committed_count(), "Finish"):
            self.controller.finish()

    def prompt_target_reached(self):
        def ask():
            session = self.controller.s
            if self._ask("Target of %d fish reached.\n\nAdd one more, or finish?"
                         % session.fish_target, "Target reached",
                         yes="Add one more", no="Finish"):
                self.controller.add_fish()
            else:
                self.controller.finish()
        self._later(ask)

    def _ask(self, message, title, yes="Yes", no="Cancel"):
        choice = JOptionPane.showOptionDialog(
            self.frame, message, "Zebrafish Quant - " + title,
            JOptionPane.YES_NO_OPTION, JOptionPane.QUESTION_MESSAGE,
            None, [yes, no], no)
        return choice == JOptionPane.YES_OPTION

    def close(self):
        self.controller.shutdown()
        if self._key_dispatcher is not None:
            KeyboardFocusManager.getCurrentKeyboardFocusManager() \
                .removeKeyEventDispatcher(self._key_dispatcher)
            self._key_dispatcher = None
        if self.frame is not None:
            self.frame.dispose()


class _CloseGuard(WindowAdapter):
    """Confirm before closing.

    The legacy panel was set to DISPOSE_ON_CLOSE, so one stray click on the X
    ended the session and took the in-progress fish and the undo history with
    it, silently.
    """

    def __init__(self, panel):
        self.panel = panel

    def windowClosing(self, event):
        session = self.panel.controller.s
        resolved, total = session.progress()
        pending = resolved > 0 and resolved < total
        message = "Close Zebrafish Quant?"
        if pending:
            message += ("\n\n%d of %d channels are captured for %s and have "
                        "not been saved." % (resolved, total,
                                             session.fish_label()))
        if self.panel._ask(message, "Close", yes="Close", no="Keep working"):
            self.panel.close()


_STATE_COLORS = {
    workflow.STATE_DONE: Color(0x1B, 0x7F, 0x3B),
    workflow.STATE_BG_SET: Color(0x99, 0x66, 0x00),
    workflow.STATE_SKIPPED: Color(0x88, 0x88, 0x88),
    workflow.STATE_PENDING: Color(0x55, 0x55, 0x55),
}


def _html(text):
    """Wrap for wrapping, escaping first.

    The legacy panel interpolated raw text into an HTML label, so a '<' in an
    image title broke the display.
    """
    safe = (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))
    return "<html><body style='width:%dpx'>%s</body></html>" % (
        PANEL_WIDTH - 20, safe)
