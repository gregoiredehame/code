"""
CODE EDITOR.

Author: Gregoire Dehame
Created: Oct 27, 2023
Modified: Sep 06, 2026
Module: code_editor.core.editor
Execute: from code_editor.core import editor
"""

# self-contained: the editor engine imports nothing from the rest of the host (only its sibling core modules).
from . import qt
from . import compat as kcore          # kcore.folder.read / kcore.message.prompt shims
from . import compat as util           # util.copy / util.scale_dpi shims

import os as _os
__icons__ = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "icons")

import re, os, sys, io, ast, tokenize, builtins, inspect, keyword, traceback
import logging
log = logging.getLogger(__name__)

from functools import partial

__tmp_shearch__ = []

__light__ = True

# Optional surface override (hex string, e.g. "#121314"). When set, the gutter and the current-line
# highlight use it instead of the __light__ defaults, so a host window can match its own theme.
__surface__ = None


_PAIRS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {value: key for key, value in _PAIRS.items()}


def _line_span(text:str, start:int, end:int) -> tuple:
    """The whole lines the range [start, end] touches, without the trailing newline.

    Returns:
        tuple: the (start, end) character offsets of those whole lines.
    """
    left = text.rfind("\n", 0, start) + 1
    right = text.find("\n", end)
    return left, len(text) if right < 0 else right


def _bracket_span(text:str, start:int, end:int) -> tuple:
    """The innermost bracket pair that fully contains [start, end], or None.

    Scans outwards from each side counting depth, so a pair nested inside the range is skipped and
    only a pair that ENCLOSES it is returned. Quotes are not considered: a bracket inside a string
    would need the tokeniser, and getting it wrong is worse than expanding one step further.

    Returns:
        tuple: the (start, end) offsets of the enclosing pair (its contents, then the brackets), or None.
    """
    start = max(0, min(start, len(text)))       # a stale caret can point past a shrunken document
    end = max(start, min(end, len(text)))
    depth, left = 0, start - 1
    while left >= 0:
        char = text[left]
        if char in _CLOSERS:
            depth += 1
        elif char in _PAIRS:
            if depth == 0:
                break
            depth -= 1
        left -= 1
    if left < 0:
        return None

    opener = text[left]
    depth, right = 0, end
    while right < len(text):
        char = text[right]
        if char == opener:
            depth += 1
        elif char == _PAIRS[opener]:
            if depth == 0:
                break
            depth -= 1
        right += 1
    if right >= len(text):
        return None
    # first the contents, then the brackets themselves - two useful steps rather than one
    inner = (left + 1, right)
    return inner if (start, end) != inner else (left, right + 1)


def _surface(fallback:"qt.QColor", override:str=None) -> "qt.QColor":
    """The surface to paint on: a per-widget `override` first, then the module one, then `fallback`.

    The per-widget override exists so the standalone window and the editor embedded in the host
    can run different themes at the same time - a module global alone would let whichever was built
    last decide for both.

    Returns:
        'qt.QColor': the QColor to paint the surface with.
    """
    return qt.QColor(override or __surface__) if (override or __surface__) else fallback

# One persistent namespace shared by every "Execute" in the editor, kept SEPARATE from this module's
# globals so user code can never rebind the editor's own names (re, os, qt, ...) and corrupt the editor.
# It behaves like Maya's own script editor: state persists between runs, __name__ is "__main__".
CONSOLE_NAMESPACE = {"__name__": "__main__", "__builtins__": builtins}

def create_signal(*arg_list) -> qt.signal:
    """Create and return a Qt signal carrying the given argument types.

    Args:
        arg_list: (tuple): - variadic argument types passed to the signal.

    Returns:
        qt.signal: the constructed signal object.
    """
    return qt.signal(*arg_list)


class CodeLineNumber(qt.QWidget):
    """Side widget that renders the line-number gutter for a code editor."""

    def __init__(self, code_editor) -> None:
        """Initialize the line-number widget parented to the given code editor.

        Args:
            code_editor: (CodeTextEdit): - editor whose lines are numbered.

        Returns:
            None.
        """
        super().__init__()
        self.setParent(code_editor)
        self.code_editor = code_editor

    def sizeHint(self) -> qt.QtCore.QSize:
        """Return the preferred size of the gutter based on the editor width.

        Returns:
            qt.QtCore.QSize: preferred size for the gutter widget.
        """
        return qt.QtCore.QSize(self.code_editor.line_number_width(), 0)

    def paintEvent(self, event) -> None:
        """Delegate gutter painting to the parent code editor.

        Args:
            event: (qt.QPaintEvent): - Qt paint event.

        Returns:
            None.
        """
        self.code_editor.line_number_paint(event)

    # the fold arrows live in this gutter, so it owns the mouse for them. They only appear while the
    # pointer is over the gutter - drawn permanently they turn every def into a piece of furniture.
    def enterEvent(self, event) -> None:
        """Flag the gutter as hovered so the fold arrows appear, then chain to the base handler.

        Returns:
            None.
        """
        self.code_editor.set_gutter_hover(True)
        return super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        """Clear the gutter-hover flag so the fold arrows hide, then chain to the base handler.

        Returns:
            None.
        """
        self.code_editor.set_gutter_hover(False)
        return super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        """Forward a gutter click to the editor's fold/change handler, then chain to the base handler.

        Returns:
            None.
        """
        self.code_editor.gutter_clicked(event.pos())
        return super().mousePressEvent(event)


class HunkPopup(qt.QFrame):
    """VS Code's inline diff bubble: what HEAD had at this spot, and a button to put it back.

    A frameless child of the editor rather than a real window, so it scrolls out of the way with the
    text instead of floating over the screen when the editor moves.
    """

    def __init__(self, editor, hunk) -> None:
        """Build the inline diff bubble showing the hunk's HEAD lines with Revert, Copy and close buttons.

        Returns:
            None.
        """
        super().__init__(editor)
        self.editor = editor
        self.hunk = hunk
        self.setObjectName("codeHunkPopup")
        self.setFrameShape(qt.QFrame.NoFrame)
        self.setAttribute(qt.Qt.WA_StyledBackground, True)

        palette = editor.palette_theme or qt.theme()
        kind, old_lines = hunk[2], list(hunk[3])
        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(qt.px(1), qt.px(1), qt.px(1), qt.px(1))
        layout.setSpacing(0)

        row = qt.QHBoxLayout()
        row.setContentsMargins(qt.px(6), qt.px(3), qt.px(4), qt.px(3))
        row.setSpacing(qt.px(4))
        title = qt.QLabel({"added": "Added", "modified": "Modified",
                           "deleted": "Deleted"}.get(kind, "Changed"))
        title.setObjectName("codeHunkTitle")
        row.addWidget(title, 0)
        row.addStretch(1)
        for label, slot in (("Revert", self._revert), ("Copy", self._copy), ("✕", self.close)):
            button = qt.QToolButton()
            button.setObjectName("codeHunkButton")
            button.setText(label)
            button.setAutoRaise(True)
            button.clicked.connect(slot)
            row.addWidget(button, 0)
        layout.addLayout(row, 0)

        if old_lines:
            self.view = qt.QPlainTextEdit("\n".join(old_lines))
            self.view.setObjectName("codeHunkText")
            self.view.setReadOnly(True)
            self.view.setFrameShape(qt.QFrame.NoFrame)
            self.view.setLineWrapMode(qt.QPlainTextEdit.NoWrap)
            self.view.setFont(editor.font())
            rows = min(len(old_lines), 8)        # a long hunk scrolls rather than filling the editor
            self.view.setFixedHeight(rows * qt.QFontMetrics(editor.font()).height() + qt.px(8))
            layout.addWidget(self.view, 1)
        else:
            # an added hunk has no "before": say so instead of showing an empty box
            empty = qt.QLabel("  Nothing here in HEAD - this is new.")
            empty.setObjectName("codeHunkEmpty")
            layout.addWidget(empty, 0)

        self.setStyleSheet("""
            QFrame#codeHunkPopup { background: %(field)s; border: 1px solid %(border)s; }
            QLabel#codeHunkTitle { color: %(dim)s; font-size: 11px; background: transparent; }
            QLabel#codeHunkEmpty { color: %(muted)s; font-size: 11px; background: transparent;
                                   padding: 4px; }
            QToolButton#codeHunkButton { border: none; background: transparent; color: %(text)s;
                                         font-size: 11px; padding: 1px 6px; border-radius: 3px; }
            QToolButton#codeHunkButton:hover { background: %(hover)s; color: %(bright)s; }
            QPlainTextEdit#codeHunkText { background: %(editor)s; color: %(text)s; border: 0px; }
        """ % palette)

    def open_at(self, point) -> None:
        """Show the bubble just under the first line of the hunk.

        Returns:
            None.
        """
        width = max(qt.px(280), self.editor.viewport().width() - self.editor.line_numbers.width()
                    - qt.px(40))
        self.setFixedWidth(width)
        self.adjustSize()
        self.move(point)
        self.show()
        self.raise_()
        self.setFocus()

    def keyPressEvent(self, event) -> None:
        """Close the bubble on Escape, otherwise chain to the base handler.

        Returns:
            None.
        """
        if event.key() == qt.Qt.Key_Escape:
            self.close()
            return
        return super().keyPressEvent(event)

    def _revert(self) -> None:
        """Revert the hunk in the editor, then close the bubble.

        Returns:
            None.
        """
        self.editor.revert_hunk(self.hunk)
        self.close()

    def _copy(self) -> None:
        """Copy the hunk's HEAD lines to the clipboard.

        Returns:
            None.
        """
        qt.QApplication.clipboard().setText("\n".join(self.hunk[3]))

    def closeEvent(self, event) -> None:
        """Clear the editor's popup reference, return focus to it, then chain to the base handler.

        Returns:
            None.
        """
        self.editor._hunk_popup = None
        self.editor.setFocus()
        return super().closeEvent(event)


