"""
CODE EDITOR.

Author: Gregoire Dehame
Created: Jul 22, 2026
Module: code_editor.core.find
Execute: from code_editor.core import find

VS Code's find widget: a small panel that FLOATS over the top-right of the editor instead of sitting
in a bar underneath it. That placement is the whole point - the bar at the bottom pushes the text up
every time it opens, so the line you were reading moves, and it is furthest from where you are
looking. Floating, it covers three lines of code you are not reading and nothing shifts.

The option toggles (Aa, ab, .*) live INSIDE the input, the way VS Code draws them, which is why the
field reserves right-hand text margins and the buttons are positioned by hand over it.

Highlighting goes through CodeTextEdit.set_search_highlights, so the current-line band, the bracket
match and the syntax-error underline all survive - the editor merges them rather than replacing.
"""

import os
import re

from . import qt

__icons__ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")


def _icon(name:str) -> "qt.QIcon":
    """Load an icon by file name from core/icons."""
    return qt.QIcon(os.path.join(__icons__, name))


_GLYPHS = {}


def glyph(name:str, colour:str="#cccccc") -> "qt.QIcon":
    """The panel's action marks, drawn as vectors.

    Written as characters - "\\u2191", "\\u2715", "\\u21b5" - their shape and weight are whatever the
    installed font decides, which is why they came out spindly and unlike VS Code's. Drawn here they
    are one stroke width, one size, and identical on every machine.
    """
    key = (name, colour)
    if key in _GLYPHS:
        return _GLYPHS[key]

    size = qt.px(16)
    pixmap = qt.QPixmap(size, size)
    pixmap.fill(qt.Qt.transparent)
    painter = qt.QPainter(pixmap)
    painter.setRenderHint(qt.QPainter.Antialiasing, True)
    pen = qt.QPen(qt.QColor(colour))
    pen.setWidthF(max(1.2, size / 10.0))
    pen.setCapStyle(qt.Qt.RoundCap)
    pen.setJoinStyle(qt.Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(qt.Qt.NoBrush)

    def point(fx, fy):
        return qt.QPointF(size * fx, size * fy)

    def arrow(tip_y, tail_y):
        """A vertical shaft with a chevron head at `tip_y`."""
        painter.drawLine(point(0.5, tail_y), point(0.5, tip_y))
        head = 0.20 if tip_y < tail_y else -0.20
        painter.drawLine(point(0.30, tip_y + head), point(0.5, tip_y))
        painter.drawLine(point(0.70, tip_y + head), point(0.5, tip_y))

    if name == "up":
        arrow(0.20, 0.80)
    elif name == "down":
        arrow(0.80, 0.20)
    elif name == "close":
        painter.drawLine(point(0.26, 0.26), point(0.74, 0.74))
        painter.drawLine(point(0.74, 0.26), point(0.26, 0.74))
    elif name == "selection":
        # VS Code's `selection` codicon reads as a BLOCK OF TEXT inside a frame, not a frame with a
        # bar through it. Three lines of decreasing length is what makes it say "these lines", which
        # is exactly what confining a search to the selection means.
        painter.drawRoundedRect(qt.QRectF(size * 0.14, size * 0.16, size * 0.72, size * 0.68),
                                size * 0.12, size * 0.12)
        line = qt.QPen(qt.QColor(colour))
        line.setWidthF(max(1.0, size / 14.0))
        line.setCapStyle(qt.Qt.RoundCap)
        painter.setPen(line)
        # 0.72 at the widest, not 0.76: a round cap reaches ~0.6px past the endpoint, which left the
        # middle line all but touching the frame
        for row, right in ((0.36, 0.66), (0.50, 0.72), (0.64, 0.56)):
            painter.drawLine(point(0.30, row), point(right, row))
        painter.setPen(pen)
    elif name in ("replace", "replace_all"):
        # an arrow running into a block: what is on the left is swapped for what is on the right
        rows = ((0.50,),) if name == "replace" else ((0.32, 0.70),)
        for y in rows[0]:
            painter.drawLine(point(0.12, y), point(0.52, y))
            painter.drawLine(point(0.40, y - 0.12), point(0.52, y))
            painter.drawLine(point(0.40, y + 0.12), point(0.52, y))
            painter.setPen(qt.Qt.NoPen)
            painter.setBrush(qt.QColor(colour))
            painter.drawRect(qt.QRectF(size * 0.64, size * (y - 0.13), size * 0.26, size * 0.26))
            painter.setPen(pen)
            painter.setBrush(qt.Qt.NoBrush)
    painter.end()

    _GLYPHS[key] = qt.QIcon(pixmap)
    return _GLYPHS[key]

# search highlights are semantic, not theming: the same two tones read correctly on every dark
# surface, and they are the ones VS Code uses
MATCH_COLOR   = "#623315"                   # every other hit
CURRENT_COLOR = "#9e6a03"                   # the one the caret is on


class FindReplace(qt.QWidget):
    """The floating find / replace panel for one CodeTextEdit."""

    MARGIN = 12                             # gap kept from the editor's top-right corner

    def __init__(self, editor:qt.QWidget) -> None:
        super().__init__(editor)
        self.editor = editor
        self.matches = []                   # [(start, end)] positions in the document
        self.index = -1                     # which match is current
        self._replacing = False             # guards the re-search a replacement would trigger
        self._range = None                  # (start, end) the search is confined to, if any

        self.setObjectName("codeFind")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)
        self.setVisible(False)

        row = qt.QHBoxLayout(self)
        row.setContentsMargins(qt.px(3), qt.px(4), qt.px(6), qt.px(4))
        row.setSpacing(qt.px(4))

        # the chevron spans both rows on the left, exactly as VS Code lays it out
        self.toggle = self._button("", "Toggle Replace", checkable=True)
        self.toggle.setObjectName("codeFindToggle")
        self.toggle.setIconSize(qt.QSize(qt.px(16), qt.px(16)))
        self.toggle.setFixedWidth(qt.px(22))
        self.toggle.setSizePolicy(qt.QSizePolicy.Fixed, qt.QSizePolicy.Expanding)
        self.toggle.toggled.connect(self._toggle_replace)
        row.addWidget(self.toggle, 0)

        # a GRID, not two independent rows: putting both inputs in the same stretching column is what
        # guarantees they are the same width. Two QHBoxLayouts would each size their field around
        # whatever trailing controls they happen to hold, and the two would never line up.
        rows = qt.QGridLayout()
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setHorizontalSpacing(qt.px(3))
        rows.setVerticalSpacing(qt.px(4))
        rows.setColumnStretch(0, 1)             # only the input column grows
        row.addLayout(rows, 1)

        # ---- find row
        self.field = OptionField("Find")
        self.case = self.field.option("Aa", "Match Case")
        self.word = self.field.option("ab", "Match Whole Word")
        font = self.word.font(); font.setUnderline(True); self.word.setFont(font)
        self.regex = self.field.option(".*", "Use Regular Expression")
        rows.addWidget(self.field, 0, 0)

        self.count = qt.QLabel("No results")
        self.count.setObjectName("codeFindCount")
        self.count.setMinimumWidth(qt.px(70))
        self.count.setAlignment(qt.Qt.AlignRight | qt.Qt.AlignVCenter)
        rows.addWidget(self.count, 0, 1)

        self.previous = self._button("", "Previous Match (Shift+Enter)", mark="up")
        self.next = self._button("", "Next Match (Enter)", mark="down")
        self.in_selection = self._button("", "Find in Selection (Alt+L)", checkable=True,
                                         mark="selection")
        self.close_button = self._button("", "Close (Escape)", mark="close")
        for column, button in enumerate((self.previous, self.next, self.in_selection,
                                         self.close_button), start=2):
            rows.addWidget(button, 0, column)

        # ---- replace row, hidden until the chevron is opened
        self.replace_field = OptionField("Replace")
        self.preserve = self.replace_field.option("AB", "Preserve Case")
        rows.addWidget(self.replace_field, 1, 0)

        self.replace_one = self._button("", "Replace (Enter)", mark="replace")
        self.replace_all = self._button("", "Replace All", mark="replace_all")
        buttons = qt.QWidget()
        # named, or it falls to the generic `QWidget { background: editor }` rule and paints a plate
        # behind the two buttons - a different surface from the panel they sit on
        buttons.setObjectName("codeTransparent")
        strip = qt.QHBoxLayout(buttons)
        strip.setContentsMargins(0, 0, 0, 0)
        strip.setSpacing(qt.px(3))
        strip.addWidget(self.replace_one)
        strip.addWidget(self.replace_all)
        strip.addStretch(1)
        rows.addWidget(buttons, 1, 1, 1, 4)     # spans the trailing columns, field column untouched

        self.replace_row = (self.replace_field, buttons)
        for widget in self.replace_row:
            widget.setVisible(False)

        # ---- wiring
        self.field.edit.textChanged.connect(self.search)
        self.field.edit.returnPressed.connect(self.go_next)
        for option in (self.case, self.word, self.regex):
            option.toggled.connect(self.search)
        self.previous.clicked.connect(self.go_previous)
        self.next.clicked.connect(self.go_next)
        self.in_selection.toggled.connect(self._toggle_range)
        self.close_button.clicked.connect(self.close_panel)
        qt.QShortcut(qt.QKeySequence("Alt+L"), self).activated.connect(self.in_selection.toggle)
        self.replace_field.edit.returnPressed.connect(self.do_replace)
        self.replace_one.clicked.connect(self.do_replace)
        self.replace_all.clicked.connect(self.do_replace_all)
        self.editor.textChanged.connect(self._document_changed)
        self.editor.installEventFilter(self)

        self._refresh_chevron()

    # ------------------------------------------------------------------ construction helpers

    def _button(self, text:str, tip:str, checkable:bool=False, mark:str=None) -> qt.QToolButton:
        button = qt.QToolButton()
        button.setObjectName("codeFindNav")
        if mark:
            button.setIcon(glyph(mark))
            button.setIconSize(qt.QSize(qt.px(16), qt.px(16)))
        button.setText(text)
        button.setToolTip(tip)
        button.setCheckable(checkable)
        button.setAutoRaise(True)                # no plate until hovered; the frame is drawn by the
        button.setFocusPolicy(qt.Qt.NoFocus)     # style otherwise. Never steal focus while typing.
        if text or mark:
            button.setFixedSize(qt.QSize(qt.px(24), qt.px(24)))
        return button

    def _toggle_replace(self, on:bool) -> None:
        for widget in self.replace_row:
            widget.setVisible(on)
        self._refresh_chevron()
        self.adjustSize()
        self.reposition()

    def _refresh_chevron(self) -> None:
        """Use the tree's chevron images: a text arrow renders at whatever size the font decides."""
        self.toggle.setIcon(_icon("chevron_down.png" if self.toggle.isChecked()
                                  else "chevron_right.png"))

    # ------------------------------------------------------------------ find in selection

    def _toggle_range(self, on:bool) -> None:
        """Confine the search to the selection, captured ONCE when the toggle goes on.

        It has to be captured rather than read live: stepping through matches replaces the selection
        with the hit, so a live read would shrink the range to the first match and lose the rest.
        """
        if on:
            cursor = self.editor.textCursor()
            if cursor.hasSelection():
                self._range = (cursor.selectionStart(), cursor.selectionEnd())
            elif self._range is None:
                self.in_selection.blockSignals(True)     # nothing to confine to: refuse quietly
                self.in_selection.setChecked(False)
                self.in_selection.blockSignals(False)
                return
        self.search()

    def _confine(self, matches:list) -> list:
        """Keep only the hits that sit entirely inside the active range."""
        if not (self.in_selection.isChecked() and self._range):
            return matches
        # the document may have shrunk under the range since it was captured
        limit = len(self.editor.toPlainText())
        low, high = min(self._range[0], limit), min(self._range[1], limit)
        return [(start, end) for start, end in matches if start >= low and end <= high]

    # ------------------------------------------------------------------ placement

    def reposition(self) -> None:
        """Pin the panel to the editor's top-right, clear of the scrollbar and the minimap."""
        if not self.isVisible():
            return
        self.adjustSize()
        viewport = self.editor.viewport()
        right = viewport.width()
        strip = getattr(getattr(self.editor, "minimap", None), "width", lambda: 0)()
        if getattr(self.editor, "minimap", None) is not None and self.editor.minimap.isVisible():
            right -= strip
        self.move(max(0, right - self.width() - qt.px(self.MARGIN)), qt.px(4))

    def eventFilter(self, watched, event):
        if watched is self.editor and event.type() in (qt.QEvent.Resize, qt.QEvent.Show):
            self.reposition()
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------------ open / close

    def show_panel(self, replace:bool=False) -> None:
        """Open the panel: seed the query from a one-line selection, scope it to a multi-line one."""
        cursor = self.editor.textCursor()
        selected = cursor.selectedText()
        if selected and u" " in selected:
            # Qt spells a newline U+2029 inside a selection. A selection spanning several lines is a
            # SCOPE, not a query - seeding the field with it could only ever match itself - so it
            # turns Find in Selection on instead, which is what VS Code does with it too.
            self._range = (cursor.selectionStart(), cursor.selectionEnd())
            self.in_selection.blockSignals(True)
            self.in_selection.setChecked(True)
            self.in_selection.blockSignals(False)
        elif selected:
            self.field.edit.setText(selected)
        if replace:
            self.toggle.setChecked(True)
        self.setVisible(True)
        self.reposition()
        self.raise_()
        target = self.replace_field.edit if replace else self.field.edit
        target.setFocus()
        target.selectAll()
        self.search()

    def close_panel(self) -> None:
        """Hide the panel, drop the highlights and give the editor its focus back."""
        self.setVisible(False)
        self.editor.set_search_highlights([])
        self.editor.setFocus()

    def keyPressEvent(self, event) -> None:
        if event.key() == qt.Qt.Key_Escape:
            self.close_panel()
            return
        if event.key() in (qt.Qt.Key_Return, qt.Qt.Key_Enter):
            self.go_previous() if event.modifiers() & qt.Qt.ShiftModifier else self.go_next()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------ searching

    def _document_changed(self) -> None:
        """The text moved under us: re-run the search unless WE are the ones editing."""
        if self.isVisible() and not self._replacing:
            self.search()

    def _pattern(self) -> "re.Pattern":
        """The compiled query, or None when it is empty or an invalid regular expression."""
        text = self.field.edit.text()
        if not text:
            return None
        pattern = text if self.regex.isChecked() else re.escape(text)
        if self.word.isChecked():
            pattern = r"\b%s\b" % pattern
        try:
            return re.compile(pattern, 0 if self.case.isChecked() else re.IGNORECASE)
        except re.error:
            return None

    def search(self, *_) -> None:
        """Find every hit, highlight them, and keep the caret's one current."""
        pattern = self._pattern()
        text = self.editor.toPlainText()
        found = [(m.start(), m.end()) for m in pattern.finditer(text)] if pattern else []
        self.matches = self._confine(found)

        invalid = bool(self.field.edit.text()) and pattern is None
        self.field.setProperty("invalid", invalid)
        self.field.style().unpolish(self.field)
        self.field.style().polish(self.field)

        if self.matches:
            # stay on the hit at or after the caret, so opening the panel does not jump you elsewhere
            position = self.editor.textCursor().selectionStart()
            self.index = next((i for i, (start, _) in enumerate(self.matches) if start >= position), 0)
        else:
            self.index = -1
        self._paint()
        self._update_count(invalid)

    def _paint(self) -> None:
        """Push the hit highlights to the editor, the current one in the brighter tone."""
        selections = []
        for order, (start, end) in enumerate(self.matches):
            selection = qt.QTextEdit.ExtraSelection()
            selection.format.setBackground(qt.QColor(CURRENT_COLOR if order == self.index
                                                     else MATCH_COLOR))
            cursor = self.editor.textCursor()
            cursor.setPosition(start)
            cursor.setPosition(end, qt.QTextCursor.KeepAnchor)
            selection.cursor = cursor
            selections.append(selection)
        self.editor.set_search_highlights(selections)

    def _update_count(self, invalid:bool=False) -> None:
        if invalid:
            self.count.setText("Bad pattern")
        elif not self.field.edit.text():
            self.count.setText("No results")
        elif not self.matches:
            self.count.setText("No results")
        else:
            self.count.setText("%d of %d" % (self.index + 1, len(self.matches)))
        for button in (self.previous, self.next, self.replace_one, self.replace_all):
            button.setEnabled(bool(self.matches))

    # ------------------------------------------------------------------ navigation

    def _go(self, step:int) -> None:
        if not self.matches:
            return
        self.index = (self.index + step) % len(self.matches)
        start, end = self.matches[self.index]
        cursor = self.editor.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, qt.QTextCursor.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()
        self._paint()
        self._update_count()

    def go_next(self) -> None:
        self._go(1)

    def go_previous(self) -> None:
        self._go(-1)

    # ------------------------------------------------------------------ replacing

    def _replacement(self, matched:str) -> str:
        """The replacement text, matching the hit's casing when Preserve Case is on."""
        text = self.replace_field.edit.text()
        if not self.preserve.isChecked() or not matched:
            return text
        if matched.isupper():
            return text.upper()
        if matched[:1].isupper():
            return text[:1].upper() + text[1:]
        return text.lower()

    def do_replace(self) -> None:
        """Replace the current hit, then move to the next one."""
        if not self.matches or self.index < 0:
            return
        start, end = self.matches[self.index]
        cursor = self.editor.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, qt.QTextCursor.KeepAnchor)
        self._replacing = True
        cursor.insertText(self._replacement(cursor.selectedText()))
        self._replacing = False
        self.search()

    def do_replace_all(self) -> None:
        """Replace every hit in one undo step, working backwards so earlier offsets stay valid."""
        if not self.matches:
            return
        cursor = self.editor.textCursor()
        self._replacing = True
        cursor.beginEditBlock()
        try:
            for start, end in reversed(self.matches):
                cursor.setPosition(start)
                cursor.setPosition(end, qt.QTextCursor.KeepAnchor)
                cursor.insertText(self._replacement(cursor.selectedText()))
        finally:
            cursor.endEditBlock()
            self._replacing = False
        self.search()


