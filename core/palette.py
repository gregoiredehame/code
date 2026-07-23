"""
CODE EDITOR.

Author: Gregoire Dehame
Created: Jul 22, 2026
Module: code_editor.core.palette
Execute: from code_editor.core import palette

VS Code's Command Palette: type a few letters, get the command, run it.

It builds its list from the menu bar itself rather than from a table someone has to maintain, so a
command added to a menu is in the palette the same day - and one removed cannot linger as a dead
entry pointing at a slot that no longer exists.

Matching is a subsequence, not a substring: "sal" finds "Save All" and "tglpn" finds "Toggle Panel",
which is what makes a palette faster than the menus it replaces. Hits are ranked so that a tighter,
earlier, word-boundary match comes first.
"""

from . import qt


def score(query:str, text:str) -> int:
    """How well `text` matches `query` as a subsequence. -1 when it does not match at all.

    Higher is better. A letter that starts a word counts for much more than one buried inside it, so
    "sa" prefers "Save All" over "Close All Editors", and consecutive letters beat scattered ones.
    """
    if not query:
        return 0
    lowered = text.lower()
    position, total, previous = 0, 0, -2
    for letter in query.lower():
        found = lowered.find(letter, position)
        if found < 0:
            return -1
        bonus = 0
        if found == 0 or not lowered[found - 1].isalnum():
            bonus += 8                                  # start of a word
        if found == previous + 1:
            bonus += 5                                  # runs of adjacent letters read as intended
        total += bonus + max(0, 10 - found // 4)        # earlier in the string is better
        previous, position = found, found + 1
    return total


class CommandPalette(qt.QWidget):
    """The overlay itself: a field over a list, floating at the top of the window."""

    WIDTH = 560
    ROWS = 12

    def __init__(self, parent:qt.QWidget, provider) -> None:
        """`provider` returns the QActions to offer, newest state each time it is opened."""
        super().__init__(parent)
        self._provider = provider
        self._entries = []                              # [(label, shortcut, action)]

        self.setObjectName("codePalette")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)
        self.setVisible(False)

        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(qt.px(4), qt.px(4), qt.px(4), qt.px(4))
        layout.setSpacing(qt.px(4))

        self.field = qt.QLineEdit()
        self.field.setObjectName("codePaletteField")
        self.field.setPlaceholderText("Type a command")
        layout.addWidget(self.field, 0)

        self.list = qt.QListWidget()
        self.list.setObjectName("codePaletteList")
        self.list.setFrameShape(qt.QFrame.NoFrame)
        self.list.setUniformItemSizes(True)
        layout.addWidget(self.list, 1)

        self.field.textChanged.connect(self._filter)
        self.field.returnPressed.connect(self._run)
        self.list.itemActivated.connect(lambda *_: self._run())
        self.field.installEventFilter(self)              # arrows must drive the list, not the field

    # ------------------------------------------------------------------ open / close

    def open(self) -> None:
        """Open on the menu bar's commands."""
        entries = []
        for action in self._provider():
            label, _, shortcut = action.text().partition("\t")
            label = label.replace("&", "").strip()
            if label and action.isEnabled():   # a greyed command must not be runnable from here
                entries.append((label, shortcut, action.trigger))
        entries.sort(key=lambda entry: entry[0].lower())
        self.open_entries(entries, "Type a command")

    def open_entries(self, entries:list, placeholder:str="Type to filter") -> None:
        """Open on an arbitrary list of (label, hint, callable).

        Go to File and Go to Symbol are the same overlay with a different list, so the widget takes
        entries rather than only actions - one piece of UI, three commands.
        """
        self._entries = entries
        self.field.setPlaceholderText(placeholder)
        self.field.clear()
        self._filter("")
        self.setVisible(True)
        self.reposition()
        self.raise_()
        self.field.setFocus()
        # watch the whole application while open: a click anywhere outside must dismiss it, and the
        # overlay has no title bar or close button to fall back on
        application = qt.QApplication.instance()
        if application is not None:
            application.installEventFilter(self)

    def toggle(self) -> None:
        """Open it, or close it if it is already up. The same key has to get you back out."""
        self.close_palette() if self.isVisible() else self.open()

    def close_palette(self) -> None:
        application = qt.QApplication.instance()
        if application is not None:
            application.removeEventFilter(self)
        self.setVisible(False)
        parent = self.parentWidget()
        if parent is not None:
            parent.setFocus()

    def reposition(self) -> None:
        """Centre it near the top of the window, the way VS Code drops it in."""
        parent = self.parentWidget()
        if parent is None:
            return
        width = min(qt.px(self.WIDTH), max(qt.px(280), parent.width() - qt.px(80)))
        self.setFixedWidth(width)
        self.adjustSize()
        self.move((parent.width() - width) // 2, qt.px(28))

    # ------------------------------------------------------------------ filtering

    def _filter(self, query:str) -> None:
        ranked = []
        for label, hint, callback in self._entries:
            value = score(query, label)
            if value >= 0:
                ranked.append((value, label, hint, callback))
        ranked.sort(key=lambda entry: (-entry[0], entry[1].lower()))

        self.list.clear()
        for _, label, hint, callback in ranked[:400]:    # a workspace can hold thousands of files
            item = qt.QListWidgetItem("%s\t%s" % (label, hint) if hint else label)
            item.setData(qt.Qt.UserRole, callback)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)
        rows = min(self.ROWS, max(1, self.list.count()))
        self.list.setFixedHeight(rows * qt.px(22) + qt.px(4))
        self.adjustSize()
        self.reposition()

    def _run(self) -> None:
        item = self.list.currentItem()
        self.close_palette()                             # close FIRST: the command may open a dialog
        if item is None:
            return
        callback = item.data(qt.Qt.UserRole)
        try:
            callback()
        except RuntimeError:
            pass                                         # the action died with a reloaded window

    # ------------------------------------------------------------------ keys

    def eventFilter(self, watched, event):
        kind = event.type()
        if watched is self.field and kind == qt.QEvent.KeyPress:
            if event.key() in (qt.Qt.Key_Down, qt.Qt.Key_Up):
                row = self.list.currentRow() + (1 if event.key() == qt.Qt.Key_Down else -1)
                self.list.setCurrentRow(max(0, min(row, self.list.count() - 1)))
                return True

        if self.isVisible():
            # Escape from ANY widget, and a click outside, both close it. Watching only the field
            # would strand the overlay open the moment focus went elsewhere.
            if kind == qt.QEvent.KeyPress and event.key() == qt.Qt.Key_Escape:
                self.close_palette()
                return True
            if kind == qt.QEvent.MouseButtonPress and not self._contains(event):
                self.close_palette()          # not consumed: the click still reaches its target
        return super().eventFilter(watched, event)

    def _contains(self, event) -> bool:
        """True when a mouse event happened inside the overlay."""
        try:
            point = event.globalPosition().toPoint() if hasattr(event, "globalPosition") \
                else event.globalPos()
            return self.rect().contains(self.mapFromGlobal(point))
        except Exception:
            return True                       # unreadable position: keep it open rather than flicker

    def keyPressEvent(self, event) -> None:
        if event.key() == qt.Qt.Key_Escape:
            self.close_palette()
            return
        super().keyPressEvent(event)