class CodeMiniMap(qt.QWidget):
    """VS Code-style minimap: a scaled-down colour map of the document, with a viewport indicator.

    Each document line is drawn as a row of tiny coloured bars taken from the syntax highlighter's own
    formats, so the miniature keeps the code's colours and shape. Clicking or dragging scrolls the editor.
    """

    WIDTH  = 84        # design-space width of the strip
    LINE_H = 2         # design-space height of one rendered line
    CHAR_W = 1         # design-space width of one rendered character

    def __init__(self, code_editor) -> None:
        """Build the minimap parented to `code_editor`.

        Returns:
            None.
        """
        super().__init__(code_editor)
        self.code_editor = code_editor
        self._dragging = False
        self.setMouseTracking(True)

    # ---- metrics

    def strip_width(self) -> int:
        """The minimap strip's DPI-scaled pixel width.

        Returns:
            int: the strip width in pixels.
        """
        return max(1, int(util.scale_dpi(self.WIDTH)))

    def _line_height(self) -> int:
        """The DPI-scaled pixel height of one rendered minimap line.

        Returns:
            int: one line's height in pixels.
        """
        return max(1, int(util.scale_dpi(self.LINE_H)))

    def _char_width(self) -> int:
        """The DPI-scaled pixel width of one rendered minimap character.

        Returns:
            int: one character's width in pixels.
        """
        return max(1, int(util.scale_dpi(self.CHAR_W)))

    def _first_line(self, total:int, visible:int) -> int:
        """Top-most document line shown in the strip, tracking the editor's scroll position.

        Returns:
            int: the 0-based number of the top-most visible line.
        """
        if total <= visible:
            return 0
        bar = self.code_editor.verticalScrollBar()
        span = max(1, bar.maximum() - bar.minimum())
        ratio = float(bar.value() - bar.minimum()) / span
        return int((total - visible) * min(1.0, max(0.0, ratio)))

    # ---- painting

    def paintEvent(self, event) -> None:
        """Render the miniature document and the viewport indicator.

        Returns:
            None.
        """
        painter = qt.QPainter(self)
        painter.fillRect(self.rect(),
                         _surface(qt.QColor(43, 43, 43) if __light__ else qt.QColor(30, 30, 30),
                                  getattr(self.code_editor, "surface", None)))

        document = self.code_editor.document()
        total    = document.blockCount()
        line_h   = self._line_height()
        char_w   = self._char_width()
        visible  = max(1, self.height() // line_h)
        first    = self._first_line(total, visible)
        max_x    = self.width()

        neutral = qt.QColor(150, 150, 150, 150)
        block   = document.findBlockByNumber(first)
        y       = 0
        while block.isValid() and y < self.height():
            text = block.text()
            if text.strip():
                ranges = block.layout().formats() if block.layout() else []
                drawn = False
                for entry in ranges or []:
                    start, length = entry.start, entry.length
                    if length <= 0 or start + length > len(text):
                        continue
                    if not text[start:start + length].strip():
                        continue          # whitespace-only run: keep the code's shape
                    x = start * char_w
                    if x >= max_x:
                        continue
                    brush = entry.format.foreground()
                    color = (qt.QColor(brush.color())
                             if brush.style() != qt.Qt.NoBrush else qt.QColor(neutral))
                    color.setAlpha(190)
                    painter.fillRect(x, y, min(length * char_w, max_x - x), line_h - 1, color)
                    drawn = True
                if not drawn:
                    # no highlighter info (plain text): draw the line's ink extent
                    lead = len(text) - len(text.lstrip())
                    x    = lead * char_w
                    w    = max(1, (len(text.rstrip()) - lead)) * char_w
                    if x < max_x:
                        painter.fillRect(x, y, min(w, max_x - x), line_h - 1, neutral)
            y += line_h
            block = block.next()

        # ---- viewport indicator: the slice of the document currently on screen
        top_block = self.code_editor.firstVisibleBlock().blockNumber()
        on_screen = max(1, self.code_editor.viewport().height() //
                        max(1, int(self.code_editor.blockBoundingRect(
                            self.code_editor.firstVisibleBlock()).height() or 1)))
        vy = (top_block - first) * line_h
        vh = on_screen * line_h
        if vy + vh > 0 and vy < self.height():
            indicator = qt.QRect(0, max(0, vy), self.width(), min(vh, self.height() - max(0, vy)))
            painter.fillRect(indicator, qt.QColor(255, 255, 255, 22))

    # ---- interaction

    def _scroll_to(self, y_pos:int) -> None:
        """Scroll the editor so the line under `y_pos` in the strip becomes the top visible line.

        Returns:
            None.
        """
        document = self.code_editor.document()
        total    = document.blockCount()
        line_h   = self._line_height()
        visible  = max(1, self.height() // line_h)
        target   = self._first_line(total, visible) + int(y_pos // line_h)

        bar  = self.code_editor.verticalScrollBar()
        span = max(1, total - 1)
        bar.setValue(bar.minimum() +
                     int((bar.maximum() - bar.minimum()) * min(1.0, max(0.0, float(target) / span))))

    def mousePressEvent(self, event) -> None:
        """Start dragging and scroll the editor to the line clicked in the strip.

        Returns:
            None.
        """
        self._dragging = True
        self._scroll_to(int(event.position().y()) if hasattr(event, "position") else event.y())

    def mouseMoveEvent(self, event) -> None:
        """While dragging, scroll the editor to the line under the pointer in the strip.

        Returns:
            None.
        """
        if self._dragging:
            self._scroll_to(int(event.position().y()) if hasattr(event, "position") else event.y())

    def mouseReleaseEvent(self, event) -> None:
        """End the minimap drag.

        Returns:
            None.
        """
        self._dragging = False

    def wheelEvent(self, event) -> None:
        """Forward wheel scrolling to the editor so the strip behaves like part of it.

        Returns:
            None.
        """
        self.code_editor.wheelEvent(event)


class CodeTextEdit(qt.QPlainTextEdit):
    """Plain-text code editor with line numbers, highlighting, and completion."""

    code_text_size_changed = create_signal(object)
    mouse_pressed = create_signal(object)
    text_has_been_changed = qt.signal(object)
    savingScript = qt.signal(object)
    savingScriptAs = qt.signal(object)      # context menu "Save As...", handled by the host
    changesReverted = qt.signal(object)     # a hunk was put back: only the host can re-ask git

    def __init__(self, file_temp:str=None, file_path:str=None, namespace:dict=None, minimap:bool=False, surface:str=None, palette:dict=None) -> None:
        """Initialize the code editor, loading file content and highlighter.

        Args:
            file_temp: (str):  - path to the temporary autosave file.
            file_path: (str):  - path to the script file being edited.
            namespace: (dict): - execution namespace for this editor. None = the shared the host console
                                 namespace (default, the host behaviour); pass a fresh dict for an
                                 isolated console (e.g. the standalone editor).
            minimap:   (bool): - True adds the VS Code-style minimap strip on the right.
            surface:   (str):  - the code area's background colour.
            palette:   (dict): - the host's full colour set, for the parts that are separate windows
                                 and so cannot inherit its stylesheet - the completion popup.

        Returns:
            None.
        """
        self.file_path = file_path
        self.file_temp = file_temp
        # per-instance execution namespace: isolated when one is given, shared the host console otherwise
        self.namespace = namespace if namespace is not None else CONSOLE_NAMESPACE
        self.shearch_and_replace = []
        self.search_selections = []

        super().__init__()
        # the host may hand us its own surface; the module globals stay the fallback for callers
        # that never set one. Instance-level, because two editors can be open on different themes.
        # Set BEFORE the font restore: apply_font_size rewrites the stylesheet and reads it.
        self.surface = surface
        self.palette_theme = palette

        # Everything the gutter and the extra-selection pass READ lives here, at the very top of
        # __init__. Both run before the end of construction - line_number_highlight() is called while
        # the widgets are still being wired - so an attribute declared further down does not exist
        # yet when they first ask for it.
        self._sticky_map = []                  # [(first, last, kind, header_text, name)], 0-based
        self._fold_headers = []                # QTextBlock handles for the folded regions
        self._folded = {}                      # {first: last}, the cached view of the above
        self._gutter_hover = False
        self._changes = []                     # git hunks: [(first, last, kind, old_lines)]
        self._hunk_popup = None                # the inline diff bubble, at most one at a time
        self._inline = {}                      # {block number: repr} of run-expression results, drawn at line end
        self.extra_cursors = []                # secondary carets; the primary is textCursor()
        self._column_from = None               # Alt press waiting to become a caret or a column
        self._column_moved = False
        # offer the enclosing call's parameters in the completion popup. The host turns this on and
        # off with the QUICK HELP panel, so the two are never out of step.
        self.argument_completion = False
        self.sticky_scroll = True

        self.setFont(qt.QFont('Consolas', 9))
        # restore the font size persisted from a previous session (Ctrl+/- remembers it)
        try:
            import maya.cmds as cmds
            if cmds.optionVar(exists=self.FONT_OPTIONVAR):
                self.apply_font_size(cmds.optionVar(query=self.FONT_OPTIONVAR), persist=False)
        except Exception:
            pass
        self._apply_editor_style()

        plus_seq = qt.QKeySequence(qt.Qt.CTRL | qt.Qt.Key_Plus)
        equal_seq = qt.QKeySequence(qt.Qt.CTRL | qt.Qt.Key_Equal)
        minus_seq = qt.QKeySequence(qt.Qt.CTRL | qt.Qt.Key_Minus)

        shortcut_zoom_in = qt.QShortcut(plus_seq, self)
        shortcut_zoom_in.setContext(qt.Qt.WidgetShortcut)
        shortcut_zoom_in.activated.connect(self.zoom_in_text)
        shortcut_zoom_in_other = qt.QShortcut(equal_seq, self)
        shortcut_zoom_in_other.setContext(qt.Qt.WidgetShortcut)
        shortcut_zoom_in_other.activated.connect(self.zoom_in_text)
        shortcut_zoom_out = qt.QShortcut(minus_seq, self)
        shortcut_zoom_out.setContext(qt.Qt.WidgetShortcut)
        shortcut_zoom_out.activated.connect(self.zoom_out_text)

        self.setWordWrapMode(qt.QTextOption.NoWrap)
        self.line_numbers = CodeLineNumber(self)
        # optional VS Code-style overview strip on the right
        self.minimap = CodeMiniMap(self) if minimap else None
        self.update_number_width(0)
        self.blockCountChanged.connect(self.update_number_width)
        self.updateRequest.connect(self.update_number_area)
        if self.minimap is not None:
            # keep the strip in sync with edits and scrolling
            self.textChanged.connect(self.minimap.update)
            self.verticalScrollBar().valueChanged.connect(self.minimap.update)
        self.cursorPositionChanged.connect(self.line_number_highlight)
        self.cursorPositionChanged.connect(self.auto_scroll_left)
        self.code_text_size_changed.connect(self.code_text_size_change)
        self.line_number_highlight()
        self.completer = None

        # live syntax check (Python only): compile the buffer ~500ms after typing stops and underline the
        # offending line in red. No dependency, no execution — just compile().
        self._lint_line = None                 # 1-based line with a syntax error, or None
        self._lint_timer = qt.QtCore.QTimer(self)
        self._lint_timer.setSingleShot(True)
        self._lint_timer.timeout.connect(self._run_lint)
        self.textChanged.connect(lambda: self._lint_timer.start(500))
        self.textChanged.connect(self._clear_inline)    # an edit shifts line numbers: drop inline results

        # the fold arrows are hit-tested in the gutter, so it has to report the pointer moving
        self.line_numbers.setMouseTracking(True)

        # sticky scroll: the class/def the top of the viewport is inside, pinned above it
        self._lint_timer.timeout.connect(self._rebuild_sticky)
        self.verticalScrollBar().valueChanged.connect(self._repaint_sticky)

        # debounced autosave to the temp file: emit ~400ms after typing stops, not on every keystroke
        # (writing + re-reading the file per character is slow on big files).
        self._autosave_timer = qt.QtCore.QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.timeout.connect(self.auto_temp_save)
        
        if self.file_path:
            if os.path.exists(self.file_path):
                if self.file_path.endswith('.py'):
                    self.setup_highlighter(format='python')
                    
                elif self.file_path.endswith('.mel'):
                    self.setup_highlighter(format='mel')
                    
                else:
                    pass
                    
                self.setPlainText(kcore.folder.read(self.file_path))   # load content (replace, clean undo)
            self.textChanged.connect(lambda: self._autosave_timer.start(400))   # debounced autosave
        else:
            self.setup_highlighter(format='python')

        # only now is there any content to read scopes from; before the load it would map an empty
        # document and the band would stay blank until the lint timer next fired
        self._rebuild_sticky()

        self.setContextMenuPolicy(qt.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.clicked_menu)  

    def clicked_menu(self, position) -> None:
        """Build and display the editor context menu at the given position.

        Args:
            position: (qt.QtCore.QPoint): - local position where the menu is requested.

        Returns:
            None.
        """
        self.menu = qt.QMenu(self)
        self.menu_save   = qt.QAction("Save")
        self.menu_save.setShortcut("Ctrl+S")
        self.menu_save.setIcon(qt.QIcon(os.path.join(__icons__, "disk.png")))
        self.menu_save.triggered.connect(partial(self.clicked_menu_save))

        self.menu_save_as = qt.QAction("Save As...")
        self.menu_save_as.setShortcut("Ctrl+Shift+S")
        self.menu_save_as.setIcon(qt.QIcon(os.path.join(__icons__, "disk.png")))
        self.menu_save_as.triggered.connect(partial(self.clicked_menu_save_as))
        
        self.menu_cut   = qt.QAction("Cut")
        self.menu_cut.setShortcut("Ctrl+X")
        self.menu_cut.setIcon(qt.QIcon(os.path.join(__icons__, "cut.png")))
        self.menu_cut.triggered.connect(partial(self.clicked_menu_cut))
        
        self.menu_copy  = qt.QAction("Copy")
        self.menu_copy.setShortcut("Ctrl+C")
        self.menu_copy.setIcon(qt.QIcon(os.path.join(__icons__, "copy.png")))
        self.menu_copy.triggered.connect(partial(self.clicked_menu_copy))
        
        self.menu_paste = qt.QAction("Paste")
        self.menu_paste.setShortcut("Ctrl+V")
        self.menu_paste.setIcon(qt.QIcon(os.path.join(__icons__, "paste.png")))
        self.menu_paste.triggered.connect(partial(self.clicked_menu_paste))
        
        self.menu_select = qt.QAction("Select All")
        self.menu_select.setShortcut("Ctrl+A")
        self.menu_select.setIcon(qt.QIcon(os.path.join(__icons__, "select.png")))
        self.menu_select.triggered.connect(partial(self.clicked_menu_select))
        
        self.menu_execute = qt.QAction("Execute")
        self.menu_execute.setShortcut("Ctrl+Return")
        self.menu_execute.triggered.connect(partial(self.clicked_menu_execute))
        
        self.menu_execute_line = qt.QAction("Execute Line")
        self.menu_execute_line.setShortcut("Ctrl+Alt+Return")
        self.menu_execute_line.triggered.connect(partial(self.clicked_menu_execute_line))
        
        self.menu_execute_all = qt.QAction("Execute All")
        self.menu_execute_all.setShortcut("Ctrl+Shift+Return")
        self.menu_execute_all.triggered.connect(partial(self.clicked_menu_execute_all))
        
        self.menu_save_to_shelf = qt.QAction("Save Script To Shelf")
        self.menu_save_to_shelf.triggered.connect(partial(self.clicked_save_to_shelf))

        self.menu_command_reference = qt.QAction("Command Reference")
        self.menu_command_reference.setIcon(qt.QIcon(os.path.join(__icons__, "internet.png")))
        self.menu_command_reference.triggered.connect(partial(self.clicked_command_reference))
        
        self.menu.addAction(self.menu_save)
        self.menu.addAction(self.menu_save_as)
        self.menu.addSeparator()
        self.menu.addAction(self.menu_cut)
        self.menu.addAction(self.menu_copy)
        self.menu.addAction(self.menu_paste)
        self.menu.addSeparator()
        self.menu.addAction(self.menu_select)
        self.menu.addSeparator()
        self.menu.addAction(self.menu_execute)
        self.menu.addAction(self.menu_execute_line)
        self.menu.addAction(self.menu_execute_all)
        self.menu.addSeparator()
        self.menu.addAction(self.menu_save_to_shelf)
        self.menu.addSeparator()
        
        try:
            import maya.cmds as cmds
            command = self.textCursor().selection().toPlainText()
            try: 
                help_string = cmds.help(command)
                self.quick_menu = self.menu.addMenu('Quick Help')
                self.quick_menu.setIcon(qt.QIcon(os.path.join(__icons__, "shearch.png")))
                self.quick_help = qt.QTextEdit()
                self.quick_help.setFont(qt.QFont('Consolas', 9))
                self.quick_help.append(f'"{command}" %s'%'\n'.join(help_string.split('\n')[3:-4]))
                self.quick_help.setReadOnly(True)
                self.quick_help.setMinimumWidth(500)
                self.quick_action = qt.QWidgetAction(self)
                self.quick_action.setDefaultWidget(self.quick_help)
                self.quick_menu.addAction(self.quick_action)
                self.menu.addAction(self.menu_command_reference)
                self.menu.addSeparator()
            except Exception:
                pass                          # no Maya help for this word -> just skip the quick-help entry
        except Exception:
            log.exception("code editor: failed to build the context menu quick-help")
        
        self.menu.exec_(self.mapToGlobal(position))


    def paintEvent(self, event) -> None:
        """Paint the editor and overlay indentation guide lines.

        Args:
            event: (qt.QPaintEvent): - Qt paint event.

        Returns:
            None.
        """
        super().paintEvent(event)

        painter = qt.QPainter(self.viewport())

        pen = qt.QPen(qt.QColor(60, 60, 60).lighter(150) if __light__ else qt.QColor(30, 30, 30).lighter(225), 1)
        painter.setPen(pen)
        space_width = self.fontMetrics().horizontalAdvance(' ')

        block = self.firstVisibleBlock()
        
        while block.isValid():
            block_rect = self.blockBoundingGeometry(block).translated(self.contentOffset())
            if block.isVisible():
                block_text = block.text()

                first_non_space_index = len(block_text) - len(block_text.lstrip())
                
                for i in range(0, first_non_space_index, 4):
                    indentation_x = block_rect.left() + i * space_width
                    painter.drawLine(indentation_x, block_rect.top(), indentation_x, block_rect.bottom())
                    
                if self._inline and block.blockNumber() in self._inline:   # run-expression result, at line end
                    painter.save()
                    painter.setPen(qt.QColor(120, 120, 120))
                    inline_font = qt.QFont(self.font())
                    inline_font.setItalic(True)
                    painter.setFont(inline_font)
                    end_x = block_rect.left() + self.fontMetrics().horizontalAdvance(block_text)
                    baseline = int(block_rect.top() + self.fontMetrics().ascent())
                    painter.drawText(int(end_x + qt.px(24)), baseline, "→  " + self._inline[block.blockNumber()])
                    painter.restore()

            block = block.next()

        painter.end()
        self._paint_extra_carets()
        # last, so the band covers the text and the guides rather than being drawn under them
        self._paint_sticky()
             
                
    # ------------------------------------------------------------------ sticky scroll
    STICKY_MAX = 5             # VS Code's default cap; deeper than this the band eats the viewport

    def _rebuild_sticky(self) -> None:
        """Re-read the file's class/def ranges from its syntax tree.

        The PREVIOUS map is kept when the buffer does not parse. Half-typed code is unparseable most
        of the time you are editing it, and a band that blanked out on every other keystroke would be
        worse than one showing a slightly stale name.

        Returns:
            None.
        """
        if not self.sticky_scroll or (getattr(self, "file_path", "") or "").endswith(".mel"):
            self._sticky_map = []
            return
        text = self.toPlainText()
        try:
            tree = ast.parse(text)
        except Exception:
            return                                  # keep what we had
        lines = text.split("\n")
        entries = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            last = getattr(node, "end_lineno", None)
            if last is None:
                continue
            first = node.lineno - 1
            if first >= len(lines):
                continue
            kind = "class" if isinstance(node, ast.ClassDef) else "def"
            entries.append((first, last - 1, kind, lines[first].rstrip(), node.name))
        entries.sort()
        self._sticky_map = entries
        self._revalidate_folds()
        self._repaint_sticky()

    def _revalidate_folds(self) -> None:
        """Re-apply the folds against the freshly read regions, if anything moved.

        The headers are blocks, so they follow the edit on their own; what can change is where each
        region ENDS - typing a line into a function makes its body longer. Re-applying is cheap and
        only happens when the result would actually differ.

        Returns:
            None.
        """
        if not self._fold_headers:
            return
        regions = self.fold_regions()
        wanted = {}
        for header in self._fold_headers:
            if header.isValid() and header.blockNumber() in regions:
                wanted[header.blockNumber()] = regions[header.blockNumber()]
        if wanted != self._folded:
            self._apply_folds()

    def _sticky_chain(self) -> list:
        """The definitions enclosing the top of the viewport, outermost first.

        A definition whose own header is at or below the top line is left out: it is on screen where
        it belongs, and repeating it in the band would show the same line twice.

        Returns:
            list: the enclosing definition entries, outermost first.
        """
        if not self.sticky_scroll or not self._sticky_map:
            return []
        block = self.firstVisibleBlock()
        if not block.isValid():
            return []
        top = block.blockNumber()
        chain = [entry for entry in self._sticky_map if entry[0] < top <= entry[1]]
        return chain[-self.STICKY_MAX:]              # innermost ones win when nesting runs deep

    def _sticky_height(self) -> int:
        """Pixel height of the band, 0 when there is nothing to pin.

        Returns:
            int: the band's height in pixels.
        """
        chain = self._sticky_chain()
        if not chain:
            return 0
        return len(chain) * self.fontMetrics().height()

    def _repaint_sticky(self) -> None:
        """Refresh just the top strip - scrolling must not repaint the whole viewport for this.

        Returns:
            None.
        """
        if not self.sticky_scroll:
            return
        height = max(self._sticky_height(), getattr(self, "_sticky_last_height", 0))
        self._sticky_last_height = self._sticky_height()
        if height:
            self.viewport().update(0, 0, self.viewport().width(), height + 2)

    def _paint_sticky(self) -> None:
        """Draw the pinned definitions over the top of the viewport.

        Each row is the BLOCK'S OWN text layout, drawn where we want it rather than re-rendered by
        hand. That layout already carries the formats the syntax highlighter applied, so a pinned
        line is coloured exactly like the real one - keywords, the name, the argument types, the
        bracket-pair tints, all of it. Painting the text ourselves only ever approximated that, and
        the approximation showed.

        Returns:
            None.
        """
        chain = self._sticky_chain()
        if not chain:
            return
        painter = qt.QPainter(self.viewport())
        metrics = self.fontMetrics()
        row_height = metrics.height()
        # right across the viewport: the minimap is a sibling widget drawn AFTER it, so the band
        # simply runs underneath instead of stopping short and leaving a notch
        width = self.viewport().width()
        band_height = row_height * len(chain)

        # the code surface, not the lighter panel one: the band is part of the editor, and a paler
        # strip across the top read as a separate widget sitting on it
        palette = self.palette_theme or qt.theme()
        painter.fillRect(0, 0, width, band_height, _surface(qt.QColor(30, 30, 30), self.surface))

        painter.setClipRect(0, 0, width, band_height)
        offset = self.contentOffset().x()                  # follow horizontal scrolling, as text does
        document = self.document()
        for row, entry in enumerate(chain):
            block = document.findBlockByNumber(entry[0])
            if not block.isValid():
                continue
            # a sticky line is by definition ABOVE the viewport, and QPlainTextEdit lays blocks out
            # lazily: ask for its rect first, or the layout would still be empty
            document.documentLayout().blockBoundingRect(block)
            painter.setPen(qt.QColor(palette["text"]))     # the colour for whatever is unformatted
            layout = block.layout()
            if layout.lineCount():
                layout.draw(painter, qt.QPointF(offset, row * row_height))
            else:
                painter.drawText(int(offset), row * row_height + metrics.ascent(), entry[3])
        painter.setClipping(False)

        painter.setPen(qt.QColor(palette["rule"]))
        painter.drawLine(0, band_height, width, band_height)
        painter.end()

    def _sticky_hit(self, point) -> int:
        """The line a click in the band points at, or -1 when the click is not in the band.

        Returns:
            int: the 0-based line clicked in the band, or -1.
        """
        chain = self._sticky_chain()
        if not chain or point.y() >= len(chain) * self.fontMetrics().height():
            return -1
        row = min(len(chain) - 1, point.y() // self.fontMetrics().height())
        return chain[row][0]

    def go_to_block(self, line:int, top:bool=True) -> None:
        """Put the caret on `line` (0-based), at the top of the viewport or centred.

        For the sticky band the scroll bar is set directly rather than using centerCursor(): word
        wrap is off, so its value IS the first visible block number, and the definition you clicked
        lands where you clicked it instead of jumping to the middle of the screen. A jump you did not
        aim with the mouse - Go to Definition - reads better centred, as it does in VS Code.

        Returns:
            None.
        """
        self.ensure_visible(line)                # never land the caret on a folded-away block
        block = self.document().findBlockByNumber(max(0, line))
        if not block.isValid():
            return
        cursor = self.textCursor()
        cursor.setPosition(block.position())
        self.setTextCursor(cursor)
        # the scroll bar counts VISIBLE lines, so its value equals the block number only while
        # nothing is folded; with folds in play, centring is the honest answer
        if top and not self._folded:
            self.verticalScrollBar().setValue(max(0, line))
        else:
            self.centerCursor()

    # ------------------------------------------------------------------ folding
    FOLD_COLUMN = 14           # design-space width of the arrow strip at the right of the gutter

    def fold_regions(self) -> dict:
        """{first_line: last_line} for every class/def worth a fold arrow.

        Built from the same scope map the sticky band and Go to Definition use, so a region always
        ends exactly where its body does - guessing the end from indentation would swallow the blank
        lines and comments that follow a function.

        Returns:
            dict: {first_line: last_line} for each foldable class/def region.
        """
        return {entry[0]: entry[1] for entry in self._sticky_map if entry[1] > entry[0]}

    def set_gutter_hover(self, state:bool) -> None:
        """Show or hide the fold arrows, which only appear under the pointer.

        Returns:
            None.
        """
        if getattr(self, "_gutter_hover", False) != bool(state):
            self._gutter_hover = bool(state)
            self.line_numbers.update()

    def line_at(self, y:int) -> int:
        """The line number at gutter coordinate `y`, or -1. Folded blocks measure 0 and are skipped.

        Returns:
            int: the line number at that y, or -1.
        """
        block = self.firstVisibleBlock()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        while block.isValid() and top <= y:
            height = self.blockBoundingRect(block).height()
            if block.isVisible() and top <= y <= top + height:
                return block.blockNumber()
            top += height
            block = block.next()
        return -1

    CHANGE_STRIP = 8           # design-space width of the git bar at the very left of the gutter

    def gutter_clicked(self, point) -> None:
        """Three strips: the change bar on the left, the numbers (inert), the fold arrows on the right.

        Returns:
            None.
        """
        line = self.line_at(point.y())
        if line < 0:
            return
        if point.x() <= qt.px(self.CHANGE_STRIP):
            self.show_hunk(line)
        elif point.x() >= self.line_numbers.width() - qt.px(self.FOLD_COLUMN):
            self.toggle_fold(line)

    def _apply_folds(self) -> None:
        """Make the document's block visibility match the folded headers.

        Every block is shown, then the body of each folded region is hidden again. Doing it wholesale
        rather than incrementally is what makes nesting come out right: a region inside another one
        must stay collapsed when the outer one opens, and tracking that by hand needs the same pass
        anyway.

        Returns:
            None.
        """
        document = self.document()
        regions = self.fold_regions()
        self._fold_headers = [b for b in self._fold_headers if b.isValid()]

        folded = {}
        for header in self._fold_headers:
            last = regions.get(header.blockNumber())
            if last is not None:
                folded[header.blockNumber()] = last
        self._folded = folded

        block = document.begin()
        while block.isValid():
            block.setVisible(True)
            block = block.next()
        for first, last in folded.items():
            block = document.findBlockByNumber(first + 1)
            while block.isValid() and block.blockNumber() <= last:
                block.setVisible(False)
                block = block.next()

        # QPlainTextEdit caches the layout: without marking the text dirty the hidden lines keep
        # their old geometry and what follows is drawn over itself
        document.markContentsDirty(0, max(1, document.characterCount()))
        layout = document.documentLayout()
        if hasattr(layout, "requestUpdate"):
            layout.requestUpdate()
        self.update_number_width()
        self.viewport().update()
        self.line_numbers.update()

    def fold(self, first:int) -> None:
        """Collapse the region whose header is on line `first`.

        Returns:
            None.
        """
        if first in self._folded or first not in self.fold_regions():
            return
        block = self.document().findBlockByNumber(first)
        if block.isValid():
            # the BLOCK is remembered, not the line number: editing anywhere above shifts every
            # number below it, and folds keyed by number would all be released on the next keystroke
            self._fold_headers.append(block)
            self._apply_folds()

    def unfold(self, first:int) -> None:
        """Expand the region whose header is on line `first`.

        Returns:
            None.
        """
        self._fold_headers = [b for b in self._fold_headers
                              if not (b.isValid() and b.blockNumber() == first)]
        self._apply_folds()

    def toggle_fold(self, line:int) -> None:
        """Fold the region at `line`, or unfold it if it already is.

        Returns:
            None.
        """
        self.unfold(line) if line in self._folded else self.fold(line)

    def fold_at_cursor(self, close:bool=True) -> None:
        """Fold (or unfold) the innermost region around the caret.

        Returns:
            None.
        """
        line = self.textCursor().blockNumber()
        holding = [first for first, last in self.fold_regions().items() if first <= line <= last]
        if not holding:
            return
        first = max(holding)                        # the innermost region starts latest
        self.fold(first) if close else self.unfold(first)

    def fold_all(self) -> None:
        """Collapse every region.

        Returns:
            None.
        """
        document = self.document()
        self._fold_headers = [document.findBlockByNumber(first) for first in self.fold_regions()]
        self._apply_folds()

    def unfold_all(self) -> None:
        """Expand everything.

        Returns:
            None.
        """
        self._fold_headers = []
        self._apply_folds()

    def ensure_visible(self, line:int) -> None:
        """Open whatever is hiding `line`, so a jump never lands on an invisible block.

        Returns:
            None.
        """
        hiding = [first for first, last in self._folded.items() if first < line <= last]
        if hiding:
            self._fold_headers = [b for b in self._fold_headers
                                  if not (b.isValid() and b.blockNumber() in hiding)]
            self._apply_folds()

    def _paint_fold_arrow(self, painter, x:int, y:int, size:int, folded:bool) -> None:
        """A chevron: pointing down when the region is open, right when it is collapsed.

        Returns:
            None.
        """
        painter.save()
        pen = qt.QPen(qt.QColor(140, 140, 140), max(1, qt.px(1)))
        pen.setCapStyle(qt.Qt.RoundCap)
        pen.setJoinStyle(qt.Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(qt.Qt.NoBrush)
        step = size / 4.0
        centre_x, centre_y = x + size / 2.0, y + size / 2.0
        if folded:
            points = [qt.QPointF(centre_x - step, centre_y - step * 1.6),
                      qt.QPointF(centre_x + step, centre_y),
                      qt.QPointF(centre_x - step, centre_y + step * 1.6)]
        else:
            points = [qt.QPointF(centre_x - step * 1.6, centre_y - step),
                      qt.QPointF(centre_x, centre_y + step),
                      qt.QPointF(centre_x + step * 1.6, centre_y - step)]
        painter.drawPolyline(points)
        painter.restore()

    # ------------------------------------------------------------------ multiple cursors
    def clear_extra_cursors(self) -> None:
        """Drop back to a single caret.

        Returns:
            None.
        """
        if self.extra_cursors:
            self.extra_cursors = []
            self.line_number_highlight()
            self.viewport().update()

    def _all_cursors(self) -> list:
        """Every caret, primary included, furthest down the document first.

        Back to front so an edit never moves a caret that has not been served yet. QTextCursor does
        keep itself in step with the document on its own, but relying on that AND on ordering would
        make a bug here twice as hard to see.

        Returns:
            list: every caret, primary included, furthest down the document first.
        """
        return sorted([self.textCursor()] + list(self.extra_cursors),
                      key=lambda c: c.position(), reverse=True)

    def add_cursor(self, cursor) -> None:
        """Add a caret, or remove the one already sitting there (Alt+click toggles).

        Returns:
            None.
        """
        for existing in list(self.extra_cursors):
            if existing.position() == cursor.position():
                self.extra_cursors.remove(existing)
                break
        else:
            if cursor.position() != self.textCursor().position():
                self.extra_cursors.append(qt.QTextCursor(cursor))
        self.line_number_highlight()
        self.viewport().update()

    def add_cursor_vertically(self, step:int) -> None:
        """Ctrl+Alt+Up / Down: a caret on the line above or below the lowest (or highest) one.

        Returns:
            None.
        """
        edge = max(self._all_cursors(), key=lambda c: c.blockNumber()) if step > 0 else \
            min(self._all_cursors(), key=lambda c: c.blockNumber())
        column = edge.positionInBlock()
        block = edge.block().next() if step > 0 else edge.block().previous()
        while block.isValid() and not block.isVisible():
            block = block.next() if step > 0 else block.previous()
        if not block.isValid():
            return
        cursor = qt.QTextCursor(block)
        cursor.setPosition(block.position() + min(column, max(0, block.length() - 1)))
        self.add_cursor(cursor)

    def select_next_occurrence(self, everything:bool=False) -> None:
        """Ctrl+D: select the word, then add a caret on each following match. Ctrl+Shift+L: all.

        Returns:
            None.
        """
        cursor = self.textCursor()
        if not cursor.hasSelection() and not self.extra_cursors:
            cursor.select(qt.QTextCursor.WordUnderCursor)
            if cursor.hasSelection():
                self.setTextCursor(cursor)      # the first press only selects, as in VS Code
                if not everything:
                    return
        needle = self.textCursor().selectedText()
        if not needle:
            return

        text = self.toPlainText()
        taken = {(c.selectionStart(), c.selectionEnd())
                 for c in [self.textCursor()] + list(self.extra_cursors)}
        starts = []
        if everything:
            at = text.find(needle)
            while at >= 0:
                starts.append(at)
                at = text.find(needle, at + 1)
        else:
            begin = max(c.selectionEnd() for c in [self.textCursor()] + list(self.extra_cursors))
            at = text.find(needle, begin)
            if at < 0:
                at = text.find(needle)          # wrap round to the top of the file
            if at >= 0:
                starts.append(at)

        for at in starts:
            if (at, at + len(needle)) in taken:
                continue
            spot = self.textCursor()
            spot.setPosition(at)
            spot.setPosition(at + len(needle), qt.QTextCursor.KeepAnchor)
            self.extra_cursors.append(spot)
        self.line_number_highlight()
        self.viewport().update()

    def _multi_selections(self) -> list:
        """The highlight behind each extra caret's selection.

        Returns:
            list: an extra selection for each extra caret that has a selection.
        """
        palette = self.palette_theme or qt.theme()
        tint = qt.QColor(palette["textsel"])
        out = []
        for cursor in self.extra_cursors:
            if cursor.hasSelection():
                selection = qt.QTextEdit.ExtraSelection()
                selection.format.setBackground(tint)
                selection.cursor = cursor
                out.append(selection)
        return out

    def _paint_extra_carets(self) -> None:
        """Draw the extra carets: Qt only ever renders the one real cursor.

        Returns:
            None.
        """
        if not self.extra_cursors:
            return
        painter = qt.QPainter(self.viewport())
        palette = self.palette_theme or qt.theme()
        painter.setPen(qt.Qt.NoPen)
        painter.setBrush(qt.QColor(palette["bright"]))
        for cursor in self.extra_cursors:
            rect = self.cursorRect(cursor)
            painter.drawRect(rect.x(), rect.y(), max(1, qt.px(1)), rect.height())
        painter.end()

    def _multi_key(self, event) -> bool:
        """Apply `event` to every caret. False means "not mine", and the normal path takes over.

        Returns:
            bool: True if the event was applied to the carets, False to fall through.
        """
        key = event.key()
        modifiers = event.modifiers()
        if key == qt.Qt.Key_Escape:
            self.clear_extra_cursors()
            return True
        # anything with Ctrl is a command, not typing - let the shortcuts have it
        if modifiers & qt.Qt.ControlModifier and key not in (qt.Qt.Key_Backspace, qt.Qt.Key_Delete):
            return False

        moves = {qt.Qt.Key_Left: qt.QTextCursor.Left, qt.Qt.Key_Right: qt.QTextCursor.Right,
                 qt.Qt.Key_Up: qt.QTextCursor.Up, qt.Qt.Key_Down: qt.QTextCursor.Down,
                 qt.Qt.Key_Home: qt.QTextCursor.StartOfLine, qt.Qt.Key_End: qt.QTextCursor.EndOfLine}
        mode = (qt.QTextCursor.KeepAnchor if modifiers & qt.Qt.ShiftModifier
                else qt.QTextCursor.MoveAnchor)

        if key in moves:
            primary = self.textCursor()
            primary.movePosition(moves[key], mode)
            self.setTextCursor(primary)
            for cursor in self.extra_cursors:
                cursor.movePosition(moves[key], mode)
            self.line_number_highlight()
            self.viewport().update()
            return True

        text = event.text()
        if key in (qt.Qt.Key_Backspace, qt.Qt.Key_Delete) or (text and text.isprintable()) \
                or key in (qt.Qt.Key_Return, qt.Qt.Key_Enter):
            primary = self.textCursor()
            primary.beginEditBlock()             # one undo step for the whole multi-edit
            try:
                for cursor in self._all_cursors():
                    if key == qt.Qt.Key_Backspace:
                        cursor.removeSelectedText() if cursor.hasSelection() \
                            else cursor.deletePreviousChar()
                    elif key == qt.Qt.Key_Delete:
                        cursor.removeSelectedText() if cursor.hasSelection() else cursor.deleteChar()
                    elif key in (qt.Qt.Key_Return, qt.Qt.Key_Enter):
                        cursor.insertText("\n")
                    else:
                        cursor.insertText(text)
            finally:
                primary.endEditBlock()
            self.setTextCursor(primary)
            self.line_number_highlight()
            self.viewport().update()
            return True
        return False

    # ------------------------------------------------------------------ git changes
    CHANGE_COLOURS = {"added": "#487e02", "modified": "#0c7d9d", "deleted": "#a31515"}

    def set_changes(self, hunks:list) -> None:
        """Record what git says changed: [(first, last, kind, old_lines)], 0-based.

        `deleted` marks the line the removed text used to sit above, so `last` equals `first` there -
        there is no line left to bar, only a place to point at.

        Returns:
            None.
        """
        self._changes = list(hunks or [])
        self.line_numbers.update()

    def hunk_at(self, line:int) -> tuple:
        """The change covering `line`, or None.

        Returns:
            tuple: the hunk covering that line, or None.
        """
        for hunk in self._changes:
            if hunk[0] <= line <= hunk[1]:
                return hunk
        return None

    def revert_hunk(self, hunk) -> None:
        """Put HEAD's version of this hunk back, in one undo step.

        Returns:
            None.
        """
        first, last, kind, old_lines = hunk[0], hunk[1], hunk[2], list(hunk[3])
        document = self.document()
        cursor = self.textCursor()
        cursor.beginEditBlock()
        try:
            if kind == "deleted":
                # nothing to replace: the removed lines go back UNDER the line they vanished from
                block = document.findBlockByNumber(first)
                cursor.setPosition(block.position() + max(0, block.length() - 1))
                cursor.insertText("\n" + "\n".join(old_lines))
            else:
                start = document.findBlockByNumber(first)
                end = document.findBlockByNumber(min(last, document.blockCount() - 1))
                cursor.setPosition(start.position())
                cursor.setPosition(end.position() + max(0, end.length() - 1),
                                   qt.QTextCursor.KeepAnchor)
                cursor.insertText("\n".join(old_lines))
        finally:
            cursor.endEditBlock()
        self.setTextCursor(cursor)
        self.changesReverted.emit(self)     # only the host can ask git for the new hunk list

    def show_hunk(self, line:int) -> None:
        """Open the peek bubble on the change at `line`: what HEAD had, and a way to put it back.

        Returns:
            None.
        """
        hunk = self.hunk_at(line)
        if hunk is None:
            return
        if self._hunk_popup is not None:
            self._hunk_popup.close()
        self._hunk_popup = HunkPopup(self, hunk)
        block = self.document().findBlockByNumber(hunk[0])
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).bottom()
        self._hunk_popup.open_at(qt.QPoint(self.line_numbers.width(), int(top)))

    def step_change(self, step:int) -> bool:
        """Move the caret to the next (or previous) changed hunk. False when there are none.

        Returns:
            bool: True if the caret moved, False when there are no hunks.
        """
        if not self._changes:
            return False
        line = self.textCursor().blockNumber()
        starts = sorted(hunk[0] for hunk in self._changes)
        ahead = [n for n in starts if n > line] if step > 0 else [n for n in starts if n < line]
        # wrap round rather than stopping dead at the last hunk, as VS Code does
        target = (ahead[0] if step > 0 else ahead[-1]) if ahead else (starts[0] if step > 0
                                                                     else starts[-1])
        self.go_to_block(target, top=False)
        return True

    def _paint_change_bar(self, painter, y:int, height:int, kind:str) -> None:
        """The coloured bar git puts down the left edge of a changed line.

        Returns:
            None.
        """
        colour = qt.QColor(self.CHANGE_COLOURS.get(kind, self.CHANGE_COLOURS["modified"]))
        width = max(2, qt.px(3))
        if kind == "deleted":
            # nothing survives to underline: a small wedge marks where the text was taken out
            painter.setPen(qt.Qt.NoPen)
            painter.setBrush(colour)
            painter.drawPolygon(qt.QPointF(0, y + height / 2.0 - qt.px(3)),
                                qt.QPointF(width * 2.0, y + height / 2.0),
                                qt.QPointF(0, y + height / 2.0 + qt.px(3)))
            return
        painter.fillRect(qt.QRect(0, int(y), width, int(height)), colour)

    # ------------------------------------------------------------------ call context
    CALL_SCAN_LINES = 60       # how far back a call's opening bracket is looked for

    @staticmethod
    def code_mask(line:str) -> list:
        """True for each character of `line` that is real code, False inside a string or a comment.

        Scanned per line, so a triple-quoted block spanning lines reads as code. That is a knowing
        trade: the alternative is parsing the whole file on every keystroke, and this only has to be
        right about the line you are currently typing a call on.

        Returns:
            list: one bool per character of `line`, True for real code.
        """
        mask, quote = [], None
        for char in line:
            if quote:
                mask.append(False)
                if char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
                mask.append(False)
            elif char == "#":
                mask.extend([False] * (len(line) - len(mask)))
                break
            else:
                mask.append(True)
        mask.extend([True] * (len(line) - len(mask)))
        return mask

    def call_context(self) -> tuple:
        """(callable_name, argument_index) for the call the caret sits inside, or None.

        Found by walking back over CODE characters only, so a bracket inside a string or a comment
        never opens a call that is not there.

        Returns:
            tuple: (callable_name, argument_index) for the enclosing call, or None.
        """
        cursor = self.textCursor()
        current = cursor.blockNumber()
        first = max(0, current - self.CALL_SCAN_LINES)
        document = self.document()

        text, code = "", []
        for number in range(first, current + 1):
            line = document.findBlockByNumber(number).text()
            if number == current:
                line = line[:cursor.positionInBlock()]
            text += line + "\n"
            code.extend(self.code_mask(line) + [True])
        caret = len(text) - 1                       # the trailing newline we just added

        depth, opened = 0, -1
        for index in range(caret - 1, -1, -1):
            if not code[index]:
                continue
            char = text[index]
            if char in ")]}":
                depth += 1
            elif char in "([{":
                if depth == 0:
                    if char != "(":
                        return None                 # a list or a dict, not a call
                    opened = index
                    break
                depth -= 1
        if opened < 0:
            return None

        # which argument: commas at the call's own bracket level, nested ones skipped
        argument, nested = 0, 0
        for index in range(opened + 1, caret):
            if not code[index]:
                continue
            char = text[index]
            if char in "([{":
                nested += 1
            elif char in ")]}":
                nested -= 1
            elif char == "," and nested == 0:
                argument += 1

        name = re.search(r"([A-Za-z_][\w.]*)$", text[:opened])
        return (name.group(1), argument) if name else None

    # what an unqualified name in someone's script almost always means
    ALIASES = {"cmds": "maya.cmds", "mc": "maya.cmds", "mel": "maya.mel",
               "om": "maya.api.OpenMaya", "OpenMaya": "maya.api.OpenMaya"}

    def insert_argument(self, name:str) -> None:
        """Type `name` into the call the caret is in, adding the separator only if one is needed.

        What comes before the caret decides: straight after the opening bracket nothing is added,
        after another argument a ", " is, and after a comma just the space. Getting that wrong is
        what makes an "insert" button more work than typing it.

        Returns:
            None.
        """
        if not name:
            return
        cursor = self.textCursor()
        raw = cursor.block().text()[:cursor.positionInBlock()]
        before = raw.rstrip()
        if not before or before.endswith("("):
            lead = ""                        # first argument, or a fresh continuation line
        elif before.endswith(","):
            lead = "" if raw != before else " "   # a space is already typed after the comma
        else:
            lead = ", "
        cursor.insertText("%s%s" % (lead, name))
        self.setTextCursor(cursor)
        self.setFocus()

    def resolve(self, name:str) -> object:
        """The live object `name` refers to, or None. A lookup - nothing is ever called.

        The editor's execution namespace is only half the answer. A standalone window runs an
        ISOLATED namespace, so `cmds` is not in it until you have run the import yourself - and
        asking for help on `cmds.circle` before importing anything is exactly when you want it. So a
        failed lookup falls back to importing the head of the name, aliases included.

        Returns:
            object: the live object `name` refers to, or None.
        """
        namespace = dict(vars(builtins))
        try:
            namespace.update(self.namespace)
        except Exception:
            pass
        try:
            return eval(name, namespace)
        except Exception:
            pass

        head = name.split(".")[0]
        module = self.ALIASES.get(head, head)
        try:
            import importlib
            namespace[head] = importlib.import_module(module)
            return eval(name, namespace)
        except Exception:
            return None

    # ------------------------------------------------------------------ line transforms
    def _selected_lines(self) -> tuple:
        """(first, last) whole lines covered by the selection, or the caret's line on its own.

        Returns:
            tuple: the (first, last) line numbers covered.
        """
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return cursor.blockNumber(), cursor.blockNumber()
        document = self.document()
        first = document.findBlock(cursor.selectionStart()).blockNumber()
        end = document.findBlock(cursor.selectionEnd())
        # a selection stopping at column 0 does NOT include that line: dragging down to the start of
        # the next line is how you select the lines above it, not one more
        last = end.blockNumber()
        if end.position() == cursor.selectionEnd() and last > first:
            last -= 1
        return first, last

    def _replace_lines(self, first:int, last:int, lines:list) -> None:
        """Swap lines `first`..`last` for `lines`, in one undo step, and keep them selected.

        Returns:
            None.
        """
        document = self.document()
        start = document.findBlockByNumber(first)
        end = document.findBlockByNumber(min(last, document.blockCount() - 1))
        cursor = self.textCursor()
        cursor.beginEditBlock()
        try:
            cursor.setPosition(start.position())
            cursor.setPosition(end.position() + max(0, end.length() - 1), qt.QTextCursor.KeepAnchor)
            cursor.insertText("\n".join(lines))
        finally:
            cursor.endEditBlock()
        head = document.findBlockByNumber(first)
        tail = document.findBlockByNumber(min(first + len(lines) - 1, document.blockCount() - 1))
        cursor.setPosition(head.position())
        cursor.setPosition(tail.position() + max(0, tail.length() - 1), qt.QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)

    def _lines(self, first:int, last:int) -> list:
        """The text of lines `first`..`last` inclusive.

        Returns:
            list: the text of each line in the range.
        """
        document = self.document()
        return [document.findBlockByNumber(n).text() for n in range(first, last + 1)]

    def sort_lines(self, descending:bool=False) -> None:
        """Sort the selected lines. Their indentation is part of the text, so blocks keep together.

        Returns:
            None.
        """
        first, last = self._selected_lines()
        if last > first:
            self._replace_lines(first, last,
                                sorted(self._lines(first, last), key=str.lower, reverse=descending))

    def join_lines(self) -> None:
        """Pull the selected lines onto one, single-spaced - the reverse of wrapping by hand.

        Returns:
            None.
        """
        first, last = self._selected_lines()
        if last == first:
            last = min(first + 1, self.document().blockCount() - 1)   # no selection: take the next
        if last == first:
            return
        parts = [line.strip() for line in self._lines(first, last)]
        self._replace_lines(first, last, [" ".join(p for p in parts if p)])

    def transform_case(self, mode:str) -> None:
        """upper / lower / title on the selection, or on the word under the caret.

        Returns:
            None.
        """
        cursor = self.textCursor()
        if not cursor.hasSelection():
            cursor.select(qt.QTextCursor.WordUnderCursor)
        text = cursor.selectedText()
        if not text:
            return
        changed = {"upper": text.upper(), "lower": text.lower(),
                   "title": text.title()}.get(mode)
        if changed is None or changed == text:
            return
        cursor.insertText(changed)
        self.setTextCursor(cursor)

    def trim_trailing(self) -> bool:
        """Strip trailing whitespace from every line. True if anything actually changed.

        Worth having on save: a stray space at the end of a line is invisible in the editor and
        shows up in git as a changed line, which buries the change you meant to make.

        Returns:
            bool: True if any trailing whitespace was removed.
        """
        text = self.toPlainText()
        cleaned = "\n".join(line.rstrip() for line in text.split("\n"))
        if cleaned == text:
            return False
        cursor = self.textCursor()
        at, scroll = cursor.position(), self.verticalScrollBar().value()
        cursor.beginEditBlock()
        try:
            cursor.select(qt.QTextCursor.Document)
            cursor.insertText(cleaned)
        finally:
            cursor.endEditBlock()
        cursor.setPosition(min(at, self.document().characterCount() - 1))
        self.setTextCursor(cursor)
        self.verticalScrollBar().setValue(scroll)      # rewriting the document scrolls it to the top
        return True

    # ------------------------------------------------------------------ symbols
    def symbol_under_cursor(self) -> str:
        """The identifier the caret is in, or an empty string when it is not on one.

        Returns:
            str: the identifier under the caret, or an empty string.
        """
        cursor = self.textCursor()
        cursor.select(qt.QTextCursor.WordUnderCursor)
        word = cursor.selectedText()
        return word if re.match(r"^[A-Za-z_]\w*$", word or "") else ""

    def definition_of(self, name:str, from_line:int) -> int:
        """The line defining `name`, seen from `from_line`, or -1.

        Same-named definitions are common - every class has an __init__ - so the innermost scope
        around the caret wins: called from inside class A, `self.build()` finds A's build and not the
        one in some other class further down the file.

        Returns:
            int: the line defining `name`, or -1.
        """
        matches = [entry for entry in self._sticky_map if entry[4] == name]
        if not matches:
            return -1
        scopes = [entry for entry in self._sticky_map if entry[0] < from_line <= entry[1]]
        for scope in reversed(scopes):                      # innermost enclosing scope first
            inside = [m for m in matches if scope[0] <= m[0] <= scope[1]]
            if inside:
                return inside[0][0]
        return matches[0][0]

    def go_to_definition(self) -> str:
        """Jump to the definition of the symbol under the caret.

        Returns "" on success, otherwise why it could not: the caller puts that in front of the user
        rather than having the key do nothing at all.

        This resolves within the CURRENT FILE only. Following imports across the workspace needs a
        real resolver, and guessing "the first def with that name anywhere" would send you to the
        wrong file often enough to be worse than saying so.

        Returns:
            str: an empty string on success, otherwise the reason it could not jump.
        """
        name = self.symbol_under_cursor()
        if not name:
            return "Put the caret on a symbol first."
        if not self._sticky_map:
            return "No symbols read from this file - it has to parse first."
        line = self.textCursor().blockNumber()
        target = self.definition_of(name, line)
        if target < 0:
            return "No definition of '%s' in this file." % name
        self.go_to_block(target, top=False)
        return ""

    def _offset(self, line:int, column:int) -> int:
        """Absolute document position of a 1-based line / 0-based column, as tokenize reports them.

        Returns:
            int: the absolute document position.
        """
        return self.document().findBlockByNumber(line - 1).position() + column

    def identifier_spots(self, name:str) -> list:
        """Every (start, end) where `name` appears as an IDENTIFIER, via tokenize.

        tokenize rather than a regex: it hands back NAME tokens only, so an occurrence inside a
        string or a comment is never touched, and `self.foo` yields the `foo` alone. It refuses to
        run on code that does not tokenise, which is the honest answer - a half-typed file cannot be
        renamed safely.

        Returns:
            list: the (start, end) offsets of each identifier occurrence.
        """
        text = self.toPlainText()
        spots = []
        try:
            for token in tokenize.generate_tokens(io.StringIO(text).readline):
                if token.type == tokenize.NAME and token.string == name:
                    spots.append((self._offset(*token.start), self._offset(*token.end)))
        except Exception:
            return []
        return spots

    def rename_symbol(self) -> str:
        """Rename every occurrence of the symbol under the caret, in this file, in ONE undo step.

        Returns "" on success, otherwise the reason. Like Go to Definition this stops at the file
        boundary; a rename that reached across the workspace without resolving imports would quietly
        break the callers it missed.

        Returns:
            str: an empty string on success, otherwise the reason it could not rename.
        """
        old = self.symbol_under_cursor()
        if not old:
            return "Put the caret on a symbol first."
        if keyword.iskeyword(old):
            return "'%s' is a Python keyword." % old
        spots = self.identifier_spots(old)
        if not spots:
            return "Cannot rename: this file does not tokenise (check for a syntax error)."

        new = kcore.message.prompt(title="Rename Symbol",
                                   label="Rename '%s' to (%d occurrence%s):"
                                         % (old, len(spots), "" if len(spots) == 1 else "s"),
                                   text=old, multilines=False, parent=self)
        new = (new or "").strip()
        if not new or new == old:
            return ""
        if not re.match(r"^[A-Za-z_]\w*$", new) or keyword.iskeyword(new):
            return "'%s' is not a valid identifier." % new

        cursor = self.textCursor()
        cursor.beginEditBlock()                    # one Ctrl+Z undoes the whole rename
        for start, end in reversed(spots):         # back to front, so earlier offsets stay valid
            cursor.setPosition(start)
            cursor.setPosition(end, qt.QTextCursor.KeepAnchor)
            cursor.insertText(new)
        cursor.endEditBlock()
        return ""

    def auto_temp_save(self, force=True) -> None:
        """Emit the changed-text signal to trigger a temporary autosave.

        Args:
            force: (bool): - whether to force emitting the change signal.

        Returns:
            None.
        """
        self.text_has_been_changed.emit(self.toPlainText())

    def _apply_editor_style(self) -> None:
        """Write the widget's OWN stylesheet: its surface and its font size.

        The size has to be in here, not only in setFont(): a stylesheet font-size beats setFont, and
        the host window applies `QWidget { font-size: 13px }` to everything it contains. Without this
        the next repolish quietly puts 13px back, and Ctrl+wheel looks like it does nothing.

        Both properties live in one place because a widget's own sheet REPLACES the previous one -
        writing the size alone would drop the background, and the reverse would drop the size.

        Returns:
            None.
        """
        base = self.surface or __surface__ or ("#2b2b2b" if __light__ else "#1e1e1e")
        points = self.font().pointSize()
        size = "%dpt" % points if points > 0 else "%dpx" % max(1, self.font().pixelSize())
        self.setStyleSheet("QPlainTextEdit { border: 0px; background-color: %s; font-size: %s; }"
                           "QPlainTextEdit QScrollBar { background: none; }" % (base, size))

        # setStyleSheet only marks the widget dirty: the new size is picked up at the NEXT polish,
        # which is why zooming looked like it needed the tab reopened. Force it now, then redo the
        # things measured from the font - the gutter width, the wrapped layout, the minimap.
        self.style().unpolish(self)
        self.style().polish(self)
        self.document().setDefaultFont(self.font())
        try:
            self.update_number_width()
            self.line_number_highlight()
            if getattr(self, "minimap", None) is not None:
                self.minimap.update()
            if getattr(self, "completer", None) is not None:
                self.completer.apply_theme()     # a separate window: the sheet above never reaches it
        except Exception:
            pass                                 # called from __init__, before those parts exist
        self.viewport().update()

    def set_text_size(self, value) -> None:
        """Set the editor font pixel size.

        Args:
            value: (int): - font pixel size to apply.

        Returns:
            None.
        """
        font = self.font()
        font.setPixelSize(value)
        self.setFont(font)
        self._apply_editor_style()


    def code_text_size_change(self, value) -> None:
        """Handle the text-size-changed signal by applying the new size.

        Args:
            value: (int): - font pixel size to apply.

        Returns:
            None.
        """
        self.set_text_size(value)


    def activate(self, value) -> None:
        """Handle completer activation (no-op placeholder).

        Args:
            value: (object): - activated completion value.

        Returns:
            None.
        """
        pass

    def resizeEvent(self, event) -> None:
        """Reposition the line-number gutter when the editor is resized.

        Args:
            event: (qt.QResizeEvent): - Qt resize event.

        Returns:
            None.
        """
        super().resizeEvent(event)
        rect = self.contentsRect()
        new_rect = qt.QtCore.QRect(rect.left(), rect.top(), self.line_number_width(), rect.height())
        self.line_numbers.setGeometry(new_rect)
        minimap = getattr(self, "minimap", None)
        if minimap is not None:
            strip = minimap.strip_width()
            minimap.setGeometry(qt.QtCore.QRect(rect.right() - strip + 1, rect.top(),
                                                strip, rect.height()))


    def mousePressEvent(self, event) -> None:
        """Emit the mouse-pressed signal and forward the event to the base class.

        Args:
            event: (qt.QMouseEvent): - Qt mouse press event.

        Returns:
            None.
        """
        line = self._sticky_hit(event.pos())
        if line >= 0:
            self.go_to_block(line)               # a click in the pinned band navigates, it does not
            return                               # place the caret under the band
        if event.modifiers() & qt.Qt.AltModifier and event.button() == qt.Qt.LeftButton:
            # not decided yet: released on the spot this adds one caret, dragged it becomes a column
            self._column_from = event.pos()
            self._column_moved = False
            return                               # Alt+click places a caret, it does not move the one
        if self.extra_cursors:
            self.clear_extra_cursors()           # a plain click goes back to a single caret
        self.mouse_pressed.emit(event)
        return super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        """Drag out a column with Alt held; otherwise just keep the pointer shape honest.

        Returns:
            None.
        """
        if self._column_from is not None and (event.buttons() & qt.Qt.LeftButton):
            if (event.pos() - self._column_from).manhattanLength() > qt.px(3):
                self._column_moved = True
                self.column_select(self._column_from, event.pos())
            return                               # the base class would drag a normal selection
        over = self._sticky_hit(event.pos()) >= 0
        self.viewport().setCursor(qt.Qt.PointingHandCursor if over else qt.Qt.IBeamCursor)
        return super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        """Settle what the Alt press meant: a caret if it never moved, a column if it did.

        Returns:
            None.
        """
        if self._column_from is not None:
            if not self._column_moved:
                self.add_cursor(self.cursorForPosition(self._column_from))
            self._column_from, self._column_moved = None, False
            return
        return super().mouseReleaseEvent(event)

    def column_select(self, start_point, end_point) -> None:
        """Put one caret per line between the two points, all at the same columns.

        The rectangle is built from COLUMNS, not from x pixels, so a line shorter than the right
        edge simply ends where it ends instead of the caret floating past its last character.

        Returns:
            None.
        """
        start = self.cursorForPosition(start_point)
        end = self.cursorForPosition(end_point)
        first, last = sorted((start.blockNumber(), end.blockNumber()))
        left, right = sorted((start.positionInBlock(), end.positionInBlock()))

        self.extra_cursors = []
        primary = None
        for number in range(first, last + 1):
            block = self.document().findBlockByNumber(number)
            if not block.isValid() or not block.isVisible():
                continue                         # a folded-away line takes no caret
            length = max(0, block.length() - 1)
            head, tail = min(left, length), min(right, length)
            cursor = self.textCursor()
            cursor.setPosition(block.position() + head)
            if tail != head:
                cursor.setPosition(block.position() + tail, qt.QTextCursor.KeepAnchor)
            if number == end.blockNumber():
                primary = cursor                 # the line under the pointer keeps the real caret
            else:
                self.extra_cursors.append(cursor)
        if primary is not None:
            self.setTextCursor(primary)
        self.line_number_highlight()
        self.viewport().update()


    def auto_scroll_left(self) -> None:
        """Snap the horizontal scroll back to the left edge when the caret lands in the left part of the
        document (a new line, Home, an indent), so a long line never leaves the next line start off-screen.

        Returns:
            None.
        """
        scrollbar = self.horizontalScrollBar()
        if scrollbar.value() <= 0:
            return
        document_x = self.cursorRect().left() + scrollbar.value()
        if document_x < self.viewport().width() * 0.6:
            scrollbar.setValue(0)

    def wheelEvent(self, event) -> None:
        """Zoom the text on Ctrl+wheel, otherwise scroll normally.

        Args:
            event: (qt.QWheelEvent): - Qt wheel event.

        Returns:
            None.
        """
        if hasattr(event, "angleDelta"):
            dy = event.angleDelta().y()
            if dy == 0 and hasattr(event, "pixelDelta"):
                dy = event.pixelDelta().y()
        else:
            dy = event.delta()

        control_modifier = getattr(qt.Qt, "ControlModifier", getattr(qt.Qt, "CTRL"))
        # some Maya / PySide combinations deliver EMPTY modifiers on wheel events: read the keyboard too
        if (event.modifiers() | qt.QApplication.keyboardModifiers()) & control_modifier:
            if dy > 0:
                self.zoom_in_text()
                event.accept()
                return
                
            elif dy < 0:
                self.zoom_out_text()
                event.accept()
                return

        return super().wheelEvent(event)


    def focusInEvent(self, event) -> None:
        """Attach the completer to this widget when it gains focus.

        Args:
            event: (qt.QFocusEvent): - Qt focus-in event.

        Returns:
            None.
        """
        if self.completer:
            self.completer.setWidget(self)
        super().focusInEvent(event)


    def keyPressEvent(self, event) -> None:
        """Handle key presses for indentation, execution, and completion.

        Args:
            event: (qt.QKeyEvent): - Qt key press event.

        Returns:
            None.
        """
        # with several carets the key goes to all of them; _multi_key returns False for anything it
        # does not own (Ctrl+S and friends), which then follows the normal path below
        if self.extra_cursors and self._multi_key(event):
            return

        pass_on = True
        quit_right_away = False
        if event.key() == qt.Qt.Key_Right:
            quit_right_away = True
        if event.key() == qt.Qt.Key_Left:
            quit_right_away = True
        if event.key() == qt.Qt.Key_Up:
            quit_right_away = True
        if event.key() == qt.Qt.Key_Down:
            quit_right_away = True

        if quit_right_away:
            super().keyPressEvent(event)
            return

        if self.completer:
            self.completer.activated.connect(self.activate)

        if self.completer:

            if self.completer.popup().isVisible():

                if event.key() == qt.Qt.Key_Enter:
                    event.ignore()
                    return
                if event.key() == qt.Qt.Key_Return:
                    event.ignore()
                    return
                if event.key() == qt.Qt.Key_Escape:
                    event.ignore()
                    return
                if event.key() == qt.Qt.Key_Tab:
                    event.ignore()
                    return
                if event.key() == qt.Qt.Key_Backtab:
                    event.ignore()
                    return

            else:
                if event.key() == qt.Qt.Key_Control or event.key() == qt.Qt.Key_Shift:
                    event.ignore()
                    self.completer.popup().hide()
                    return
            if event.key() == qt.Qt.Key_Control:
                event.ignore()
                self.completer.popup().hide()
                return

        if event.modifiers() & qt.Qt.ControlModifier:      # bitwise: actually tests the Ctrl flag (not `and`)
            if event.key() == qt.Qt.Key_Enter or event.key() == qt.Qt.Key_Return:
                # Ctrl+Enter always runs: the selection if any, otherwise the whole buffer
                self.run() if self.textCursor().hasSelection() else self.execute(self.toPlainText())
                return
            if event.key() == qt.Qt.Key_Slash:             # Ctrl+/ : toggle comment on the selected lines
                self.toggle_comment()
                return
            if event.key() == qt.Qt.Key_G:                 # Ctrl+G : go to line
                self.go_to_line()
                return
            if event.key() == qt.Qt.Key_D:                 # Ctrl+D : duplicate current line / selection
                self.duplicate_line()
                return

        if event.modifiers() & qt.Qt.AltModifier:          # Alt+Up / Alt+Down : move line(s)
            if event.key() == qt.Qt.Key_Up:
                self.move_line(-1)
                return
            if event.key() == qt.Qt.Key_Down:
                self.move_line(1)
                return

        if event.key() == qt.Qt.Key_Backtab or event.key() == qt.Qt.Key_Tab:
            self.handle_tab(event)
            pass_on = False

        if event.key() == qt.Qt.Key_Enter or event.key() == qt.Qt.Key_Return:
            # plain Enter on a selection = execute it (like Maya's script editor); otherwise a newline
            if self.textCursor().hasSelection():
                self.run()
                return
            self.handle_enter(event)
            pass_on = False

        # auto-close brackets/quotes, wrap a selection, skip over a matching closer, smart backspace
        if pass_on and self._handle_auto_pairs(event):
            pass_on = False

        if pass_on:
            super().keyPressEvent(event)

        # call tips: show the signature when '(' is typed, hide it on ')'
        if event.text() == "(":
            self._show_call_tip()
        elif event.text() == ")":
            qt.QToolTip.hideText()

        if self.completer:
            text = self.completer.text_under_cursor()
            if text:
                result = self.completer.handle_text(text)
                if result == True:
                    rect = self.cursorRect()
                    width = self.completer.popup().sizeHintForColumn(0) + self.completer.popup().verticalScrollBar().sizeHint().width()
                    if width > 350:
                        width = 350
                    rect.setWidth(util.scale_dpi(width))
                    self.completer.complete(rect)

                if result == False:
                    self.completer.popup().hide()
                    self.completer.clear_completer_list()
                    self.completer.refresh_completer = True


    def line_number_paint(self, event) -> None:
        """Paint the line numbers into the gutter widget.

        Args:
            event: (qt.QPaintEvent): - Qt paint event for the gutter.

        Returns:
            None.
        """
        painter = qt.QPainter(self.line_numbers)
        painter.fillRect(event.rect(),
                         _surface(qt.QColor(43, 43, 43) if __light__ else qt.QColor(30, 30, 30),
                                  self.surface))
        font = qt.QFont("Consolas", 9)
        painter.setFont(font)
        
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        offset = self.contentOffset()
        top = self.blockBoundingGeometry(block).translated(offset).top()
        bottom = top + self.blockBoundingRect(block).height()

        painter.setRenderHint(qt.QPainter.Antialiasing, True)
        arrows = qt.px(self.FOLD_COLUMN)
        regions = self.fold_regions()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                number = str(block_number + 1)
                painter.setPen(qt.QColor(60, 60, 60).lighter(150) if __light__ else qt.QColor(30, 30, 30).lighter(225))
                # the numbers give up the strip on the right, where the fold arrows go
                width = self.line_numbers.width() - 9 - arrows
                height = self.fontMetrics().height()
                painter.drawText(0, top, width, height, qt.Qt.AlignRight, number)

                # a folded region always shows its arrow; an open one only under the pointer
                folded = block_number in self._folded
                if block_number in regions and (folded or self._gutter_hover):
                    self._paint_fold_arrow(painter, self.line_numbers.width() - arrows,
                                           int(top), arrows, folded)

                # the git change bar hugs the left edge, where VS Code puts it
                for hunk in self._changes:
                    if hunk[0] <= block_number <= hunk[1]:
                        self._paint_change_bar(painter, top, height, hunk[2])
                        break

                # blue dot on the left of any line containing TODO (also FIXME/XXX)
                if re.search(r'\b(?:TODO|FIXME|XXX)\b', block.text()):
                    painter.setBrush(qt.QBrush(qt.QColor(80, 150, 230)))
                    painter.setPen(qt.Qt.NoPen)
                    r = 4
                    painter.drawEllipse(qt.QtCore.QPoint(r + 1 + qt.px(4), int(top + height / 2)),
                                        r, r)

            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            block_number += 1
    
    
    def line_number_width(self) -> int:
        """Return the pixel width required to display the gutter line numbers.

        Returns:
            int: gutter width in pixels.
        """
        digits = 1
        max_value = max(1, self.blockCount())
        while max_value >= 10:
            max_value /= 10
            digits += 1
        advance = (self.fontMetrics().horizontalAdvance('9') if qt.__qt__ == "pyside6"
                   else self.fontMetrics().width('9'))
        return 30 + advance * digits + qt.px(self.FOLD_COLUMN)   # + the strip the arrows sit in
        
        
    def line_number_highlight(self) -> None:
        """Highlight the current line and reapply any active search selections.

        Returns:
            None.
        """
        extra_selections = []
        if not self.isReadOnly():
            selection = qt.QTextEdit.ExtraSelection()

            line_color = _surface(qt.QColor(43, 43, 43) if __light__ else qt.QColor(30, 30, 30),
                                  self.surface).lighter(135)
            selection.format.setBackground(line_color)
            selection.format.setProperty(qt.QTextFormat.FullWidthSelection, True)

            selection.cursor = self.textCursor()
            selection.cursor.clearSelection()
            extra_selections.append(selection)
        extra_selections.extend(self._multi_selections())
        extra_selections.extend(self._occurrence_selections())
        extra_selections.extend(self.search_selections)
        extra_selections.extend(self._bracket_selections())
        extra_selections.extend(self._lint_selections())
        self.setExtraSelections(extra_selections)

    def _occurrence_selections(self) -> list:
        """Faintly box every other occurrence of the identifier under the caret, as VS Code does.

        Whole words only, so `pos` does not light up inside `position`. It stays quiet when there is
        nothing useful to show - a selection of your own, a one-letter name, or a hit on every line of
        a big file, where the highlight would be noise rather than information.

        Returns:
            list: an extra selection for each other occurrence of the word.
        """
        cursor = self.textCursor()
        if cursor.hasSelection() or self.isReadOnly():
            return []
        cursor.select(qt.QTextCursor.WordUnderCursor)
        word = cursor.selectedText()
        if len(word) < 2 or not re.match(r"^[A-Za-z_]\w*$", word):
            return []

        text = self.toPlainText()
        here = cursor.selectionStart()
        tint = _surface(qt.QColor(30, 30, 30), self.surface).lighter(190)
        found = []
        for match in re.finditer(r"\b%s\b" % re.escape(word), text):
            if match.start() == here:
                continue                          # the one the caret is in stays plain
            if len(found) >= self.MAX_OCCURRENCES:
                return []                         # too common to mean anything - show none
            sel = qt.QTextEdit.ExtraSelection()
            sel.format.setBackground(tint)
            spot = self.textCursor()
            spot.setPosition(match.start())
            spot.setPosition(match.end(), qt.QTextCursor.KeepAnchor)
            sel.cursor = spot
            found.append(sel)
        return found

    def _run_lint(self) -> None:
        """Compile the buffer (Python files only) and remember which line has a syntax error, then refresh
        the highlight. compile() never runs the code — it only parses it.

        Returns:
            None.
        """
        line = None
        if not (getattr(self, "file_path", "") or "").endswith(".mel"):
            text = self.toPlainText()
            if text.strip():
                try:
                    compile(text, "<lint>", "exec")
                except SyntaxError as error:
                    line = error.lineno
                except Exception:
                    line = None
        if line != self._lint_line:
            self._lint_line = line
            self.line_number_highlight()

    def _lint_selections(self) -> list:
        """A red wavy-ish underline on the line flagged by the live syntax check (if any).

        Returns:
            list: the underline selection for the flagged line, or empty when there is none.
        """
        lint_line = getattr(self, "_lint_line", None)
        if not lint_line:
            return []
        block = self.document().findBlockByNumber(lint_line - 1)
        if not block.isValid():
            return []
        sel = qt.QTextEdit.ExtraSelection()
        sel.format.setUnderlineColor(qt.QColor(220, 70, 70))
        sel.format.setUnderlineStyle(qt.QTextCharFormat.WaveUnderline)
        cursor = self.textCursor()
        cursor.setPosition(block.position())
        cursor.setPosition(block.position() + max(1, block.length() - 1), qt.QTextCursor.KeepAnchor)
        sel.cursor = cursor
        return [sel]

    def _bracket_selections(self) -> list:
        """Return extra selections highlighting the bracket next to the caret and its match (or a red
        highlight when unmatched). Handles () [] {} in both directions.

        Returns:
            list: the extra selections for the bracket and its match, or a single red one when unmatched.
        """
        if self.isReadOnly():
            return []
        opening = {"(": ")", "[": "]", "{": "}"}
        closing = {v: k for k, v in opening.items()}
        doc = self.toPlainText()
        pos = self.textCursor().position()

        # a bracket immediately before or after the caret
        cand = None                                    # (index_of_bracket, direction)
        if pos < len(doc) and (doc[pos] in opening or doc[pos] in closing):
            cand = (pos, +1 if doc[pos] in opening else -1)
        elif pos > 0 and (doc[pos - 1] in opening or doc[pos - 1] in closing):
            cand = (pos - 1, +1 if doc[pos - 1] in opening else -1)
        if cand is None:
            return []

        idx, direction = cand
        ch = doc[idx]
        want = opening[ch] if direction > 0 else closing[ch]
        depth = 0
        match = None
        i = idx
        while 0 <= i < len(doc):
            c = doc[i]
            if c == ch:
                depth += 1
            elif c == want:
                depth -= 1
                if depth == 0:
                    match = i
                    break
            i += direction

        def _sel(index:int, ok:bool) -> object:
            """Build an extra selection boxing the bracket at `index`, green when matched, red when not.

            Returns:
                object: the bracket's extra selection.
            """
            s = qt.QTextEdit.ExtraSelection()
            color = qt.QColor(90, 130, 90) if ok else qt.QColor(150, 60, 60)
            s.format.setBackground(color)
            c = self.textCursor()
            c.setPosition(index)
            c.setPosition(index + 1, qt.QTextCursor.KeepAnchor)
            s.cursor = c
            return s

        if match is None:
            return [_sel(idx, False)]                  # unmatched -> red
        return [_sel(idx, True), _sel(match, True)]

    def set_search_highlights(self, selections) -> None:
        """Store search-result selections and refresh the highlighting.

        Args:
            selections: (list): - extra selections to display as search hits.

        Returns:
            None.
        """
        self.search_selections = selections
        self.line_number_highlight()


    def update_number_width(self, value=0) -> None:
        """Update the viewport margin to fit the gutter width.

        Args:
            value: (int): - unused block-count value from the signal.

        Returns:
            None.
        """
        minimap = getattr(self, "minimap", None)
        # isVisibleTo(): True unless the strip was explicitly hidden, even before the window is shown
        right = minimap.strip_width() if (minimap is not None and minimap.isVisibleTo(self)) else 0
        self.setViewportMargins(self.line_number_width(), 0, right, 0)


    def update_number_area(self, rect, y_value) -> None:
        """Scroll or repaint the gutter in response to editor updates.

        Args:
            rect:    (qt.QtCore.QRect): - region of the viewport to update.
            y_value: (int): - vertical scroll delta.

        Returns:
            None.
        """
        if y_value:
            self.line_numbers.scroll(0, y_value)
        if not y_value:
            self.line_numbers.update(0, rect.y(), self.line_numbers.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_number_width()


    def _clear_inline(self) -> None:
        """Drop every inline result (an edit has shifted the line numbers they hang on).

        Returns:
            None.
        """
        if self._inline:
            self._inline = {}
            self.viewport().update()

    def _set_inline(self, block:int=None, value:object=None) -> None:
        """Store a run result to draw at the end of line `block`, capped to one short line.

        Args:
            block: (int):    - 0-based line number the result hangs on.
            value: (object): - the evaluated value.

        Returns:
            None.
        """
        try:
            text = repr(value).replace("\n", " ")
        except Exception:
            text = "<unrepresentable>"
        if len(text) > 120:
            text = text[:120] + "…"
        self._inline[block] = text
        self.viewport().update()

    def execute(self, text:str=None, at_block:int=None) -> None:
        """Compile and run `text` as Python in the shared console namespace, safely.

        User code runs in CONSOLE_NAMESPACE (never this module's globals), so it cannot corrupt the
        editor. Compile errors and runtime exceptions are caught and printed as a clean traceback instead of
        propagating out of a Qt event handler (which would be swallowed or destabilise the UI). When the
        source is a single expression and `at_block` is given, its value is shown inline at that line's end.

        Args:
            text:      (str): - the Python source to execute.
            at_block:  (int): - the 0-based line the inline result hangs on (None = no inline, statements).

        Returns:
            None.
        """
        if not text or not text.strip():
            return
        # echo the executed code to the output (like Maya's script editor shows the command it ran)
        sys.stdout.write(text if text.endswith("\n") else text + "\n")

        expression = None
        if at_block is not None:
            try:
                expression = compile(text, "<the host>", "eval")   # a single expression -> show its value inline
            except SyntaxError:
                expression = None

        def _report() -> None:
            """Print the user's traceback, skipping this frame so it starts at their code."""
            exc_type, exc_value, exc_tb = sys.exc_info()
            user_tb = exc_tb.tb_next if exc_tb is not None else exc_tb
            sys.stderr.write("".join(traceback.format_exception(exc_type, exc_value, user_tb)))

        if expression is not None:
            try:
                value = eval(expression, self.namespace)
                if value is not None:
                    self._set_inline(at_block, value)
            except Exception:
                _report()
            return

        try:
            code = compile(text, "<the host>", "exec")
        except SyntaxError:
            sys.stderr.write(traceback.format_exc())
            return
        try:
            exec(code, self.namespace)
        except Exception:
            _report()

    def run(self) -> None:
        """Execute the currently selected text as Python, showing an expression's value inline.

        Returns:
            None.
        """
        cursor = self.textCursor()
        end_block = self.document().findBlock(cursor.selectionEnd()).blockNumber() if cursor.hasSelection() \
            else cursor.blockNumber()
        self.execute(cursor.selection().toPlainText(), at_block=end_block)

    
    FONT_OPTIONVAR = "code_editor_font_size"     # persisted editor font size (Maya optionVar)
    MAX_OCCURRENCES = 60                       # past this a name is too common for the highlight to help

    def apply_font_size(self, size:int=None, persist:bool=True) -> None:
        """Set the editor font size, preserving the current family, and remember it across sessions.

        Args:
            size:    (int):  - point size to apply (clamped to 6..48).
            persist: (bool): - store it in a Maya optionVar so it is restored next time.

        Returns:
            None.
        """
        try:
            size = max(6, min(48, int(size)))
        except (TypeError, ValueError):
            return
        font = self.font()
        font.setPointSize(size)                # keep the configured family, only change the size
        self.setFont(font)
        self._apply_editor_style()             # setFont alone loses to the inherited font-size
        if persist:
            try:
                import maya.cmds as cmds
                cmds.optionVar(intValue=(self.FONT_OPTIONVAR, size))
            except Exception:
                pass

    def zoom_in_text(self) -> None:
        """Increase the editor font size by one point (persisted).

        Returns:
            None.
        """
        self.apply_font_size(self.font().pointSize() + 1)


    def zoom_out_text(self) -> None:
        """Decrease the editor font size by one point (persisted).

        Returns:
            None.
        """
        self.apply_font_size(self.font().pointSize() - 1)


    def setup_highlighter(self, format='python') -> None:
        """Attach a syntax highlighter matching the requested language.

        Args:
            format: (str): - highlighter type, 'python' or 'mel'.

        Returns:
            None.
        """
        if format == 'python':
            self.highlighter = PythonHighlighter(self.document())
            
        elif format == 'mel':
            self.highlighter = MelHighlighter(self.document())
        else:
            pass

    def remove_tab(self, string_value) -> str:
        """Remove one leading four-space indent from the string if present.

        Args:
            string_value: (str): - line of text to unindent.

        Returns:
            str: the string with one indent level removed, if any.
        """
        string_section = string_value[0:4]
        if string_section == '    ':
            return string_value[4:]
        return string_value

    def add_tab(self, string_value) -> str:
        """Prepend one four-space indent to the given string.

        Args:
            string_value: (str): - line of text to indent.

        Returns:
            str: the indented string.
        """
        return '    %s' % string_value

    def handle_enter(self, event) -> None:
        """Insert a newline preserving and extending the current indentation.

        Args:
            event: (qt.QKeyEvent): - Qt key event that triggered the newline.

        Returns:
            None.
        """
        cursor = self.textCursor()
        current_block = cursor.block()
        cursor_position = cursor.positionInBlock()
        current_block_text = str(current_block.text())
        current_found = ''
        if not current_found:
            current_found = re.search('^ +', current_block_text)
            if current_found:
                current_found = current_found.group(0)
        indent = 0
        if current_found:
            indent = len(current_found)
        colon_position = current_block_text.find(':')
        comment_position = current_block_text.find('#')
        if colon_position > -1:
            sub_indent = 4
            if comment_position > -1 and comment_position < colon_position:
                sub_indent = 0
            indent += sub_indent
        # dedent the next line one level after a block-ending statement (return/pass/break/continue/raise)
        first_word = current_block_text.strip().split(" ", 1)[0].rstrip("()")
        if first_word in ("return", "pass", "break", "continue", "raise"):
            indent = max(0, indent - 4)
        if cursor_position < indent:
            indent = (cursor_position - indent) + indent
        cursor.beginEditBlock()               # one undo step for the newline + its auto-indent
        cursor.insertText(('\n' + ' ' * indent))
        cursor.endEditBlock()


    def handle_tab(self, event) -> None:
        """Indent or unindent the selection in response to Tab or Backtab.

        Args:
            event: (qt.QKeyEvent): - Qt key event, Tab or Backtab.

        Returns:
            None.
        """
        edit_cursor = self.textCursor()
        edit_cursor.beginEditBlock()          # group the whole (un)indent into a single undo step

        cursor = self.textCursor()
        document = self.document()
        start_position = cursor.anchor()
        select_position = cursor.selectionStart()
        select_start_block = document.findBlock(select_position)
        start = select_position - select_start_block.position()

        end_position = cursor.position()
        if start_position > end_position:
            temp_position = end_position
            end_position = start_position
            start_position = temp_position

        if event.key() == qt.Qt.Key_Tab:
            if not cursor.hasSelection():
                self.insertPlainText('    ')
                start_position += 4
                end_position = start_position

            if cursor.hasSelection():
                cursor.setPosition(start_position)
                cursor.movePosition(qt.QTextCursor.StartOfLine)
                cursor.setPosition(end_position, qt.QTextCursor.KeepAnchor)
                text = cursor.selection()
                text = text.toPlainText()
                split_text = text.split('\n')
                edited = []
                inc = 0
                for text_split in split_text:
                    edited.append(self.add_tab(text_split))
                    if inc == 0:
                        start_position += 4

                    end_position += 4
                    inc += 1

                edited_text = '\n'.join(edited)
                cursor.insertText(edited_text)
                self.setTextCursor(cursor)

        if event.key() == qt.Qt.Key_Backtab:
            if not cursor.hasSelection():
                cursor = self.textCursor()
                cursor.movePosition(qt.QTextCursor.StartOfLine)
                cursor.movePosition(qt.QTextCursor.Right, qt.QTextCursor.KeepAnchor, 4)

                text = cursor.selection()
                text = text.toPlainText()
                if text:
                    if text == '    ':
                        cursor.insertText('')
                        self.setTextCursor(cursor)
                        start_position -= 4
                        end_position = start_position
            if cursor.hasSelection():
                cursor.setPosition(start_position)
                cursor.movePosition(qt.QTextCursor.StartOfLine)
                cursor.setPosition(end_position, qt.QTextCursor.KeepAnchor)
                cursor.movePosition(qt.QTextCursor.EndOfLine, qt.QTextCursor.KeepAnchor)
                self.setTextCursor(cursor)
                text = cursor.selection()
                text = str(text.toPlainText())
                split_text = text.split('\n')
                edited = []
                inc = 0
                skip_indent = False
                for text_split in split_text:
                    new_string_value = text_split
                    if not skip_indent:
                        new_string_value = self.remove_tab(text_split)
                    if inc == 0 and new_string_value == text_split:
                        skip_indent = True

                    if not skip_indent:
                        if new_string_value != text_split:
                            if inc == 0:
                                offset = (start - 4) + 4
                                if offset > 4:
                                    offset = 4
                                start_position -= offset

                            end_position -= 4

                    edited.append(new_string_value)
                    inc += 1
                edited_text = '\n'.join(edited)
                cursor.insertText(edited_text)
                self.setTextCursor(cursor)

        cursor = self.textCursor()
        cursor.setPosition(start_position)
        cursor.setPosition(end_position, qt.QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)

        edit_cursor.endEditBlock()            # close the single-undo group

    PAIRS = {"(": ")", "[": "]", "{": "}", '"': '"', "'": "'"}

    def _show_call_tip(self) -> None:
        """When '(' is typed, show the callable's signature (and first doc line) as a tooltip. Resolves the
        callable from the live console namespace, so it works for your functions, cmds.*, etc.

        Returns:
            None.
        """
        cursor = self.textCursor()
        line = cursor.block().text()[:cursor.positionInBlock()]
        match = re.search(r'([A-Za-z_][\w.]*)\($', line)   # name(  just before the caret
        if not match:
            return
        name = match.group(1)

        obj = self.resolve(name)       # shared with Quick Help, so both see cmds without an import
        if obj is None:
            return

        target = obj
        if inspect.isclass(obj):                           # for a class, show its __init__ signature
            target = getattr(obj, "__init__", obj)

        tip = None
        try:
            tip = "%s%s" % (name, inspect.signature(target))
        except (TypeError, ValueError):
            # C funcs / Maya commands have no Python signature -> use the first useful doc line
            doc = inspect.getdoc(obj) or ""
            first = next((l for l in doc.splitlines() if l.strip()), "")
            if first:
                tip = "%s  —  %s" % (name, first.strip())
        if not tip:
            return

        point = self.mapToGlobal(self.cursorRect().bottomRight())
        qt.QToolTip.showText(point, tip, self)

    def _handle_auto_pairs(self, event) -> bool:
        """Auto-close brackets/quotes, wrap a selection, skip over a matching closer, and pair-delete on
        backspace. Returns True when it handled the key (so the default insert is skipped).

        Returns:
            bool: True if the key was handled and the default insert should be skipped.
        """
        text = event.text()
        cursor = self.textCursor()

        # Backspace between an empty pair "()" -> delete both
        if event.key() == qt.Qt.Key_Backspace and not cursor.hasSelection():
            pos  = cursor.position()
            doc  = self.toPlainText()
            if 0 < pos < len(doc) and doc[pos - 1] in self.PAIRS and self.PAIRS[doc[pos - 1]] == doc[pos]:
                cursor.beginEditBlock()
                cursor.deleteChar()
                cursor.deletePreviousChar()
                cursor.endEditBlock()
                return True
            return False

        if text not in self.PAIRS and text not in self.PAIRS.values():
            return False

        # typing a closer right before the same closer -> just step over it (don't duplicate)
        if text in self.PAIRS.values() and not cursor.hasSelection():
            pos = cursor.position()
            doc = self.toPlainText()
            if pos < len(doc) and doc[pos] == text:
                cursor.movePosition(qt.QTextCursor.Right)
                self.setTextCursor(cursor)
                return True

        if text not in self.PAIRS:
            return False
        closer = self.PAIRS[text]

        # wrap a selection in the pair
        if cursor.hasSelection():
            selected = cursor.selectedText()
            cursor.beginEditBlock()
            cursor.insertText(text + selected + closer)
            cursor.endEditBlock()
            return True

        # for quotes, don't auto-close when sitting on a word char (e.g. typing an apostrophe in text)
        pos = cursor.position()
        doc = self.toPlainText()
        if text in ('"', "'"):
            prev_char = doc[pos - 1] if pos > 0 else ""
            if prev_char.isalnum() or prev_char == text:
                return False

        cursor.beginEditBlock()
        cursor.insertText(text + closer)
        cursor.movePosition(qt.QTextCursor.Left)     # place the caret between the pair
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        return True

    def _comment_token(self) -> str:
        """'//' for a .mel file, '#' otherwise.

        Returns:
            str: the comment token for this file's language.
        """
        return "//" if (getattr(self, "file_path", "") or "").endswith(".mel") else "#"

    def toggle_comment(self) -> None:
        """Comment or uncomment every line touched by the selection (or the current line). If all touched
        lines are already commented it uncomments; otherwise it comments. One undo step.

        Returns:
            None.
        """
        token = self._comment_token()
        cursor = self.textCursor()
        doc    = self.document()

        start = cursor.selectionStart()
        end   = cursor.selectionEnd()
        first = doc.findBlock(start).blockNumber()
        last  = doc.findBlock(end).blockNumber()
        # a selection ending exactly at a line start shouldn't include that empty next line
        if end > start and doc.findBlock(end).position() == end and last > first:
            last -= 1

        blocks = [doc.findBlockByNumber(n) for n in range(first, last + 1)]
        # decide direction: uncomment only if every non-empty line is already commented
        non_empty = [b for b in blocks if b.text().strip()]
        all_commented = non_empty and all(b.text().lstrip().startswith(token) for b in non_empty)

        edit = self.textCursor()
        edit.beginEditBlock()
        for b in blocks:
            text = b.text()
            if not text.strip():
                continue
            c = self.textCursor()
            c.setPosition(b.position())
            if all_commented:
                # remove the first token (and one following space if present)
                stripped = text.lstrip()
                lead = len(text) - len(stripped)
                c.setPosition(b.position() + lead)
                remove = len(token) + (1 if stripped[len(token):len(token)+1] == " " else 0)
                c.setPosition(c.position() + remove, qt.QTextCursor.KeepAnchor)
                c.removeSelectedText()
            else:
                c.insertText(token + " ")
        edit.endEditBlock()

    def go_to_line(self) -> None:
        """Prompt for a line number and move the cursor there.

        Returns:
            None.
        """
        total = self.document().blockCount()
        # parented to the editor: a stylesheet travels down the parent chain, and a dialog raised
        # with no parent comes up in the host application's default grey
        line = kcore.message.prompt(title="Go to Line", label="Line (1-%d):" % total, text="",
                                    multilines=False, parent=self)
        if not line:
            return
        try:
            n = max(1, min(total, int(str(line).strip())))
        except (TypeError, ValueError):
            return
        block  = self.document().findBlockByNumber(n - 1)
        cursor = self.textCursor()
        cursor.setPosition(block.position())
        self.setTextCursor(cursor)
        self.centerCursor()

    def duplicate_line(self) -> None:
        """Duplicate the current line, or the selected lines, below. One undo step.

        Returns:
            None.
        """
        cursor = self.textCursor()
        doc = self.document()
        if cursor.hasSelection():
            first = doc.findBlock(cursor.selectionStart())
            last  = doc.findBlock(cursor.selectionEnd())
            start = first.position()
            end   = last.position() + last.length() - 1
            block_text = self.toPlainText()[start:end]
        else:
            block = cursor.block()
            block_text = block.text()
            end = block.position() + block.length() - 1

        c = self.textCursor()
        c.beginEditBlock()
        c.setPosition(end)
        c.insertText("\n" + block_text)          # copy placed on the line(s) below
        c.endEditBlock()

    # ---- selection commands
    #
    # Everything VS Code offers here that a QPlainTextEdit can actually do. What is missing from the
    # list - Add Cursor Above/Below, Add Next Occurrence, Select All Occurrences, Column Selection -
    # all need MULTIPLE carets and disjoint selections, and a QTextEdit has exactly one QTextCursor.
    # Faking them would mean reimplementing input, caret painting and coordinated editing.

    def copy_line(self, direction:int=1) -> None:
        """Copy the current line (or the selected lines) above (-1) or below (1), one undo step.

        The caret follows the COPY, which is what makes repeating it stack duplicates the way you
        expect rather than pushing the original further away.

        Returns:
            None.
        """
        cursor = self.textCursor()
        document = self.document()
        first = document.findBlock(cursor.selectionStart())
        last = document.findBlock(cursor.selectionEnd())
        start = first.position()
        end = last.position() + last.length() - 1
        text = self.toPlainText()[start:end]

        edit = self.textCursor()
        edit.beginEditBlock()
        if direction < 0:
            edit.setPosition(start)
            edit.insertText(text + "\n")
            edit.setPosition(start)                     # the caret lands on the new line above
        else:
            edit.setPosition(end)
            edit.insertText("\n" + text)
        edit.endEditBlock()
        self.setTextCursor(edit)

    def duplicate_selection(self) -> None:
        """Repeat the selection right after itself; with no selection, duplicate the line.

        Returns:
            None.
        """
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return self.duplicate_line()
        text = cursor.selectedText().replace(u" ", "\n")   # Qt's newline inside a selection
        end = cursor.selectionEnd()
        edit = self.textCursor()
        edit.beginEditBlock()
        edit.setPosition(end)
        edit.insertText(text)
        edit.endEditBlock()

    def select_line(self) -> None:
        """Select the whole current line; called again, extend the selection by one more line.

        Returns:
            None.
        """
        cursor = self.textCursor()
        document = self.document()
        first = document.findBlock(cursor.selectionStart())
        last = document.findBlock(cursor.selectionEnd())
        start = first.position()
        end = last.position() + last.length() - 1
        if (cursor.selectionStart(), cursor.selectionEnd()) == (start, end):
            following = last.next()                     # already whole lines: take the next one too
            if following.isValid():
                end = following.position() + following.length() - 1
        cursor.setPosition(start)
        cursor.setPosition(min(end, len(self.toPlainText())), qt.QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)

    def expand_selection(self) -> None:
        """Grow the selection one step: word, then line, then enclosing brackets, then everything.

        Returns:
            None.
        """
        start, end = self.textCursor().selectionStart(), self.textCursor().selectionEnd()
        stack = getattr(self, "_expand_stack", [])
        if not stack or stack[-1][1] != (start, end):
            stack = []                                  # the caret moved on its own: start over
        stack.append(((start, end), None))

        text = self.toPlainText()
        if start == end:
            probe = self.textCursor()
            probe.select(qt.QTextCursor.WordUnderCursor)
            span = (probe.selectionStart(), probe.selectionEnd()) if probe.hasSelection() \
                else _line_span(text, start, end)
            if span == (start, end):
                span = _line_span(text, start, end)
        else:
            line = _line_span(text, start, end)
            span = line if (start, end) != line else (_bracket_span(text, start, end)
                                                      or (0, len(text)))

        stack[-1] = ((start, end), span)
        self._expand_stack = stack
        self._select(span)

    def shrink_selection(self) -> None:
        """Step back to what the selection was before the last expand.

        Returns:
            None.
        """
        stack = getattr(self, "_expand_stack", [])
        cursor = self.textCursor()
        current = (cursor.selectionStart(), cursor.selectionEnd())
        while stack:
            previous, produced = stack.pop()
            if produced == current:
                self._expand_stack = stack
                self._select(previous)
                return
        self._expand_stack = []

    def _select(self, span:tuple) -> None:
        """Set the primary caret to select the given (start, end) span.

        Returns:
            None.
        """
        cursor = self.textCursor()
        cursor.setPosition(span[0])
        cursor.setPosition(span[1], qt.QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)

    def move_line(self, direction:int=0) -> None:
        """Move the current line (or selected lines) up (-1) or down (+1). One undo step, keeps selection.

        Returns:
            None.
        """
        if direction not in (-1, 1):
            return
        doc    = self.document()
        cursor = self.textCursor()
        first_n = doc.findBlock(cursor.selectionStart()).blockNumber()
        last_n  = doc.findBlock(cursor.selectionEnd()).blockNumber()

        if direction < 0 and first_n == 0:
            return
        if direction > 0 and last_n == doc.blockCount() - 1:
            return

        # work on whole lines: rebuild the document's line list with the block group shifted by one
        lines = self.toPlainText().split("\n")
        block = lines[first_n:last_n + 1]
        del lines[first_n:last_n + 1]
        insert_at = first_n - 1 if direction < 0 else first_n + 1
        lines[insert_at:insert_at] = block

        c = self.textCursor()
        c.beginEditBlock()
        c.select(qt.QTextCursor.Document)
        c.insertText("\n".join(lines))
        c.endEditBlock()

        # reselect the moved block at its new position
        new_first = insert_at
        new_last  = insert_at + len(block) - 1
        start_block = doc.findBlockByNumber(new_first)
        end_block   = doc.findBlockByNumber(new_last)
        moved = self.textCursor()
        moved.setPosition(start_block.position())
        moved.setPosition(end_block.position() + end_block.length() - 1, qt.QTextCursor.KeepAnchor)
        self.setTextCursor(moved)

    def set_completer(self, completer) -> None:
        """Instantiate the given completer class and bind it to this editor.

        Args:
            completer: (type): - completer class to instantiate.

        Returns:
            None.
        """
        self.completer = completer()
        self.completer.setWidget(self)
        self.completer.apply_theme()    # only now can it read OUR palette and font, not defaults

    # pop menu commands
    def clicked_menu_save(self) -> None:
        """Emit the saving-script signal to persist the current script.

        Returns:
            None.
        """
        self.flush_autosave()          # make sure the temp file holds the latest text before saving
        self.savingScript.emit(True)

    def clicked_menu_save_as(self) -> None:
        """Ask the host to save this buffer under a new name.

        Like clicked_menu_save this only EMITS: the widget has no idea where a script belongs - the
        standalone window writes straight to disk, the host writes through its workspace.

        Returns:
            None.
        """
        self.flush_autosave()
        self.savingScriptAs.emit(True)

    def flush_autosave(self) -> None:
        """Write the pending debounced autosave now (called before a save/close so nothing is lost).

        Returns:
            None.
        """
        timer = getattr(self, "_autosave_timer", None)
        if timer is not None and timer.isActive():
            timer.stop()
            self.auto_temp_save()


    def clicked_menu_cut(self) -> None:
        """Cut the current selection to the clipboard.

        Returns:
            None.
        """
        self.cut()


    def clicked_menu_copy(self) -> None:
        """Copy the currently selected text to the clipboard.

        Returns:
            None.
        """
        util.copy(string=self.textCursor().selection().toPlainText())


    def clicked_menu_paste(self) -> None:
        """Paste clipboard text at the cursor.

        pyperclip is used when it is installed and ignored when it is not: Qt's own clipboard does
        the job, and this package depends on nothing outside maya and PySide. It used to print a
        message and paste NOTHING when the import failed, which made an optional module look
        required.

        Returns:
            None.
        """
        try:
            import pyperclip
            self.textCursor().insertText(pyperclip.paste())
        except Exception:
            self.paste()                      # Qt's clipboard: always there
        
        
    def clicked_menu_select(self) -> None:
        """Select all text in the editor.

        Returns:
            None.
        """
        self.selectAll()


    def clicked_menu_execute(self) -> None:
        """Execute the currently selected text as Python code.

        Returns:
            None.
        """
        self.execute(self.textCursor().selection().toPlainText())


    def clicked_menu_execute_line(self) -> None:
        """Execute the line under the cursor as Python code.

        Returns:
            None.
        """
        cursor = self.textCursor()
        cursor.movePosition(cursor.StartOfLine)
        cursor.movePosition(cursor.EndOfLine, cursor.KeepAnchor)
        # selectedText() uses U+2029 for line breaks; toPlainText() on the selection keeps real newlines
        self.execute(cursor.selection().toPlainText())


    def clicked_menu_execute_all(self) -> None:
        """Execute the entire editor contents as Python code.

        Returns:
            None.
        """
        self.execute(self.toPlainText())


    def clicked_save_to_shelf(self) -> None:
        """Create a shelf button on the active shelf that runs the selected code (or the whole buffer).

        Returns:
            None.
        """
        import maya.cmds as cmds
        import maya.mel as mel

        text = self.textCursor().selection().toPlainText() or self.toPlainText()
        if not text.strip():
            return
        label = kcore.message.prompt(title="Save to Shelf", label="Button label:", text="script",
                                     multilines=False, parent=self)
        if not label:
            return
        try:
            top   = mel.eval("$temp = $gShelfTopLevel")
            shelf = cmds.tabLayout(top, query=True, selectTab=True)
            source_type = "mel" if (getattr(self, "file_path", "") or "").endswith(".mel") else "python"
            cmds.shelfButton(parent=shelf, label=str(label), annotation=str(label),
                             imageOverlayLabel=str(label)[:6], sourceType=source_type, command=text)
            log.info("saved a shelf button '%s' to '%s'." % (label, shelf))
        except Exception:
            log.exception("could not create the shelf button")


    def clicked_command_reference(self) -> None:
        """Open the Maya documentation page for the selected command.

        Returns:
            None.
        """
        try:
            import maya.cmds as cmds
            command = self.textCursor().selection().toPlainText()
            try: 
                cmds.help(command)
                if self.file_path.endswith('.py'):
                    cmds.showHelp(f"CommandsPython/{command}.html", docs=True)

                elif self.file_path.endswith('.mel'):
                    cmds.showHelp(f"Commands/{command}.html", docs=True)
            except Exception:
                pass                          # no help page for this command -> ignore
        except Exception:
            log.exception("code editor: command-reference lookup failed")


class CodeCompleter(qt.QCompleter):
    """Namespace-aware completer built on the stdlib rlcompleter.

    Completions come from the LIVE console namespace (CONSOLE_NAMESPACE, where the editor executes
    code) plus the builtins, so it completes anything that actually exists: your own variables and
    functions, imported modules (cmds, kcore, ...), chained attributes (cmds.polyCube().<tab>), self.,
    keyword args, etc. rlcompleter introspects real objects — for 'a.b.c' it evaluates only the object
    chain before the last dot, never the user's full expression.
    """

    def __init__(self, parent=None) -> None:
        """Initialize the completer, its model, and popup styling.

        Args:
            parent: (qt.QWidget): - optional parent widget.

        Returns:
            None.
        """
        super().__init__(parent)
        self.refresh_completer = True   # kept for external compatibility
        self._info_cache = {}           # {call name: describe()}, for the argument completions

        self.setCompletionMode(qt.QCompleter.PopupCompletion)
        self.setCaseSensitivity(qt.Qt.CaseInsensitive)
        self.setModel(qt.QStandardItemModel())
        self.activated.connect(self.insert_completion)

        popup = self.popup()
        popup.setFrameShape(qt.QFrame.NoFrame)
        popup.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        popup.setIconSize(qt.QSize(qt.px(16), qt.px(16)))
        self.apply_theme()

    def apply_theme(self) -> None:
        """Paint the popup in the editor's OWN palette and at the editor's OWN font size.

        Both were hard-coded, which showed: the standalone window runs the vscode palette and the
        embedded panel the host one, and the popup drew the other palette's blue in both. The font ignored Ctrl+wheel
        for the same reason, so the list stayed 9pt over text the user had zoomed to 16.

        Returns:
            None.
        """
        popup = self.popup()
        if popup is None:
            return
        editor = self.widget()
        palette = getattr(editor, "palette_theme", None) or qt.theme()
        font = editor.font() if editor is not None else popup.font()
        popup.setFont(font)
        popup.setStyleSheet("""
            QAbstractItemView {
                border: 1px solid %(border)s;
                outline: none;
                padding: 2px;
                background-color: %(field)s;
                color: %(text)s;
            }
            QAbstractItemView::item {
                border: none;
                padding: 2px 4px;
            }
            QAbstractItemView::item:selected {
                background-color: %(accent)s;
                color: %(bright)s;
            }
            QAbstractItemView QScrollBar {
                background: none;
            }
        """ % palette)

    def _namespace(self) -> dict:
        """The namespace to introspect: the editor's own console namespace (isolated or shared), plus

        Returns:
            dict: the console namespace to introspect, plus builtins.
        builtins. Falls back to the shared the host console namespace when no editor is attached yet."""
        ns = dict(vars(builtins))
        editor = self.widget()
        try:
            ns.update(getattr(editor, "namespace", None) or CONSOLE_NAMESPACE)
        except Exception:
            pass
        return ns

    def _kind(self, name:str, obj:object) -> str:
        """Which symbol badge `name` gets in the popup - the same kinds the Outline paints.

        Returns:
            str: the badge name ('keyword', 'module', 'class', 'def' or 'var').
        """
        if keyword.iskeyword(name):
            return "keyword"
        if obj is None:
            return "var"
        if inspect.ismodule(obj):
            return "module"
        if inspect.isclass(obj):
            return "class"
        if callable(obj):
            return "def"
        return "var"

    def _owner(self, expression:str, namespace:dict) -> object:
        """The object whose attributes are being completed, for `a.b.<here>` - or None for a bare name.

        Only a plain dotted identifier is resolved: `cmds.polyCube().<tab>` is left alone rather than
        evaluated, because evaluating it would CALL polyCube just to decorate a list with icons.

        Returns:
            object: the object whose attributes are being completed, or None for a bare name.
        """
        if "." not in expression:
            return None
        base = expression.rsplit(".", 1)[0]
        if not re.match(r"^[A-Za-z_][\w.]*$", base):
            return None
        try:
            return eval(base, namespace)               # lookup only - the guard above bars any call
        except Exception:
            return None

    def _completions(self, expression:str) -> list:
        """Return [(name, kind)] for `expression` (e.g. 'cm' or 'cmds.poly').

        rlcompleter handles both a bare name (globals/builtins) and dotted access (introspects the object
        before the last dot). We de-duplicate, strip the trailing '(' rlcompleter adds to callables, and
        drop dunder names for a cleaner list. The kind comes from the object itself, so a class, a
        function and a plain value are told apart in the popup exactly as they are in the Outline.

        Returns:
            list: [(name, kind)] completions for the expression.
        """
        import rlcompleter
        namespace = self._namespace()
        completer = rlcompleter.Completer(namespace)
        owner = self._owner(expression, namespace)
        results, seen = [], set()
        i = 0
        while True:
            try:
                item = completer.complete(expression, i)
            except Exception:
                break
            if item is None:
                break
            i += 1
            name = item.rstrip("(")                    # rlcompleter appends '(' to callables
            short = name.rsplit(".", 1)[-1]            # keep only the attribute, not the full path
            if short.startswith("__") or short in seen:
                continue
            seen.add(short)
            try:
                obj = getattr(owner, short) if owner is not None else namespace.get(short)
            except Exception:
                obj = None                             # a property that raises on access, say
            results.append((short, self._kind(short, obj)))
        return results

    def text_under_cursor(self) -> str:
        """Return the word currently under the editor's text cursor.

        Returns:
            str: the word under the cursor, or an empty string.
        """
        widget = self.widget()
        if not widget:
            return ""
        cursor = widget.textCursor()
        cursor.select(qt.QTextCursor.WordUnderCursor)
        return cursor.selectedText()

    def _call_info(self, widget, name:str) -> dict:
        """describe() for `name`, cached: this runs on every keystroke inside a call.

        Returns:
            dict: the describe() info for the call.
        """
        if name in self._info_cache:
            return self._info_cache[name]
        from . import signature
        info = signature.describe(name, widget.resolve(name))
        if len(self._info_cache) > 32:
            self._info_cache.clear()
        self._info_cache[name] = info
        return info

    def _argument_matches(self, widget, prefix:str) -> list:
        """The parameters of the call the caret is inside, filtered on `prefix`.

        Same source as the QUICK HELP panel, so the popup and the panel never disagree about what a
        call takes. Off unless the host turns it on - it follows that panel's visibility.

        Returns:
            list: [(name, 'arg')] parameters of the enclosing call matching the prefix.
        """
        if not getattr(widget, "argument_completion", False) or not prefix:
            return []
        context = widget.call_context()
        if context is None:
            return []
        info = self._call_info(widget, context[0])
        low = prefix.lower()
        out = []
        for row in info.get("params", []):
            name = (list(row) + [""])[0].lstrip("*")
            if name.lower().startswith(low):
                out.append((name, "arg"))
        return sorted(set(out))

    def handle_text(self, text:str=None) -> bool:
        """Populate the completion model from the live namespace for the token left of the cursor.

        Args:
            text: (str): - current word under the cursor (unused; we read the line directly).

        Returns:
            bool: True if completions were found, False otherwise.
        """
        widget = self.widget()
        if not widget:
            return False

        line = widget.textCursor().block().text()[:widget.textCursor().positionInBlock()]
        word = re.search(r"(\w*)$", line).group(1)
        arguments = self._argument_matches(widget, word)

        # the dotted expression ending at the cursor: identifiers, dots, and () for chained calls
        match = re.search(r'([A-Za-z_][\w.]*(?:\(\))?(?:\.[\w]*)*)$', line)
        if not match:
            # no name being typed, but the arguments of the enclosing call may still apply
            return self._offer(arguments, word)
        expression = match.group(1)
        if not expression or expression.isdigit():
            return self._offer(arguments, word)

        # the prefix = what comes after the last dot (or the whole token for a bare name)
        prefix = expression.rsplit(".", 1)[-1] if "." in expression else expression
        if "." in expression:
            arguments = []                             # typing a dotted name, not an argument
        documents = [] if "." in expression else self._document_matches(widget, prefix)
        if "." not in expression and len(expression) < 2 and not arguments and not documents:
            return False                               # don't pop up on a single leading char

        matches = sorted(set((name, kind) for name, kind in self._completions(expression)
                             if name.lower().startswith(prefix.lower())))
        # arguments win a name clash: inside a call, `name` means the parameter, not some global
        taken = {name for name, _kind in arguments}
        combined = arguments + [(n, k) for n, k in matches if n not in taken]
        # document identifiers (variables typed but not executed yet) come last, deduplicated
        taken |= {name for name, _kind in combined}
        combined += [(n, k) for n, k in documents if n not in taken]
        return self._offer(combined, prefix)

    def _document_matches(self, widget, prefix:str) -> list:
        """Identifiers present in the DOCUMENT itself, so a variable is offered before the code ever ran.

        Typing `test = 10` then `print(te` proposes `test`: these names come from the text; the live
        namespace stays the authority for kinds and dotted access.

        Args:
            widget: (object): - the editor widget.
            prefix:    (str): - the typed prefix to filter on.

        Returns:
            list: [(name, "variable")] sorted document identifiers matching the prefix.
        """
        if not prefix:
            return []
        low = prefix.lower()
        names = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", widget.toPlainText()))
        return sorted((name, "variable") for name in names
                      if name.lower().startswith(low) and name != prefix and not name.startswith("__"))

    def _offer(self, matches:list, prefix:str) -> bool:
        """Put [(name, kind)] in the popup, in the order given. False when there is nothing.

        The ORDER is the caller's: arguments of the enclosing call come before names from the
        namespace, and sorting here would shuffle the two groups together.

        Returns:
            bool: True if any completions were offered, False when there is nothing.
        """
        if not matches:
            return False
        model = qt.QStandardItemModel()
        seen = set()
        for name, kind in matches:
            if name in seen:
                continue
            seen.add(name)
            row = qt.QStandardItem(qt.symbol_icon(kind), name)
            row.setEditable(False)
            model.appendRow(row)
        self.setModel(model)
        self.setCompletionPrefix(prefix)
        self.popup().setCurrentIndex(self.completionModel().index(0, 0))
        return True

    def clear_completer_list(self) -> None:
        """Reset the completion model to an empty list.

        Returns:
            None.
        """
        self.setModel(qt.QStandardItemModel())

    def insert_completion(self, completion:str=None) -> None:
        """Replace the prefix under the cursor with the chosen completion.

        Args:
            completion: (str): - completion text to insert.

        Returns:
            None.
        """
        widget = self.widget()
        if not widget or not completion:
            return
        prefix = self.completionPrefix()
        cursor = widget.textCursor()
        cursor.movePosition(qt.QTextCursor.Left, qt.QTextCursor.KeepAnchor, len(prefix))
        cursor.insertText(completion)
        widget.setTextCursor(cursor)


class SearchReplaceBar(qt.QWidget):
    """Search and replace bar that overlays the bottom of the code editor.

    Shortcuts:
        Ctrl+F  — show bar, focus search field
        Ctrl+H  — show bar, focus replace field
        Escape  — close bar
        Enter   — find next
        Shift+Enter — find previous
    """

    def __init__(self, code_edit, parent=None) -> None:
        """Build the search-and-replace bar and wire its widgets and signals.

        Args:
            code_edit: (CodeTextEdit): - editor the bar searches and edits.
            parent:    (qt.QWidget): - optional parent widget.

        Returns:
            None.
        """
        super().__init__(parent)
        self.code_edit = code_edit
        self._matches = []
        self._current = -1

        self.setStyleSheet("""
            SearchReplaceBar { background-color: #333333; border-top: 1px solid #555555; }
            QLineEdit {
                background-color: #2b2b2b; color: #d4d4d4;
                border: 1px solid #555555; padding: 2px 4px;
                font-family: Consolas; font-size: 9pt;
            }
            QLabel { color: #888888; font-family: Consolas; font-size: 9pt; }
            QToolButton {
                background: #444444; color: #d4d4d4;
                border: 1px solid #555555; padding: 2px 5px;
            }
            QToolButton:hover { background: #555555; }
            QToolButton:checked { background: #5285a6; }
            QPushButton {
                background: #444444; color: #d4d4d4;
                border: 1px solid #555555; padding: 2px 8px;
            }
            QPushButton:hover { background: #555555; }
        """)

        layout = qt.QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 3, 3)
        layout.setSpacing(4)

        self.search_field = qt.QLineEdit()
        self.search_field.setPlaceholderText("Search...")
        self.search_field.setFixedWidth(160)
        self.search_field.setFont(qt.QFont('Consolas', 9))

        self.case_btn = qt.QToolButton()
        self.case_btn.setText("Aa")
        self.case_btn.setCheckable(True)
        self.case_btn.setToolTip("Case sensitive")

        self.prev_btn = qt.QToolButton()
        self.prev_btn.setText(u"▲")
        self.prev_btn.setToolTip("Previous match (Shift+Enter)")

        self.next_btn = qt.QToolButton()
        self.next_btn.setText(u"▼")
        self.next_btn.setToolTip("Next match (Enter)")

        self.match_label = qt.QLabel("")
        self.match_label.setFont(qt.QFont('Consolas', 9))
        self.match_label.setFixedWidth(55)
        self.match_label.setAlignment(qt.Qt.AlignCenter)

        self.replace_field = qt.QLineEdit()
        self.replace_field.setPlaceholderText("Replace...")
        self.replace_field.setFixedWidth(160)
        self.replace_field.setFont(qt.QFont('Consolas', 9))

        self.replace_btn = qt.QPushButton("Replace")
        self.replace_all_btn = qt.QPushButton("All")

        self.close_btn = qt.QToolButton()
        self.close_btn.setText(u"✕")
        self.close_btn.setToolTip("Close (Escape)")

        for w in [self.search_field, self.case_btn, self.prev_btn,
                  self.next_btn, self.match_label,
                  self.replace_field, self.replace_btn, self.replace_all_btn]:
            layout.addWidget(w)
        layout.addStretch()
        layout.addWidget(self.close_btn)

        self.search_field.textChanged.connect(self.update_highlights)
        self.search_field.returnPressed.connect(self.find_next)
        self.case_btn.toggled.connect(self.update_highlights)
        self.prev_btn.clicked.connect(self.find_previous)
        self.next_btn.clicked.connect(self.find_next)
        self.replace_btn.clicked.connect(self.replace_one)
        self.replace_all_btn.clicked.connect(self.replace_all)
        self.close_btn.clicked.connect(self.close_bar)

        self.code_edit.textChanged.connect(self.on_editor_changed)

        self.setVisible(False)

    # Public API

    def show_bar(self, focus='search') -> None:
        """Show the bar, seed the search field, and focus the requested field.

        Args:
            focus: (str): - which field to focus, 'search' or 'replace'.

        Returns:
            None.
        """
        self.setVisible(True)
        selected = self.code_edit.textCursor().selectedText()
        if selected and '\n' not in selected:
            self.search_field.setText(selected)
        self.search_field.selectAll()
        if focus == 'replace':
            self.replace_field.setFocus()
        else:
            self.search_field.setFocus()
        self.update_highlights()

    def close_bar(self) -> None:
        """Hide the bar, clear search highlights, and refocus the editor.

        Returns:
            None.
        """
        self.setVisible(False)
        self.code_edit.set_search_highlights([])
        self.code_edit.setFocus()

    # Events

    def keyPressEvent(self, event) -> None:
        """Handle Escape and Enter/Shift+Enter navigation within the bar.

        Args:
            event: (qt.QKeyEvent): - Qt key press event.

        Returns:
            None.
        """
        if event.key() == qt.Qt.Key_Escape:
            self.close_bar()
        elif event.key() in (qt.Qt.Key_Return, qt.Qt.Key_Enter):
            if event.modifiers() & qt.Qt.ShiftModifier:
                self.find_previous()
            else:
                self.find_next()
        else:
            super().keyPressEvent(event)

    # Internal helpers

    def get_pattern(self) -> object:
        """Compile the search field text into a regex honoring case sensitivity.

        Returns:
            re.Pattern: compiled pattern, or None if empty or invalid.
        """
        text = self.search_field.text()
        if not text:
            return None
        flags = 0 if self.case_btn.isChecked() else re.IGNORECASE
        try:
            return re.compile(re.escape(text), flags)
        except Exception:
            return None

    def on_editor_changed(self) -> None:
        """Refresh highlights when the editor text changes while the bar is open.

        Returns:
            None.
        """
        if self.isVisible() and self.search_field.text():
            self.update_highlights()

    def update_highlights(self) -> None:
        """Recompute all match positions and update the highlight display.

        Returns:
            None.
        """
        pattern = self.get_pattern()
        if not pattern:
            self._matches = []
            self._current = -1
            self.match_label.setText("")
            self.code_edit.set_search_highlights([])
            return

        content = self.code_edit.toPlainText()
        self._matches = [(m.start(), m.end()) for m in pattern.finditer(content)]

        if self._matches:
            self._current = max(0, min(self._current, len(self._matches) - 1))
            self.rebuild_highlights()
            self.scroll_to_current()
        else:
            self._current = -1
            self.match_label.setText(u"–")
            self.code_edit.set_search_highlights([])

    def rebuild_highlights(self) -> None:
        """Rebuild the extra-selection highlights for the current matches.

        Returns:
            None.
        """
        if not self._matches:
            self.code_edit.set_search_highlights([])
            return

        doc = self.code_edit.document()
        selections = []
        for i, (start, end) in enumerate(self._matches):
            sel = qt.QTextEdit.ExtraSelection()
            cursor = qt.QTextCursor(doc)
            cursor.setPosition(start)
            cursor.setPosition(end, qt.QTextCursor.KeepAnchor)
            if i == self._current:
                sel.format.setBackground(qt.QColor(255, 165, 0))
                sel.format.setForeground(qt.QColor(0, 0, 0))
            else:
                sel.format.setBackground(qt.QColor(97, 50, 20))
                sel.format.setForeground(qt.QColor(212, 212, 212))
            sel.cursor = cursor
            selections.append(sel)

        self.code_edit.set_search_highlights(selections)
        self.match_label.setText("%d/%d" % (self._current + 1, len(self._matches)))

    def scroll_to_current(self) -> None:
        """Move the editor cursor to the current match and scroll it into view.

        Returns:
            None.
        """
        if self._current < 0 or not self._matches:
            return
        start, end = self._matches[self._current]
        doc = self.code_edit.document()
        cursor = qt.QTextCursor(doc)
        cursor.setPosition(start)
        cursor.setPosition(end, qt.QTextCursor.KeepAnchor)
        self.code_edit.setTextCursor(cursor)
        self.code_edit.ensureCursorVisible()

    # Navigation

    def find_next(self) -> None:
        """Advance to and reveal the next search match, wrapping around.

        Returns:
            None.
        """
        if not self._matches:
            self.update_highlights()
            return
        self._current = (self._current + 1) % len(self._matches)
        self.rebuild_highlights()
        self.scroll_to_current()

    def find_previous(self) -> None:
        """Move to and reveal the previous search match, wrapping around.

        Returns:
            None.
        """
        if not self._matches:
            self.update_highlights()
            return
        self._current = (self._current - 1) % len(self._matches)
        self.rebuild_highlights()
        self.scroll_to_current()

    # Replace

    def replace_one(self) -> None:
        """Replace the current match with the replacement text.

        Returns:
            None.
        """
        if not self._matches or self._current < 0:
            return
        start, end = self._matches[self._current]
        cursor = qt.QTextCursor(self.code_edit.document())
        cursor.setPosition(start)
        cursor.setPosition(end, qt.QTextCursor.KeepAnchor)
        cursor.insertText(self.replace_field.text())
        self.update_highlights()

    def replace_all(self) -> None:
        """Replace every match in the editor with the replacement text.

        Returns:
            None.
        """
        pattern = self.get_pattern()
        if not pattern:
            return
        content = self.code_edit.toPlainText()
        new_content = pattern.sub(self.replace_field.text(), content)
        if new_content != content:
            cursor = qt.QTextCursor(self.code_edit.document())
            cursor.beginEditBlock()
            cursor.movePosition(qt.QTextCursor.Start)
            cursor.movePosition(qt.QTextCursor.End, qt.QTextCursor.KeepAnchor)
            cursor.insertText(new_content)
            cursor.endEditBlock()
        self.update_highlights()


def get_syntax_format(color=None, name:str='', style:str='') -> qt.QTextCharFormat:
    """Build a text char format from a color, style name, and font styles.

    Args:
        color: (str|list): - named color string or RGB list.
        name:  (str): - style role name driving foreground/background choice.
        style: (str): - space-separated font styles, e.g. 'bold italic'.

    Returns:
        qt.QTextCharFormat: the configured character format.
    """
    _color = None
    if isinstance(color, str):
        _color = qt.QColor()
        _color.setNamedColor(color)
    if isinstance(color, list):
        _color = qt.QColor(*color)

    _format = qt.QTextCharFormat()

    if _color:
        if name != "shearch_and_replace":
            _format.setForeground(_color)
        else:
            _format.setForeground(qt.QColor(212,212,212))
            _format.setBackground(_color)
            
    if 'bold' in style:
        _format.setFontWeight(qt.QFont.Bold)
    if 'italic' in style:
        _format.setFontItalic(True)

    return _format


def python_syntax_styles(name:str) -> qt.QTextCharFormat:
    """Return the char format for a given Python syntax style role.

    Args:
        name: (str): - syntax role name, e.g. 'keyword' or 'string'.

    Returns:
        qt.QTextCharFormat: the format for the role, or None if unknown.
    """
    if name == 'keyword':
        return get_syntax_format([197,134,192], name)
    if name == 'builtins':
        return get_syntax_format([220,220,170], name)
    if name == 'builtin':
        return get_syntax_format([220,220,170], name)
    if name == 'string':
        return get_syntax_format([206,145,120], name)
    if name == 'docstring':
        return get_syntax_format([128,128,128], name, 'italic')
    if name == 'number':
        return get_syntax_format([181,206,168], name)
    if name == 'class':
        return get_syntax_format([102,201,135], name)
    if name == 'method':
        return get_syntax_format([86,156,214], name)
    if name == 'comment':
        return get_syntax_format([96,139,78], name)
    if name == 'decorator':
        return get_syntax_format([102,201,135], name, 'italic')
    if name == 'operator':
        return get_syntax_format([212,212,212], name)
    if name == 'self':
        return get_syntax_format([156,220,254], name)
    if name == 'command':
        return get_syntax_format([86,156,214], name)
    if name == 'shearch_and_replace':
        return get_syntax_format([97, 50, 20], name)
                    
                    
def mel_syntax_styles(name:str) -> qt.QTextCharFormat:
    """Return the char format for a given MEL syntax style role.

    Args:
        name: (str): - syntax role name, e.g. 'command' or 'string'.

    Returns:
        qt.QTextCharFormat: the format for the role, or None if unknown.
    """
    if name == 'keyword':
        return get_syntax_format([197,134,192], name)
    if name == 'command':
        return get_syntax_format([156,220,254], name)
    if name == 'string':
        return get_syntax_format([206,145,120], name)
    if name == 'number':
        return get_syntax_format([181,206,168], name)
    if name == 'variables':
        return get_syntax_format([96,139,78], name)
    if name == 'procs':
        return get_syntax_format([86,156,214], name)
    if name == 'comment':
        return get_syntax_format([128,128,128], name, 'italic')
    if name == 'shearch_and_replace':
        return get_syntax_format([97, 50, 20], name)
        
        
# A block's state carries the multi-line string state in its low bits and, for Python, the bracket
# nesting depth in the high ones - see PythonHighlighter.colour_brackets for why it has to live there.
_STATE_SHIFT = 8


class _MultilineStrings:
    """The triple-quoted string pass, shared by the python and MEL highlighters.

    It lived as a byte-for-byte copy in both classes, so a fix to one silently missed the other. A
    mixin rather than a common base class: each highlighter still derives from QSyntaxHighlighter
    directly, and only this one method is held in common.
    """

    def match_multiline(self, text, delimiter, in_state, style) -> bool:
        """Highlight a triple-quoted multi-line string spanning block states.

        Args:
            text:      (str): - text of the current document block.
            delimiter: (object): - compiled regex matching the string delimiter.
            in_state:  (int): - block state flagging an open multi-line string.
            style:     (qt.QTextCharFormat): - format applied to the string.

        Returns:
            bool: True if the block ends inside a multi-line string.
        """
        # low bits only: colour_brackets parks the bracket nesting depth in the high ones. Harmless
        # where no depth is stored - a state of 0..2 is unchanged by the modulo.
        if self.previousBlockState() % _STATE_SHIFT == in_state:
            start, add = 0, 0
        else:
            if qt.__qt__ == "pyside6":
                match = delimiter.match(text)
                start = match.capturedStart()
                add = match.capturedLength() if match.hasMatch() else -1
            else:
                start = delimiter.indexIn(text)
                add = delimiter.matchedLength()

            if start in self.tripleQuoutesWithinStrings:
                return False

        while start >= 0:
            if qt.__qt__ == "pyside6":
                end_match = delimiter.match(text, start + add)
                end = end_match.capturedStart() if end_match.hasMatch() else -1
                end_length = end_match.capturedLength()
            else:
                end = delimiter.indexIn(text, start + add)
                end_length = delimiter.matchedLength()

            if end >= add:
                length = end - start + add + end_length
                self.setCurrentBlockState(0)
            else:
                self.setCurrentBlockState(in_state)
                length = len(text) - start + add
            self.setFormat(start, length, style)

            if qt.__qt__ == "pyside6":
                start_match = delimiter.match(text, start + length)
                start = start_match.capturedStart() if start_match.hasMatch() else -1
            else:
                start = delimiter.indexIn(text, start + length)

        return self.currentBlockState() == in_state

class PythonHighlighter(_MultilineStrings, qt.QSyntaxHighlighter):
    """Syntax highlighter that colorizes Python source in the code editor."""

    keywords = [
        'and', 'assert', 'break', 'class', 'continue', 'def',
        'del', 'elif', 'else', 'except', 'exec', 'finally',
        'for', 'from', 'global', 'if', 'import', 'in',
        'is', 'lambda', 'not', 'or', 'pass',
        'raise', 'return', 'try', 'while', 'yield',
        'None', 'True', 'False', 'as',
    ]   # 'print' is not a keyword in py3; it is coloured as a builtin (same as len), via dir(builtins)

    operators = [
        '=',
        '==', '!=', '<', '<=', '>', '>=',
        r'\+', '-', r'\*', '/', '//', r'\%', r'\*\*',
        r'\+=', '-=', r'\*=', '/=', r'\%=',
        r'\^', r'\|', r'\&', r'\~', '>>', '<<',
    ]

    builtins = dir(builtins)
               

    def __init__(self, parent=None, shearch_and_replace:list=None) -> None:
        """Compile the Python highlighting rules and multi-line string states.

        Args:
            parent:              (qt.QTextDocument): - document to highlight.
            shearch_and_replace: (list): - extra patterns to highlight as matches.

        Returns:
            None.
        """
        super().__init__(parent)


        # Multi-line strings (expression, flag, style)
        #   self.tri_single = (qt.QtCore.QRegularExpression("'''"), 1, python_syntax_styles('docstring')) if qt.__qt__ == "pyside6" else (qt.QtCore.QRegExp("'''"), 1, python_syntax_styles('docstring'))
        #   self.tri_double = (qt.QtCore.QRegularExpression('"""'), 1, python_syntax_styles('docstring')) if qt.__qt__ == "pyside6" else (qt.QtCore.QRegExp('"""'), 2, python_syntax_styles('docstring'))
        # Multi-line strings (expression, state, style)
        if qt.__qt__ == "pyside6":
            self.tri_single = (qt.QtCore.QRegularExpression("'''"), 1, python_syntax_styles('docstring'))
            self.tri_double = (qt.QtCore.QRegularExpression('"""'), 2, python_syntax_styles('docstring'))
        else:
            self.tri_single = (qt.QtCore.QRegExp("'''"), 1, python_syntax_styles('docstring'))
            self.tri_double = (qt.QtCore.QRegExp('"""'), 2, python_syntax_styles('docstring'))

        self.shearch_and_replace = list(shearch_and_replace or [])

        # what colour_brackets treats as "already a literal": a bracket painted in one of these has
        # been claimed by a string or a comment rule, and must not be counted as nesting
        self._literal_colours = {
            python_syntax_styles(name).foreground().color().name()
            for name in ("string", "docstring", "comment")}

        rules = []
        
        # 'methods' followed by a (
        rules += [(r'\b(\w+)\s*(?=\()', 0, python_syntax_styles('method'))]
        
        # Keyword, operator, and brace rules
        rules += [(r'%s' % o, 0, python_syntax_styles('operator')) for o in PythonHighlighter.operators]

        # builtins and keywords as ONE alternation each (not one regex per word), so highlightBlock scans a
        # couple of patterns per line instead of ~180 — a big per-keystroke win on large files.
        if PythonHighlighter.builtins:
            rules += [(r'\b(?:%s)\b' % '|'.join(re.escape(b) for b in PythonHighlighter.builtins), 0, python_syntax_styles('builtin'))]
        if PythonHighlighter.keywords:
            rules += [(r'\b(?:%s)\b' % '|'.join(PythonHighlighter.keywords), 0, python_syntax_styles('keyword'))]
                           
        # All other rules
        rules += [
            # 'self'
            (r'\bself\b', 0, python_syntax_styles('self')),
            
            # '.' followed by an identifier
            (r'\.(\w+)\(', 1, python_syntax_styles('command')),

            # 'def' followed by an identifier
            (r'\bdef\b\s*(\w+)', 1, python_syntax_styles('method')),
            
            # 'class' followed by an identifier
            (r'\bclass\b\s*(\w+)', 1, python_syntax_styles('class')),

            # Numeric literals
            (r'\b[+-]?[0-9]+[lL]?\b', 0, python_syntax_styles('number')),
            (r'\b[+-]?0[xX][0-9A-Fa-f]+[lL]?\b', 0, python_syntax_styles('number')),
            (r'\b[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\b', 0, python_syntax_styles('number')),

            # Double-quoted string, possibly containing escape sequences
            (r'"[^"\\]*(\\.[^"\\]*)*"', 0, python_syntax_styles('string')),
            # Single-quoted string, possibly containing escape sequences
            (r"'[^'\\]*(\\.[^'\\]*)*'", 0, python_syntax_styles('string')),

            # From '#' until a newline
            (r'#[^\n]*', 0, python_syntax_styles('comment')),
            
            # From '@' until a newline
            (r'@[^\n]*', 0, python_syntax_styles('decorator')),
        ]

        rules += [(r'%s' % o, 0, python_syntax_styles('shearch_and_replace')) for o in self.shearch_and_replace]
        
        self.rules = [(qt.QtCore.QRegularExpression(pat), index, fmt) for (pat, index, fmt) in rules] if qt.__qt__ == "pyside6" else [(qt.QtCore.QRegExp(pat), index, fmt) for (pat, index, fmt) in rules]
        

    def highlightBlock(self, text) -> None:
        """Apply Python syntax rules and multi-line string states to a block.

        Args:
            text: (str): - text of the current document block.

        Returns:
            None.
        """
        self.tripleQuoutesWithinStrings = []
        for expression, nth, format in self.rules:
            if qt.__qt__ == "pyside6":
                regex_match = expression.globalMatch(text)
                while regex_match.hasNext():
                    match = regex_match.next()
                    index = match.capturedStart(nth)
                    length = len(match.captured(nth))
                    if index >= 0:
                        self.setFormat(index, length, format)
            else:
                index = expression.indexIn(text, 0)
                while index >= 0:
                    index = expression.pos(nth)
                    length = len(expression.cap(nth))
                    self.setFormat(index, length, format)
                    index = expression.indexIn(text, index + length)

        self.setCurrentBlockState(0)

        in_multiline = self.match_multiline(text, *self.tri_single)
        if not in_multiline:
            in_multiline = self.match_multiline(text, *self.tri_double)

        self.colour_brackets(text)

    # VS Code's default bracket-pair colours, cycled by nesting depth, plus red for an orphan closer
    BRACKET_COLOURS = ("#ffd700", "#da70d6", "#179fff")
    BRACKET_ORPHAN  = "#e4676b"

    def colour_brackets(self, text) -> None:
        """Tint () [] {} by nesting depth and carry that depth on to the next line.

        The depth is parked in the HIGH bits of the block state because Qt only re-highlights the
        blocks below when a block's state changes: kept anywhere else, a depth change would not
        travel down the file after an edit, and everything under a newly opened bracket would keep
        the colours of the old nesting until the whole document was rehighlighted.

        match_multiline owns the low bits and reads them modulo STATE_SHIFT, so the two coexist.
        This runs LAST, once the rules and the multi-line pass have formatted the block, which is how
        a bracket inside a string or a comment is recognised and left alone.

        Returns:
            None.
        """
        previous = self.previousBlockState()
        depth = max(0, previous // _STATE_SHIFT) if previous > 0 else 0
        base = max(0, self.currentBlockState())

        for index, char in enumerate(text):
            if char not in "()[]{}" or self._is_literal(index):
                continue
            if char in "([{":
                colour = self.BRACKET_COLOURS[depth % len(self.BRACKET_COLOURS)]
                depth += 1
            elif depth > 0:
                depth -= 1
                colour = self.BRACKET_COLOURS[depth % len(self.BRACKET_COLOURS)]
            else:
                colour = self.BRACKET_ORPHAN            # a closer with nothing open before it
            fmt = qt.QTextCharFormat()
            fmt.setForeground(qt.QColor(colour))
            self.setFormat(index, 1, fmt)

        self.setCurrentBlockState(base + depth * _STATE_SHIFT)

    def _is_literal(self, index:int) -> bool:
        """True if the character at `index` was already formatted as a string or a comment.

        Returns:
            bool: True if the character is inside a string or a comment.
        """
        return self.format(index).foreground().color().name() in self._literal_colours

    
                    
class MelHighlighter(_MultilineStrings, qt.QSyntaxHighlighter):
    """Syntax highlighter that colorizes MEL source in the code editor."""

    keywords = [
        'true', 'false', 'if', 'else', 'elseif', 'while', 'for', 'switch',
        'case', 'default', 'break', 'continue', 'return', 'proc', 'global',
        'local', 'int', 'float', 'string', 'vector', 'matrix', 'point', 'array', 'in'
    ]

    _commands = None                            # cached MEL command list (loaded lazily, once)

    @classmethod
    def mel_commands(cls) -> list:
        """The list of MEL commands, loaded from Maya on first use and cached.

        Loading it here (not at class-definition time) means importing this module never requires a live
        Maya session, and the (expensive) melInfo query runs at most once for the whole editor.

        Returns:
            list: the MEL command names.
        """
        if cls._commands is None:
            try:
                import maya.cmds as cmds
                cls._commands = cmds.melInfo(query=True, all=True) or []
            except Exception:
                cls._commands = []
        return cls._commands


    def __init__(self, parent=None, shearch_and_replace:list=None) -> None:
        """Compile the MEL highlighting rules and multi-line string states.

        Args:
            parent:              (qt.QTextDocument): - document to highlight.
            shearch_and_replace: (list): - extra patterns to highlight as matches.

        Returns:
            None.
        """
        super().__init__(parent)


        # Multi-line strings (expression, flag, style)
        self.tri_single = (qt.QtCore.QRegularExpression("'''"), 1, mel_syntax_styles('comment')) if qt.__qt__ == "pyside6" else (qt.QtCore.QRegExp("'''"), 1, mel_syntax_styles('comment'))
        self.tri_double = (qt.QtCore.QRegularExpression('"""'), 2, mel_syntax_styles('comment')) if qt.__qt__ == "pyside6" else (qt.QtCore.QRegExp('"""'), 2, mel_syntax_styles('comment'))
        self.shearch_and_replace = list(shearch_and_replace or [])
        
        rules = []

        # thousands of MEL commands as ONE alternation regex (not one regex each), scanned once per line.
        commands = MelHighlighter.mel_commands()
        if commands:
            rules += [(r'\b(?:%s)\b' % '|'.join(re.escape(c) for c in commands), 0, mel_syntax_styles('command'))]

        # 'procs' followed by a (
        rules += [(r'\b(\w+)\s*(?=\()', 0, mel_syntax_styles('procs'))]

        # Keyword rules as one alternation
        if MelHighlighter.keywords:
            rules += [(r'\b(?:%s)\b' % '|'.join(MelHighlighter.keywords), 0, mel_syntax_styles('keyword'))]
                           
        # All other rules
        rules += [
            # '$' followed by an identifier
            (r'\$\w+', 0, mel_syntax_styles('variables')),
            
            # '.' followed by an identifier
            (r'\.(\w+)\(', 1, mel_syntax_styles('procs')),

            # 'def' followed by an identifier
            (r'\bproc\b\s*(\w+)', 1, mel_syntax_styles('procs')),

            # Numeric literals
            (r'\b[+-]?[0-9]+[lL]?\b', 0, mel_syntax_styles('number')),
            (r'\b[+-]?0[xX][0-9A-Fa-f]+[lL]?\b', 0, mel_syntax_styles('number')),
            (r'\b[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\b', 0, mel_syntax_styles('number')),

            # Double-quoted string, possibly containing escape sequences
            (r'"[^"\\]*(\\.[^"\\]*)*"', 0, mel_syntax_styles('string')),
            # Single-quoted string, possibly containing escape sequences
            (r"'[^'\\]*(\\.[^'\\]*)*'", 0, mel_syntax_styles('string')),

            # From '//' until a newline
            (r'//[^\n]*', 0, mel_syntax_styles('comment')),
        ]

        rules += [(r'%s' % o, 0, mel_syntax_styles('shearch_and_replace')) for o in self.shearch_and_replace]
        self.rules = [(qt.QtCore.QRegularExpression(pat), index, fmt) for (pat, index, fmt) in rules] if qt.__qt__ == "pyside6" else [(qt.QtCore.QRegExp(pat), index, fmt) for (pat, index, fmt) in rules]
    
    
    def highlightBlock(self, text) -> None:
        """Apply MEL syntax rules and multi-line string states to a block.

        Args:
            text: (str): - text of the current document block.

        Returns:
            None.
        """
        self.tripleQuoutesWithinStrings = []

        for expression, nth, format in self.rules:
            if qt.__qt__ == "pyside6":
                # PySide6: Use QRegularExpression
                regex_match = expression.globalMatch(text)
                while regex_match.hasNext():
                    match = regex_match.next()
                    index = match.capturedStart(nth)
                    
                    if expression.pattern() in [r'"[^"\\]*(\\.[^"\\]*)*"', r"'[^'\\]*(\\.[^'\\]*)*'"]:
                        innerIndex = self.tri_single[0].match(text, index + 1).capturedStart()
                        if innerIndex == -1:
                            innerIndex = self.tri_double[0].match(text, index + 1).capturedStart()
                        
                        if innerIndex != -1:
                            tripleQuoteIndexes = range(innerIndex, innerIndex + 3)
                            self.tripleQuoutesWithinStrings.extend(tripleQuoteIndexes)

                    if index in self.tripleQuoutesWithinStrings:
                        continue
                        
                    length = len(match.captured(nth))
                    self.setFormat(index, length, format)
            else:
                index = expression.indexIn(text, 0)
                while index >= 0:
                    if expression.pattern() in [r'"[^"\\]*(\\.[^"\\]*)*"', r"'[^'\\]*(\\.[^'\\]*)*'"]:
                        innerIndex = self.tri_single[0].indexIn(text, index + 1)
                        if innerIndex == -1:
                            innerIndex = self.tri_double[0].indexIn(text, index + 1)
                        
                        if innerIndex != -1:
                            tripleQuoteIndexes = range(innerIndex, innerIndex + 3)
                            self.tripleQuoutesWithinStrings.extend(tripleQuoteIndexes)

                    if index in self.tripleQuoutesWithinStrings:
                        index += 1
                        expression.indexIn(text, index)
                        continue
                    
                    index = expression.pos(nth)
                    length = len(expression.cap(nth))
                    self.setFormat(index, length, format)
                    index = expression.indexIn(text, index + length)

        self.setCurrentBlockState(0)

        in_multiline = self.match_multiline(text, *self.tri_single)
        if not in_multiline:
            in_multiline = self.match_multiline(text, *self.tri_double)


    
    