class OptionField(qt.QWidget):
    """A QLineEdit with small toggle buttons sitting inside it, on the right."""

    def __init__(self, placeholder:str, parent:qt.QWidget=None) -> None:
        super().__init__(parent)
        self.setObjectName("codeFindField")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)
        self.options = []

        layout = qt.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.edit = qt.QLineEdit()
        self.edit.setObjectName("codeFindEdit")
        self.edit.setPlaceholderText(placeholder)
        self.edit.setFrame(False)
        layout.addWidget(self.edit, 1)

    def option(self, text:str, tip:str) -> qt.QToolButton:
        """Add a toggle inside the field, to the right of the text."""
        button = qt.QToolButton(self)
        button.setObjectName("codeFindOption")
        button.setText(text)
        button.setToolTip(tip)
        button.setCheckable(True)
        button.setAutoRaise(True)
        button.setFocusPolicy(qt.Qt.NoFocus)
        button.setFixedSize(qt.QSize(qt.px(21), qt.px(21)))
        self.options.append(button)
        self._reserve()
        return button

    def _reserve(self) -> None:
        """Keep the typed text clear of the buttons that overlap the field."""
        width = sum(b.width() + qt.px(2) for b in self.options) + qt.px(6)
        self.edit.setTextMargins(qt.px(4), 0, width, 0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        x = self.width() - qt.px(4)
        for button in reversed(self.options):
            x -= button.width()
            button.move(x, (self.height() - button.height()) // 2)
            x -= qt.px(2)
