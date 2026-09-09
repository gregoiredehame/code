"""
CODE EDITOR.

Author: Gregoire Dehame
Created: Jul 21, 2026
Modified: Sep 08, 2026
Module: code_editor.window
Execute: from code_editor import window

The standalone VS-Code-style code editor window: a dockable Maya window with a top menu bar
(File/Edit/Selection/View/Go/Run), a left sidebar (Workspace / Outline / Timeline), a script tab bar and
the reused Maya output console. Its own isolated execution namespace (not the shared the host console).
"""

import os
import sys
import ast
import json
import builtins
import traceback
import subprocess

from maya.app.general.mayaMixin import MayaQWidgetDockableMixin

# built entirely on the self-contained engine in core (no dependency on the rest of the host)
from .core import qt
from .core import compat
from .core import vcs
from .core import signature
from .core import seti_map
from .core import languages
from .core import find
from .core import palette
from .core import lint
from .core import session
from .core import editor as editor_module
from .core import console as output_widget

WORKSPACE_OPTIONVAR = "code_editor_workspace_folders"   # json list of folder paths

# exact VS Code default gitDecoration.*ResourceForeground values
_STATUS_COLOR = {
    "M": "#e2c08d",   # modified  (gold)
    "A": "#73c991",   # added / staged (green — VS Code untracked green)
    "U": "#73c991",   # untracked (green)
    "D": "#c74e39",   # deleted   (red)
    "R": "#e2c08d",   # renamed   (gold)
    "C": "#e4676b",   # conflict  (red)
    "S": "#8db9e2",   # submodule (blue)
}
_STATUS_TOOLTIP = {
    "M": "Modified", "A": "Added", "U": "Untracked",
    "D": "Deleted", "R": "Renamed", "C": "Conflict", "S": "Submodule",
}

__icons__ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "core", "icons")


def _icon(name:str) -> "qt.QIcon":
    """Load a tree icon by file name from core/icons.

    Returns:
        'qt.QIcon': the icon built from core/icons/<name>.
    """
    return qt.QIcon(os.path.join(__icons__, name))


_PIN_ICON = None


def _pin_icon() -> "qt.QIcon":
    """A small drawn push-pin (a round head over a needle) for pinned tabs, no icon file needed.

    Returns:
        'qt.QIcon': a cached push-pin icon.
    """
    global _PIN_ICON
    if _PIN_ICON is not None:
        return _PIN_ICON
    size = qt.px(16)
    unit = size / 16.0
    pixmap = qt.QPixmap(size, size)
    pixmap.fill(qt.Qt.transparent)
    painter = qt.QPainter(pixmap)
    painter.setRenderHint(qt.QPainter.Antialiasing, True)
    colour = qt.QColor("#c8c8c8")
    painter.setPen(qt.QPen(colour, max(1.0, 1.4 * unit)))
    painter.drawLine(qt.QPointF(8 * unit, 9 * unit), qt.QPointF(8 * unit, 13 * unit))   # needle
    painter.setPen(qt.Qt.NoPen)
    painter.setBrush(colour)
    painter.drawEllipse(qt.QRectF(4.5 * unit, 3 * unit, 7 * unit, 7 * unit))            # head
    painter.end()
    _PIN_ICON = qt.QIcon(pixmap)
    return _PIN_ICON


def diff_hunks(old:str, new:str) -> list:
    """Where `new` differs from `old`, as [(first, last, kind, old_lines)] into NEW, 0-based.

    difflib rather than `git diff`: the old text is already in hand from `git show`, and comparing
    it here costs no extra process. Kinds follow VS Code's gutter - added, modified, deleted - and a
    deletion is reported against the line it happened above, since no line survives to mark.

    `old_lines` is what HEAD had there, carried along so the gutter can show it and put it back
    without asking git a second question.

    Returns:
        list: the [(first, last, kind, old_lines)] hunks into NEW, 0-based.
    """
    import difflib
    hunks = []
    was = (old or "").split("\n")
    matcher = difflib.SequenceMatcher(None, was, (new or "").split("\n"))
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            hunks.append((max(0, j1 - 1), max(0, j1 - 1), "deleted", was[i1:i2]))
        else:
            hunks.append((j1, max(j1, j2 - 1), "added" if tag == "insert" else "modified",
                          was[i1:i2]))
    return hunks


# ----------------------------------------------------------------------------------------------- tabs

class Breadcrumbs(qt.QWidget):
    """The path strip over the editor - VS Code's breadcrumbs: the host > core > constraint.py > build.

    Two halves that refresh on different events: the FOLDERS and the file change only when the tab
    does, the SYMBOLS follow the caret. The symbol half is rebuilt only when the enclosing scope
    actually changes, not on every keypress, or moving along one line would tear down and rebuild a
    row of widgets for nothing.
    """

    fileActivated   = qt.signal(str)      # a file was picked from a crumb's drop-down
    revealRequested = qt.signal(str)      # "Reveal in File Explorer", from the foot of that menu
    symbolActivated = qt.signal(int)      # a symbol was clicked: 0-based line
    SKIP = {".git", "__pycache__", "node_modules", ".vs", ".vscode", ".idea"}

    def __init__(self, parent:qt.QWidget=None) -> None:
        """Build the empty breadcrumb strip with its horizontal layout and trailing stretch.

        Returns:
            None.
        """
        super().__init__(parent)
        self.setObjectName("codeBreadcrumbs")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)
        self._path_crumbs = []            # widgets for the folders + file half
        self._symbol_crumbs = []          # widgets for the symbol half
        self._symbol_key = None           # what the symbol half currently shows

        self._row = qt.QHBoxLayout(self)
        self._row.setContentsMargins(qt.px(8), 0, qt.px(8), 0)
        self._row.setSpacing(0)
        self._row.addStretch(1)

    def minimumSizeHint(self) -> qt.QSize:
        """Never gate the window's width.

        A row of buttons reports the SUM of their widths as its minimum, so a deep path -
        `project > ui > code_editor > window.py > Editor > __init__` - became a floor the whole
        window could not be dragged below. This is chrome: it clips instead.

        Returns:
            qt.QSize: a size with zero width and the layout's own minimum height.
        """
        return qt.QSize(0, super().minimumSizeHint().height())

    def _crumb(self, text:str, icon=None, slot=None) -> qt.QToolButton:
        """Build a breadcrumb tool button, optionally with an icon and a click slot.

        Returns:
            qt.QToolButton: the configured crumb button (disabled when no slot is given).
        """
        button = qt.QToolButton()
        button.setObjectName("codeCrumb")
        button.setText(text)
        button.setAutoRaise(True)
        if icon is not None:
            button.setIcon(icon)
            button.setIconSize(qt.QSize(qt.px(14), qt.px(14)))
            button.setToolButtonStyle(qt.Qt.ToolButtonTextBesideIcon)
        if slot is not None:
            # the slot is handed the button: a drop-down has to be placed under the crumb that
            # opened it, and only the caller knows which one that is
            button.clicked.connect(lambda *_, b=button: slot(b))
        else:
            button.setEnabled(False)
        return button

    def _separator(self) -> qt.QLabel:
        """The chevron between crumbs.

        The package's own chevron_right.png, not a text "›": the glyph came out tiny and its size
        depended on whatever font the host had, while the icon is the same one the trees use and
        scales with the DPI setting like everything else.

        Returns:
            qt.QLabel: a label carrying the chevron_right pixmap.
        """
        label = qt.QLabel()
        label.setObjectName("codeCrumbSep")
        label.setPixmap(_icon("chevron_right.png").pixmap(qt.px(14), qt.px(14)))
        return label

    def _menu_for(self, folder:str, button:qt.QWidget) -> None:
        """Drop the folder open under its crumb, so another file can be opened from there.

        VS Code's behaviour, and the one worth having: the crumb is a way INTO the folder. Sending
        the path to the OS file browser instead, as this did, took you out of the editor entirely -
        that is still available, at the foot of the menu.

        Returns:
            None.
        """
        menu = qt.QMenu(self)
        self._fill(menu, folder)
        if not menu.isEmpty():
            menu.addSeparator()
        menu.addAction("Reveal in File Explorer", lambda: self.revealRequested.emit(folder))
        point = button.mapToGlobal(qt.QPoint(0, button.height()))
        menu.exec_(point) if hasattr(menu, "exec_") else menu.exec(point)

    def _fill(self, menu:qt.QMenu, folder:str) -> None:
        """List `folder`: sub-folders first as sub-menus, then the files, each opening a tab.

        Returns:
            None.
        """
        try:
            names = sorted(os.listdir(folder), key=lambda n: n.lower())
        except OSError:
            return
        folders = [n for n in names if os.path.isdir(os.path.join(folder, n))]
        files = [n for n in names if not os.path.isdir(os.path.join(folder, n))]

        for name in folders:
            if name in self.SKIP:
                continue
            full = os.path.join(folder, name)
            sub = menu.addMenu(name)
            # filled on demand: listing the whole tree up front would stat every folder below this
            # one just to open a menu
            sub.aboutToShow.connect(lambda m=sub, p=full: self._fill_once(m, p))
        if folders and files:
            menu.addSeparator()
        for name in files:
            if name.startswith("."):
                continue
            full = os.path.join(folder, name)
            action = menu.addAction(file_icon(full), name)
            action.triggered.connect(lambda *_, p=full: self.fileActivated.emit(p))

    def _fill_once(self, menu:qt.QMenu, folder:str) -> None:
        """Fill a sub-menu the first time it is shown, and leave it alone afterwards.

        Returns:
            None.
        """
        if menu.isEmpty():
            self._fill(menu, folder)

    def _drop(self, widgets:list) -> None:
        """Drop.

        Returns:
            None.
        """
        for widget in widgets:
            self._row.removeWidget(widget)
            widget.setParent(None)
            widget.deleteLater()
        del widgets[:]

    def _insert(self, index:int, widget, store:list) -> int:
        """Put `widget` at `index`, remember it in `store`, and return the next index.

        The trailing stretch was added first and so always sits last; inserting by index keeps every
        crumb to the left of it.

        Returns:
            int: the next insertion index (index + 1).
        """
        self._row.insertWidget(index, widget, 0)
        store.append(widget)
        return index + 1

    def set_path(self, file_path:str=None, roots:list=None) -> None:
        """Rebuild the folder / file half for `file_path`, shown relative to its workspace root.

        Returns:
            None.
        """
        self._drop(self._path_crumbs)
        self._drop(self._symbol_crumbs)   # the symbols belong to the old file
        self._symbol_key = None
        if not file_path:
            return

        folder = os.path.dirname(file_path)
        # the deepest workspace root containing the file: the crumb trail should start where the
        # workspace does, not at the drive letter
        root = ""
        for candidate in sorted(roots or [], key=len, reverse=True):
            if folder == candidate or folder.startswith(candidate + os.sep):
                root = candidate
                break
        if root:
            parts = [os.path.basename(root.rstrip("/\\")) or root]
            rest = os.path.relpath(folder, root)
            if rest not in (".", ""):
                parts += rest.split(os.sep)
        else:
            parts = [p for p in folder.replace("\\", "/").split("/")[-2:] if p]

        # what each crumb reveals, found by walking UP from the folder rather than rebuilding the
        # path downwards: `parts` is always the tail of `folder`, whether it came from a workspace
        # root or from the fallback, and joining forwards doubled a segment in the fallback case
        targets, walk = [], folder
        for _ in parts:
            targets.append(walk)
            walk = os.path.dirname(walk)
        targets.reverse()

        at = 0
        for part, target in zip(parts, targets):
            at = self._insert(at, self._crumb(
                part, slot=lambda b, p=target: self._menu_for(p, b)), self._path_crumbs)
            at = self._insert(at, self._separator(), self._path_crumbs)

        # the file crumb opens ITS OWN folder, so the drop-down lists the files beside this one -
        # which is the quickest way to the next tab
        # file_icon is defined further down the module; it resolves at call time, not at import
        at = self._insert(at, self._crumb(
            os.path.basename(file_path), icon=file_icon(file_path),
            slot=lambda b, p=folder: self._menu_for(p, b)), self._path_crumbs)

    def set_symbols(self, chain:list) -> None:
        """Show the class/def chain around the caret. `chain` is [(first, last, kind, text, name)].

        Returns:
            None.
        """
        key = tuple((entry[0], entry[4]) for entry in chain)
        if key == self._symbol_key:
            return                        # same scope: leave the widgets alone
        self._symbol_key = key
        self._drop(self._symbol_crumbs)
        at = len(self._path_crumbs)       # the symbols carry on where the path half stopped
        for entry in chain:
            at = self._insert(at, self._separator(), self._symbol_crumbs)
            line = entry[0]
            at = self._insert(at, self._crumb(
                entry[4], icon=qt.symbol_icon(entry[2]),
                slot=lambda _b, n=line: self.symbolActivated.emit(n)), self._symbol_crumbs)


class EditorPage(qt.QWidget):
    """One script tab: a CodeTextEdit and its floating find panel, saved straight to disk."""

    def __init__(self, file_path:str=None, namespace:dict=None, surface:str=None, palette:dict=None, parent:qt.QWidget=None) -> None:
        """Build an editor page for `file_path` (None = untitled), sharing `namespace` for execution.

        `surface` is the owning editor's code-area colour, passed down rather than read from a module
        global so a standalone window and an embedded panel can run different themes side by side.

        Returns:
            None.
        """
        super().__init__(parent)
        self.file_path = file_path

        layout = qt.QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # the crumb strip sits in its OWN row above the code, not floating over it: it is chrome, and
        # text scrolling under it would be unreadable
        self.crumbs = Breadcrumbs()
        layout.addWidget(self.crumbs, 0, 0)

        # `palette` is the whole colour set, not just the code surface: the completion popup is a
        # separate top-level window and has to paint its own border, field and accent
        self.code = editor_module.CodeTextEdit(file_path=file_path, namespace=namespace,
                                               minimap=True, surface=surface, palette=palette)
        self.code.set_completer(editor_module.CodeCompleter)
        # a reference data's .py is OS-locked read-only (edit it in the source krig): open it read-only here too,
        # so it shows greyed / non-editable and matches the file lock instead of failing only on save.
        if file_path and os.path.isfile(file_path) and not os.access(file_path, os.W_OK):
            self.code.setReadOnly(True)
        # the engine only knows python and mel; swap in a rule highlighter for the other languages
        extra = languages.rules_for(os.path.splitext(file_path or "")[1])[0]
        if extra:
            self.code.highlighter = languages.attach(self.code.document(), file_path)
            # a fresh QSyntaxHighlighter queues its first pass, which would land AFTER the caller has
            # connected to textChanged and read as a user edit; do it now so nothing stays pending
            self.code.highlighter.rehighlight()
        # the code area is the editor surface; the side bars and terminal sit on the lighter panel
        # one. The widget's own stylesheet belongs to set_text_size (see there), so nothing is set
        # on it here - doing so would drop the font size and break Ctrl+wheel zooming.
        self.code.setObjectName("codeEdit")
        layout.addWidget(self.code, 1, 0)
        self.crumbs.symbolActivated.connect(lambda line: self.code.go_to_block(line, top=False))
        self.code.cursorPositionChanged.connect(self._follow_crumbs)

        # the find panel floats OVER the editor rather than sitting in a row under it: a bar at the
        # bottom shifts the text up every time it opens, moving the line you were reading
        self.search = find.FindReplace(self.code)

        # Ctrl+F / Ctrl+H are NOT bound here: the Edit menu binds them at window level, and two
        # shortcuts matching the same key make Qt call it ambiguous and fire neither.
        escape = qt.QShortcut(qt.QKeySequence("Esc"), self.code)
        escape.setContext(qt.Qt.WidgetWithChildrenShortcut)
        escape.activated.connect(self._escape)

        self.code.document().setModified(False)

    def _escape(self) -> None:
        """Escape backs out one thing at a time: the extra carets first, then the find panel.

        A QShortcut wins over the editor's own keyPressEvent, so this has to be decided here - left
        to the editor, Escape would close the panel while a dozen carets stayed on screen.

        Returns:
            None.
        """
        if self.code.extra_cursors:
            self.code.clear_extra_cursors()
            return
        self.search.close_panel()

    def _follow_crumbs(self) -> None:
        """Keep the symbol half of the crumb trail on the scope holding the caret.

        Returns:
            None.
        """
        line = self.code.textCursor().blockNumber()
        chain = [entry for entry in self.code._sticky_map if entry[0] <= line <= entry[1]]
        self.crumbs.set_symbols(chain)

    def name(self) -> str:
        """The tab label: the file's basename, or 'untitled'.

        Returns:
            str: the file's basename, or 'untitled' when there is no path.
        """
        return os.path.basename(self.file_path) if self.file_path else "untitled"

    def is_modified(self) -> bool:
        """Whether the editor document has unsaved changes.

        Returns:
            bool: True when the document has unsaved modifications.
        """
        return self.code.document().isModified()

    def save(self, path:str=None) -> bool:
        """Write the editor text to `path` (or self.file_path). Returns True on success.

        The write goes through a temp file swapped in with os.replace, so a failure mid-save leaves the
        original untouched instead of truncated.

        Returns:
            bool: True when the file was written, False otherwise.
        """
        path = path or self.file_path
        if not path:
            return False
        try:
            session.write_atomic(path, self.code.toPlainText())
            self.file_path = path
            self.code.file_path = path
            self.code.document().setModified(False)
            return True
        except Exception as exception:
            compat.message.critical(title="Save Error", buttons=["Close"],
                                    message_text="Could not save the file.", informative_text=str(exception), parent=self)
            return False


class PreviewTabBar(qt.QTabBar):
    """Tab bar that italicises 'preview' tabs, the ones opened by a single click in the tree.

    Qt has no per-tab font, and setting one on the painter does not survive CE_TabBarTabLabel: the
    stylesheet rule carries a font-size, so the style rebuilds the font from the widget's and the italic
    is dropped. Icon and text are therefore laid out here, and only the tab SHAPE comes from the style.
    """

    LEFT     = 14                               # matches the QTabBar::tab padding in the stylesheet
    RIGHT    = 10
    SPACING  = 6                                # between the icon and the label
    LABEL    = "#9d9d9d"
    LABEL_ON = "#ffffff"
    FONT     = 13                               # must match the QTabBar::tab rule in the stylesheet

    def __init__(self, parent:qt.QWidget=None) -> None:
        """Set up the tab bar with its pinned label font and default preview predicate.

        Returns:
            None.
        """
        super().__init__(parent)
        self.is_preview = lambda index: False       # replaced by the window
        self.is_pinned = lambda index: False        # replaced by the window: draws a pin glyph
        self.mover = None                           # window callback: move a tab between panes on drop
        self._press = None                          # (index, pos) of a left press, to start a cross-pane drag
        self._drop_at = -1                          # insertion index under a hovering drag (-1 = none)
        self.setAcceptDrops(True)
        font = self.font()                          # the labels are hand-drawn: pin the size ourselves
        font.setPixelSize(self.FONT)
        self.setFont(font)

    MIME = "application/x-kata-tab"
    _dragging = None                                # (source_bar, index) while a cross-pane drag is in flight

    @staticmethod
    def _drop_pos(event) -> "qt.QPoint":
        """The drop position as a QPoint, across PySide2 (pos) and PySide6 (position)."""
        return event.position().toPoint() if hasattr(event, "position") else event.pos()

    def mousePressEvent(self, event) -> None:
        """Record a left press so a drag that leaves the bar can move the tab to another pane.

        Returns:
            None.
        """
        if event.button() == qt.Qt.LeftButton:
            self._press = (self.tabAt(event.pos()), event.pos())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        """Start a cross-pane drag once the cursor leaves the bar; inside it, native reordering runs.

        Returns:
            None.
        """
        if (self._press and (event.buttons() & qt.Qt.LeftButton)
                and self._press[0] >= 0 and not self.rect().contains(event.pos())):
            index, self._press = self._press[0], None
            PreviewTabBar._dragging = (self, index)
            data = qt.QtCore.QMimeData()
            data.setData(self.MIME, b"1")
            drag = qt.QDrag(self)
            drag.setMimeData(data)
            drag.setPixmap(self.grab(self.tabRect(index)))
            (drag.exec_ if hasattr(drag, "exec_") else drag.exec)(qt.Qt.MoveAction)
            PreviewTabBar._dragging = None
            return
        super().mouseMoveEvent(event)

    def _drop_index(self, pos) -> int:
        """The insertion index under `pos`: before/after a tab by its centre, or the end past the last.

        Returns:
            int: the insertion index in [0, count].
        """
        index = self.tabAt(pos)
        if index < 0:
            return self.count()
        rect = self.tabRect(index)
        return index if pos.x() < rect.center().x() else index + 1

    def dragEnterEvent(self, event) -> None:
        """Accept a tab dragged from another pane and show the insertion marker.

        Returns:
            None.
        """
        if event.mimeData().hasFormat(self.MIME):
            self._drop_at = self._drop_index(self._drop_pos(event))
            self.update()
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        """Move the insertion marker as the tab hovers this bar.

        Returns:
            None.
        """
        if event.mimeData().hasFormat(self.MIME):
            self._drop_at = self._drop_index(self._drop_pos(event))
            self.update()
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dragLeaveEvent(self, event) -> None:
        """Clear the insertion marker when the drag leaves this bar.

        Returns:
            None.
        """
        self._drop_at = -1
        self.update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        """Drop the dragged tab onto this pane at the marked insertion position.

        Returns:
            None.
        """
        if event.mimeData().hasFormat(self.MIME) and PreviewTabBar._dragging and self.mover:
            source_bar, source_index = PreviewTabBar._dragging
            drop = self._drop_at if self._drop_at >= 0 else self._drop_index(self._drop_pos(event))
            self._drop_at = -1
            self.update()
            self.mover(source_bar, source_index, self, drop)
            event.acceptProposedAction()
        else:
            self._drop_at = -1
            super().dropEvent(event)

    def paintEvent(self, event) -> None:
        """Draw each tab: shape from the style, then icon and label laid out by hand.

        Returns:
            None.
        """
        painter = qt.QStylePainter(self)
        option = qt.QStyleOptionTab()
        for index in range(self.count()):
            self.initStyleOption(option, index)
            painter.drawControl(qt.QStyle.CE_TabBarTabShape, option)

            rect = self.tabRect(index)
            left = rect.left() + qt.px(self.LEFT)
            if self.is_pinned(index):               # pin glyph before the file icon (no widget, crash-safe)
                pin = _pin_icon().pixmap(qt.px(12), qt.px(12))
                painter.drawPixmap(left, rect.center().y() - pin.height() // 2, pin)
                left += pin.width() + qt.px(self.SPACING)
            icon = self.tabIcon(index)
            if not icon.isNull():
                size = self.iconSize()
                icon.paint(painter, qt.QRect(left, rect.center().y() - size.height() // 2,
                                             size.width(), size.height()))
                left += size.width() + qt.px(self.SPACING)

            # stop the text before the close cross. Do NOT query tabButton() here: during a repaint that races
            # a tab close / reorder its wrapper can dangle, and reading it hard-crashes Maya (shiboken AV). Just
            # reserve a fixed slot for the cross when the bar is closable.
            right = rect.right() - qt.px(self.RIGHT)
            if self.tabsClosable():
                right -= qt.px(18)

            font = qt.QFont(self.font())
            font.setItalic(bool(self.is_preview(index)))
            painter.setFont(font)
            lit = index == self.currentIndex() or (option.state & qt.QStyle.State_MouseOver)
            painter.setPen(qt.QColor(self.LABEL_ON if lit else self.LABEL))
            text = qt.QFontMetrics(font).elidedText(self.tabText(index), qt.Qt.ElideRight,
                                                    max(0, right - left))
            painter.drawText(qt.QRect(left, rect.top(), max(0, right - left), rect.height()),
                             qt.Qt.AlignLeft | qt.Qt.AlignVCenter, text)

        if self._drop_at >= 0:                          # insertion marker under a hovering cross-pane drag
            if self._drop_at < self.count():
                marker = self.tabRect(self._drop_at).left()
            elif self.count():
                marker = self.tabRect(self.count() - 1).right()
            else:
                marker = 0
            painter.setPen(qt.QPen(qt.QColor("#0a84ff"), qt.px(2)))
            painter.drawLine(marker, qt.px(2), marker, self.height() - qt.px(2))


class ImagePage(qt.QWidget):
    """Image preview tab: the picture centred on a checkerboard, with its size in a header."""

    def __init__(self, path:str, parent:qt.QWidget=None) -> None:
        """Build the image preview page: a header line over the picture on a scroll area.

        Returns:
            None.
        """
        super().__init__(parent)
        self.file_path = path
        self._title = os.path.basename(path)
        self.setObjectName("codeImagePage")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)

        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.pixmap = qt.QPixmap(path)
        header = qt.QLabel("  %s" % self._describe())
        header.setObjectName("codePageHeader")
        layout.addWidget(header, 0)

        self.view = qt.QLabel()
        self.view.setAlignment(qt.Qt.AlignCenter)
        self.view.setObjectName("codeImageView")
        area = qt.QScrollArea()
        area.setWidget(self.view)
        area.setWidgetResizable(True)
        area.setObjectName("codeImageArea")
        layout.addWidget(area, 1)

        self._render()

    def name(self) -> str:
        """The tab label: the image file's basename.

        Returns:
            str: the image file's basename.
        """
        return self._title

    def _describe(self) -> str:
        """Build the header line: title with pixel dimensions and file size, or a cannot-display note.

        Returns:
            str: the header text (title plus dimensions and size, or a cannot-display note).
        """
        if self.pixmap.isNull():
            return "%s  —  cannot be displayed" % self._title
        try:
            size = os.path.getsize(self.file_path)
        except OSError:
            size = 0
        unit = "%.1f KB" % (size / 1024.0) if size < 1024 * 1024 else "%.1f MB" % (size / 1048576.0)
        return "%s  —  %d x %d  •  %s" % (self._title, self.pixmap.width(),
                                          self.pixmap.height(), unit)

    def _render(self) -> None:
        """Scale down to fit the view, never up: a small icon stays at its native size.

        Returns:
            None.
        """
        if self.pixmap.isNull():
            self.view.setText("Cannot display this image.")
            self.view.setObjectName("codeImageView")
            return
        available = self.size()
        if (self.pixmap.width() > available.width() or
                self.pixmap.height() > available.height()):
            self.view.setPixmap(self.pixmap.scaled(available, qt.Qt.KeepAspectRatio,
                                                   qt.Qt.SmoothTransformation))
        else:
            self.view.setPixmap(self.pixmap)

    def resizeEvent(self, event) -> None:
        """Re-scale the image to fit whenever the page is resized.

        Returns:
            None.
        """
        super().resizeEvent(event)
        self._render()


class BadgeTabBar(qt.QTabBar):
    """Panel tab bar whose label can carry a count, written into the text itself: "PROBLEMS 12".

    Nothing is painted by hand. An earlier version drew the label itself so it could place a bubble
    after it, and that is precisely why PROBLEMS did not match OUTPUT: a hand-drawn label uses the
    WIDGET's font while the others are drawn by the style with the font from the stylesheet, and the
    two do not resolve to the same size under Maya's DPI scaling. Letting the style draw every tab is
    what makes them identical.
    """

    WIDEST = "(99+)"                            # counts are capped here, so the tab cannot grow more

    def __init__(self, parent:qt.QWidget=None) -> None:
        """Set up the badge tab bar's per-tab label store and width-reservation set.

        Returns:
            None.
        """
        super().__init__(parent)
        self._labels = {}                       # tab index -> its text without any count
        self._reserved = set()                  # tabs that keep room for the longest count

    def set_badge(self, index:int, count:int) -> None:
        """Write `count` after the tab's label; 0 leaves the label alone.

        Returns:
            None.
        """
        base = self._labels.setdefault(index, self.tabText(index))
        self._reserved.add(index)
        self.setTabText(index, "%s %s" % (base, self._text(count)) if count else base)
        self.updateGeometry()

    def _text(self, count:int) -> str:
        """Format a count as a parenthesised badge, capped at the widest value.

        Returns:
            str: the badge text, e.g. '(12)', or '(99+)' once capped.
        """
        return "(%d)" % count if count < 100 else self.WIDEST

    def tabSizeHint(self, index:int) -> object:
        """Hold a badge-carrying tab at the width of its longest possible label.

        Sizing it to the CURRENT text would widen the tab the moment a problem appears and shove
        every tab after it sideways.

        Returns:
            object: the tab size, widened to fit the longest possible badge when reserved.
        """
        size = super().tabSizeHint(index)
        if index in self._reserved:
            metrics = self.fontMetrics()
            widest = metrics.horizontalAdvance("%s %s" % (self._labels.get(index, ""), self.WIDEST))
            size.setWidth(size.width() + max(0, widest - metrics.horizontalAdvance(self.tabText(index))))
        return size


class DiffView(qt.QPlainTextEdit):
    """One side of a diff: read-only, syntax highlighted, with the editor's minimap strip.

    Reuses the engine's highlighter and CodeMiniMap, so a diff reads exactly like the editor does.
    """

    def __init__(self, path:str="", parent:qt.QWidget=None) -> None:
        """Build a read-only, syntax-highlighted diff pane with a minimap strip for `path`.

        Returns:
            None.
        """
        super().__init__(parent)
        self.setReadOnly(True)
        self.setWordWrapMode(qt.QTextOption.NoWrap)
        self.setFont(qt.QFont("Consolas", 9))
        self.setObjectName("codeDiffView")
        self.setStyleSheet("QPlainTextEdit QScrollBar { background: none; }")

        # keep the reference: a garbage-collected highlighter silently stops colouring
        self.highlighter = languages.attach(self.document(), path)

        self.minimap = editor_module.CodeMiniMap(self)
        self.verticalScrollBar().valueChanged.connect(self.minimap.update)
        self.textChanged.connect(self.minimap.update)

    def resizeEvent(self, event) -> None:
        """Keep the minimap pinned to the right edge and reserve its width in the viewport.

        Returns:
            None.
        """
        super().resizeEvent(event)
        strip = self.minimap.strip_width()
        rect = self.contentsRect()
        self.minimap.setGeometry(qt.QRect(rect.right() - strip + 1, rect.top(), strip, rect.height()))
        self.setViewportMargins(0, 0, strip, 0)


class DiffPage(qt.QWidget):
    """Side-by-side diff of one file: the old revision on the left, the new one on the right.

    Both sides are padded so matching lines stay on the same row; removed lines are tinted red on the
    left, added lines green on the right, and changed lines are marked on both sides.
    """

    REMOVED = "#4b1818"
    ADDED   = "#123d1b"
    GUTTER  = "#9d9d9d"

    def __init__(self, path:str, old_text:str, new_text:str, title:str="", parent:qt.QWidget=None) -> None:
        """Build the side-by-side diff of `path` from `old_text` and `new_text`.

        Returns:
            None.
        """
        super().__init__(parent)
        self.file_path = path
        self._title = title or os.path.basename(path)

        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = qt.QLabel("  %s" % self._title)
        header.setObjectName("codePageHeader")
        layout.addWidget(header, 0)

        self.left  = self._make_side()
        self.right = self._make_side()
        splitter = qt.QSplitter(qt.Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.left)
        splitter.addWidget(self.right)
        splitter.setSizes([qt.px(500), qt.px(500)])
        layout.addWidget(splitter, 1)

        self._fill(old_text or "", new_text or "")

        # keep both panes locked together while scrolling
        self.left.verticalScrollBar().valueChanged.connect(self.right.verticalScrollBar().setValue)
        self.right.verticalScrollBar().valueChanged.connect(self.left.verticalScrollBar().setValue)

    def name(self) -> str:
        """The tab label of the diff page.

        Returns:
            str: the diff page's title.
        """
        return self._title

    def _make_side(self) -> "DiffView":
        """Build one DiffView pane for this file.

        Returns:
            'DiffView': a diff pane for one side of the comparison.
        """
        return DiffView(self.file_path)

    def _fill(self, old_text:str, new_text:str) -> None:
        """Align both revisions with difflib and paint the changed rows.

        Returns:
            None.
        """
        import difflib
        old_lines = old_text.splitlines()
        new_lines = new_text.splitlines()

        left_rows, right_rows = [], []          # (text, tint or None)
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for offset in range(i2 - i1):
                    left_rows.append((old_lines[i1 + offset], None))
                    right_rows.append((new_lines[j1 + offset], None))
                continue
            removed = old_lines[i1:i2]
            added   = new_lines[j1:j2]
            for index in range(max(len(removed), len(added))):
                left_rows.append((removed[index], self.REMOVED) if index < len(removed) else ("", self.REMOVED))
                right_rows.append((added[index], self.ADDED) if index < len(added) else ("", self.ADDED))

        self._render(self.left, left_rows)
        self._render(self.right, right_rows)

    def _render(self, view, rows) -> None:
        """Write the rows into `view`, tinting whole lines through extra selections.

        Returns:
            None.
        """
        view.setPlainText("\n".join(text for text, _ in rows))
        selections = []
        document = view.document()
        for number, (_, tint) in enumerate(rows):
            if not tint:
                continue
            block = document.findBlockByNumber(number)
            if not block.isValid():
                continue
            selection = qt.QTextEdit.ExtraSelection()
            selection.format.setBackground(qt.QColor(tint))
            selection.format.setProperty(qt.QTextFormat.FullWidthSelection, True)
            selection.cursor = qt.QTextCursor(block)
            selection.cursor.clearSelection()
            selections.append(selection)
        view.setExtraSelections(selections)


# ------------------------------------------------------------------------------------------- sidebar

# names shown dimmed (like VS Code): caches, build/vcs dirs (dotfiles are also dimmed, see is_dim)
_DIM_NAMES = {"__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", ".ipynb_checkpoints"}
# file extensions shown dimmed (compiled / generated artifacts)
_DIM_EXTS = {".pyc", ".pyo", ".pyd", ".o", ".obj", ".class", ".map"}

_icon_cache = {}


def file_icon(path:str) -> "qt.QIcon":
    """Return the Seti (VS Code) icon for a file: exact-name match first, then extension, then default.

    Returns:
        'qt.QIcon': the cached Seti icon matching the file's name or extension.
    """
    base = os.path.basename(path).lower()
    rel = seti_map.NAME_ICON.get(base)
    if rel is None:
        ext = os.path.splitext(path)[1].lower()
        rel = seti_map.EXT_ICON.get(ext, seti_map.DEFAULT_ICON)
    icon = _icon_cache.get(rel)
    if icon is None:
        icon = _icon(rel)
        _icon_cache[rel] = icon
    return icon


def is_dim(name:str) -> bool:
    """True if `name` should be shown dimmed (dotfiles, caches, vcs/build dirs, compiled files).

    Returns:
        bool: True when the name should be shown dimmed.
    """
    low = name.lower()
    return (low in _DIM_NAMES or low.startswith(".")
            or os.path.splitext(low)[1] in _DIM_EXTS)


class CollapsibleSection(qt.QWidget):
    """A VS-Code-style sidebar section: a clickable header (chevron + TITLE) over a collapsible body."""

    def __init__(self, title:str="", body:qt.QWidget=None, expanded:bool=True, parent:qt.QWidget=None) -> None:
        """Build a collapsible sidebar section: a clickable header over `body`.

        Returns:
            None.
        """
        super().__init__(parent)
        self._expanded = expanded
        self._title = title
        self.on_toggle = None                   # optional callback(section) invoked after each toggle
        self.on_expand = None                   # called only when it OPENS, to load what it shows
        # False keeps the section at the height of what it holds instead of letting it share the
        # column's free space - Open Editors, which is only ever as tall as its list
        self.resizable = True

        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = qt.QToolButton()
        self.header.setText("  %s" % title.upper())
        self.header.setToolButtonStyle(qt.Qt.ToolButtonTextBesideIcon)
        self.header.setIconSize(qt.QSize(qt.px(16), qt.px(16)))
        # VS Code Dark Modern section header: a 1px top separator (same colour/thickness as the splitters)
        self.header.setObjectName("codeSectionHeader")
        self.header.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
        self.header.clicked.connect(self._toggle)
        layout.addWidget(self.header, 0)

        # the file name beside the title is smaller and not bold, which one QToolButton cannot do to
        # part of its own text. It is a child label laid over the button, transparent to the mouse so
        # the whole row stays clickable.
        self.suffix = qt.QLabel(self.header)
        self.suffix.setObjectName("codeSectionSuffix")
        self.suffix.setAttribute(qt.Qt.WA_TransparentForMouseEvents, True)
        self.suffix.hide()
        self.header.resizeEvent = self._header_resized

        self.body = body if body is not None else qt.QWidget()
        layout.addWidget(self.body, 1)          # body eats this section's free vertical space
        self.body.setVisible(expanded)
        self._apply_policy()
        self._refresh_chevron()

    def _set(self, name:str, state:bool) -> None:
        """Flip a stylesheet property on the header and make the style re-read it.

        Returns:
            None.
        """
        value = "true" if state else "false"
        if self.header.property(name) == value:
            return
        self.header.setProperty(name, value)
        self.header.style().unpolish(self.header)
        self.header.style().polish(self.header)

    def set_leading(self, leading:bool=True) -> None:
        """Leading sections drop their top border: the view header above them already drew that rule,

        Returns:
            None.
        and the two together made a 2px line under Explorer while Search showed a 1px one."""
        self._set("first", leading)

    def set_closing(self, closing:bool=True) -> None:
        """Close the section with a bottom rule. Set on the last one of a fully collapsed column, so

        Returns:
            None.
        the stacked headers read as a block instead of dissolving into the empty space below."""
        self._set("closing", closing)

    def _apply_policy(self) -> None:
        """Size policy only - this widget never forces a pixel height on anything.

        A collapsed section hides its body, so its own sizeHint is already exactly the header, and a
        Fixed policy holds it there. Measuring instead (setMaximumHeight(header.sizeHint())) read the
        hint during construction, BEFORE the stylesheet gave the header its padding and border, and
        the column stayed wrong until every section had been toggled once. Qt re-reads a sizeHint on
        its own whenever the style changes; a number written into setMaximumHeight it cannot.

        Returns:
            None.
        """
        grows = self._expanded and self.resizable
        self.setSizePolicy(qt.QSizePolicy.Expanding,
                           qt.QSizePolicy.Expanding if grows else qt.QSizePolicy.Fixed)

    def set_resizable(self, state:bool) -> None:
        """Whether this section shares the column's free space, or stays as tall as what it holds.

        A method rather than a bare attribute because the size policy has to be re-applied: setting
        `resizable` alone left the section on the Expanding policy it was built with, and Open Editors
        went on stretching over the whole column.

        Returns:
            None.
        """
        self.resizable = bool(state)
        self._apply_policy()

    def refresh_height(self) -> None:
        """Tell the layout the content changed height, so it re-reads the body's hint.

        Returns:
            None.
        """
        self.body.updateGeometry()
        self.updateGeometry()

    def _toggle(self) -> None:
        """Flip the section open or closed, refresh the chevron and fire the callbacks.

        Returns:
            None.
        """
        self._expanded = not self._expanded
        self.body.setVisible(self._expanded)
        self._apply_policy()
        self._refresh_chevron()
        if self._expanded and callable(self.on_expand):
            self.on_expand()                    # a collapsed section loads nothing until it opens
        if callable(self.on_toggle):
            self.on_toggle(self)

    def set_suffix(self, text:str="") -> None:
        """Show a context note beside the title - VS Code puts the file name next to TIMELINE.

        Returns:
            None.
        """
        self.suffix.setText(text or "")
        self.suffix.setVisible(bool(text))
        self._place_suffix()

    def _place_suffix(self) -> None:
        """Sit the note just after the title, and let it elide rather than push the row wider.

        Returns:
            None.
        """
        if not self.suffix.isVisible():
            return
        metrics = qt.QFontMetrics(self.header.font())
        start = (self.header.iconSize().width() + qt.px(14)
                 + metrics.horizontalAdvance(self.header.text()))
        room = max(0, self.header.width() - start - qt.px(8))
        self.suffix.setText(qt.QFontMetrics(self.suffix.font()).elidedText(
            self.suffix.text(), qt.Qt.ElideMiddle, room))
        self.suffix.setGeometry(start, 0, room, self.header.height())

    def _header_resized(self, event) -> None:
        """Re-place the suffix label after the header button is resized.

        Returns:
            None.
        """
        qt.QToolButton.resizeEvent(self.header, event)
        self._place_suffix()

    def _refresh_chevron(self) -> None:
        """Set the header chevron icon to match the expanded state.

        Returns:
            None.
        """
        self.header.setIcon(_icon("chevron_down.png" if self._expanded else "chevron_right.png"))

    def is_expanded(self) -> bool:
        """Whether the section is currently expanded.

        Returns:
            bool: True when the section body is shown.
        """
        return self._expanded


class ViewHeader(qt.QWidget):
    """The title row at the top of a side bar view: its name, and a "..." that hides its sections.

    VS Code puts one on every view. The menu is not decoration: a view with three sections in it is
    unusable on a short screen, and this is the only way to put two of them away without collapsing
    each by hand every time.
    """

    def __init__(self, title:str, parent:qt.QWidget=None) -> None:
        """Build the view header row: the title label and the '...' actions button.

        Returns:
            None.
        """
        super().__init__(parent)
        self.setObjectName("codeViewHeader")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)
        self._sections = []                     # [(label, CollapsibleSection)]

        row = qt.QHBoxLayout(self)
        row.setContentsMargins(qt.px(10), qt.px(4), qt.px(4), qt.px(4))
        row.setSpacing(0)

        self.title = qt.QLabel(title.upper())
        self.title.setObjectName("codeViewTitle")
        row.addWidget(self.title, 1)

        self.more = qt.QToolButton()
        self.more.setObjectName("codeViewMore")
        self.more.setText(u"…")
        self.more.setToolTip("Views and More Actions...")
        self.more.setAutoRaise(True)
        self.more.setFocusPolicy(qt.Qt.NoFocus)
        self.more.clicked.connect(self._menu)
        row.addWidget(self.more, 0)

    def set_sections(self, sections:list) -> None:
        """`sections` is [(label, CollapsibleSection)]; an empty list hides the "..." entirely.

        Returns:
            None.
        """
        self._sections = sections
        self.more.setVisible(bool(sections))

    def _menu(self) -> None:
        """A checkable entry per section, ticked when the section is showing.

        Returns:
            None.
        """
        menu = qt.QMenu(self.more)
        for label, section in self._sections:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(section.isVisible())
            action.toggled.connect(lambda state, s=section: self._show(s, state))
        point = self.more.mapToGlobal(qt.QPoint(0, self.more.height()))
        menu.exec_(point) if hasattr(menu, "exec_") else menu.exec(point)

    @staticmethod
    def _show(section, state:bool) -> None:
        """Hide or show a section.

        Hiding every one of them is allowed: the header itself never goes away, so the menu that put
        them away is still there to bring them back.

        Returns:
            None.
        """
        section.setVisible(state)
        stack = section.parent()
        if isinstance(stack, SectionStack):
            stack.apply_sizes()         # the space it held, and the closing rule, both move on


class SectionStack(qt.QSplitter):
    """Vertical stack of CollapsibleSections whose boundaries the user can drag, like VS Code.

    No header is ever measured here, which is what used to break the column. A collapsed section
    hides its body and takes a Fixed size policy, so the splitter reads the header height off the
    section's own sizeHint - at layout time, once the stylesheet has given it its padding and border.
    Asking a collapsed section for 0 is enough: setSizes clamps every request to what each child's
    policy allows, and a Fixed child comes back at exactly its header.

    Before, those heights were computed here with header.sizeHint() during construction - too early,
    the padding was not applied yet - and written into setMaximumHeight. A number written once cannot
    follow the style, so the column stayed wrong until every section had been toggled by hand.
    """

    def __init__(self, sections, parent:qt.QWidget=None, leads:bool=True) -> None:
        """`leads` says this stack is the first thing under the view header, so its own first section
        drops the top border that header already draws. The Explorer passes False: there, Open Editors

        Returns:
            None.
        leads the column and the stack starts mid-way down."""
        super().__init__(qt.Qt.Vertical, parent)
        self.setObjectName("codeSectionStack")
        self.setChildrenCollapsible(False)
        # 1px, and painted transparent: the visible line is the header's own top border, so collapsed
        # headers still sit flush against each other instead of showing a gap between them
        self.setHandleWidth(1)
        self._sections = list(sections)
        for index, section in enumerate(self._sections):
            section.on_toggle = lambda *_: self.apply_sizes()
            section.set_leading(leads and index == 0)
            self.addWidget(section)
            self.setStretchFactor(index, 1 if section.is_expanded() else 0)
        self._open_before = [i for i, s in enumerate(self._sections) if s.is_expanded()]
        self._mark_closing()
        # deliberately no setSizes here: the splitter's own first layout already gives the collapsed
        # sections their header and the open ones the rest, and it does it after the style is applied

    def apply_sizes(self) -> None:
        """Redistribute after a section was opened, closed or hidden.

        Every number used comes from the splitter's CURRENT layout, never from a hint measured ahead
        of time: `room` is the height it really has, and a collapsed section is asked for 0 and left
        to come back at its own header.

        Returns:
            None.
        """
        sizes = self.sizes()
        # the column's real height, not the sum of the sizes: fully collapsed, the children only
        # occupy their headers and that sum is a fraction of the space actually there to hand out
        room = max(sum(sizes), self.height())
        shown = [i for i, s in enumerate(self._sections) if not s.isHidden()]
        opened = [i for i in shown if self._sections[i].is_expanded()]
        for index in range(len(self._sections)):
            self.setStretchFactor(index, 1 if index in opened else 0)
        self._mark_closing()

        wanted = [0] * len(sizes)
        if opened:
            fresh = [i for i in opened if i not in self._open_before]
            kept = [i for i in opened if i in self._open_before]
            held = sum(sizes[i] for i in kept)
            if fresh and held:
                # a section that just opened takes an even share, and the ones already open give it
                # up in proportion to what they hold - a drag the user made is scaled, not discarded
                share = room // len(opened)
                spare = room - share * len(fresh)
                for i in fresh:
                    wanted[i] = share
                for i in kept:
                    wanted[i] = max(1, spare * sizes[i] // held)
            elif held:
                # nothing new opened, so whatever closed freed its space: hand it to those still open
                for i in opened:
                    wanted[i] = max(1, room * sizes[i] // held)
            else:
                for i in opened:
                    wanted[i] = room // len(opened)
        self.setSizes(wanted)
        self._open_before = opened

    def _mark_closing(self) -> None:
        """Only the last visible header closes the block, and only while the whole column is shut.

        Returns:
            None.
        """
        shown = [s for s in self._sections if not s.isHidden()]
        shut = not any(s.is_expanded() for s in shown)
        last = shown[-1] if shown else None
        for section in self._sections:
            section.set_closing(shut and section is last)


# item data roles shared by the panel and its status delegate
_PATH_ROLE   = qt.Qt.UserRole + 1          # absolute path stored on each item
_LOADED_ROLE = qt.Qt.UserRole + 2          # True once a dir item's children were read
_ISDIR_ROLE  = qt.Qt.UserRole + 3          # True if the item is a directory
_STATUS_ROLE = qt.Qt.UserRole + 4          # git status letter (M/A/U/D/R/C) or None
_DIM_ROLE    = qt.Qt.UserRole + 5          # True if the item should be shown dimmed
_LINE_ROLE   = qt.Qt.UserRole + 6          # 1-based line number (search results)
_ROOT_ROLE   = qt.Qt.UserRole + 7          # workspace root a source-control entry belongs to
_STAGED_ROLE = qt.Qt.UserRole + 8          # True when the entry is a staged change
_COUNT_ROLE  = qt.Qt.UserRole + 9          # group header: number shown in the blue bubble
_SUBTITLE_ROLE = qt.Qt.UserRole + 10       # dimmed folder path drawn after a file name
_AUTHOR_ROLE = qt.Qt.UserRole + 11         # commit author (graph)
_REFS_ROLE   = qt.Qt.UserRole + 12         # branch/tag names on a commit (graph)
_HEAD_ROLE   = qt.Qt.UserRole + 13         # True for the tip commit (graph)
_TRAIL_ROLE  = qt.Qt.UserRole + 14         # dimmed text pinned to the right edge of a row
_CLOSE_ROLE  = qt.Qt.UserRole + 15         # row reserves a close slot on its left (Open Editors)
_DETAIL_ROLE = qt.Qt.UserRole + 16         # the one-line form of a problem, for Copy


# Symbol kinds in the Outline. VS Code draws these from its codicon font, which is not in this
# package, so they are painted once at first use: a rounded plate in the kind's colour with its
# initial knocked out of it. Same reading at 16px, no asset to ship.
# The painter itself lives in core/qt.py, where the completion popup can reach it too - the Outline
# and the completion list must show the same icon for the same kind of symbol.
symbol_icon = qt.symbol_icon


class GitStatusDelegate(qt.QStyledItemDelegate):
    """Fully custom-painted tree row (VS Code style): row background, icon, colour-tinted name, and a
    right-aligned git marker (a letter for files, a dot for folders). Painting the text ourselves is the
    only reliable way to tint it across Qt styles (the native style ignores per-item palette overrides).
    """

    GUIDE_COLOR = "#333333"    # indent guides: hairline and dark, so they read as structure not decoration
    SELECT      = "#5db4e5"    # selection outline
    TEXT        = "#cccccc"
    TEXT_ON     = "#e8e8e8"    # selected row
    TEXT_DIM    = "#7a7a7a"    # dotfiles, caches, compiled artefacts

    def __init__(self, tree, parent=None) -> None:
        """Store the tree reference and default the leaf-alignment flag on.

        Returns:
            None.
        """
        super().__init__(parent)
        self._tree = tree                            # needed to compute indentation depth for guide lines
        # True pulls a childless row left into the chevron column its siblings use, which is what lines
        # a file up under a sibling FOLDER's chevron in the workspace tree. An explicit tree (the krig
        # process) turns it off: there, a row's children must simply sit one step in from their parent.
        self.align_leaves = True

    def paint(self, painter, option, index) -> None:
        """Custom-paint one tree row: row band, indent guides, icon, tinted name and git marker.

        Returns:
            None.
        """
        painter.save()
        rect = qt.QRect(option.rect)
        selected = bool(option.state & qt.QStyle.State_Selected)
        hovered  = bool(option.state & qt.QStyle.State_MouseOver)

        # ---- full-width row band ----
        # extend the row to the viewport's right edge so hover/selection fill the WHOLE line
        band = qt.QRect(rect)
        band.setLeft(0)
        band.setRight(self._tree.viewport().width() - 1)

        # ---- background + selection (Maya style: light translucent fill + light-blue outline) ----
        # independent of window focus so the selection never greys out when the window is inactive
        if selected:
            painter.fillRect(band, qt.QColor(255, 255, 255, 28))     # subtle light fill
            pen = qt.QPen(qt.QColor(self.SELECT))                     # accent outline
            pen.setWidth(1)
            painter.setPen(pen)
            painter.setBrush(qt.Qt.NoBrush)
            painter.drawRect(qt.QRect(band.left(), band.top(), band.width() - 1, band.height() - 1))
        elif hovered:
            painter.fillRect(band, qt.QColor(255, 255, 255, 14))     # faint hover

        # ---- indent guide lines (thin vertical rules per depth level, like VS Code) ----
        depth = 0
        parent = index.parent()
        while parent.isValid():
            depth += 1
            parent = parent.parent()

        # ---- icon (computed BEFORE guides so guides can be clipped to stay left of it) ----
        # Qt draws the chevron in the column left of rect.left(); a row without one would start a whole
        # indent step further right and read as nested one level too deep, so it is pulled back into
        # that empty column. What decides is whether a chevron is actually THERE, not whether the row
        # is a folder: a process action can carry a script AND sub-actions, and testing is_dir alone
        # dragged its icon underneath its own chevron.
        icon    = index.data(qt.Qt.DecorationRole)
        is_dir  = bool(index.data(_ISDIR_ROLE))
        model   = index.model()
        # folders always reserve the column (a collapsed one has a lazy-load placeholder, an expanded
        # empty one has nothing - and must not shift when it opens)
        chevron = is_dir or (model is not None and model.hasChildren(index))
        indent  = self._tree.indentation()
        content = rect.left() if (chevron or not self.align_leaves) else rect.left() - indent
        text_left = content + qt.px(4)

        # One vertical guide per ANCESTOR level, in the indentation gutter left of this row. Guides sit
        # left of each indent column so they never cross the expand/collapse chevron Qt paints there.
        if depth > 0 and not selected:
            pen = qt.QPen(qt.QColor(self.GUIDE_COLOR))
            pen.setWidth(1)
            pen.setCosmetic(True)                        # stay hairline whatever the DPI scaling
            painter.setPen(pen)
            for back in range(1, depth + 1):
                x = rect.left() - back * indent - qt.px(7)
                if x < text_left - qt.px(2):             # keep guides clear of the icon column
                    painter.drawLine(x, rect.top(), x, rect.bottom())
        if icon is not None and not icon.isNull():
            size = qt.px(16)
            iy = rect.center().y() - size // 2
            icon.paint(painter, qt.QRect(text_left, iy, size, size), qt.Qt.AlignCenter)
            text_left += size + qt.px(4)

        # ---- name colour ----
        # a file carries its own status; a folder carries the strongest status found beneath it. Both are
        # tinted, so a change shows up without having to expand the branch.
        letter = index.data(_STATUS_ROLE)
        if letter:
            name_color = _STATUS_COLOR.get(letter, "#cccccc")
        elif index.data(_DIM_ROLE):
            name_color = self.TEXT_DIM               # dimmed (dotfiles / caches / compiled)
        else:
            name_color = self.TEXT_ON if selected else self.TEXT

        # ---- name text (extends to the git marker at the right edge of the full-width band) ----
        marker_w = qt.px(16)
        text_width = max(0, band.right() - text_left - marker_w)
        text_rect = qt.QRect(text_left, rect.top(), text_width, rect.height())
        painter.setFont(option.font)
        painter.setPen(qt.QColor(name_color))
        name = index.data(qt.Qt.DisplayRole) or ""
        elided = option.fontMetrics.elidedText(name, qt.Qt.ElideRight, text_rect.width())
        painter.drawText(text_rect, qt.Qt.AlignLeft | qt.Qt.AlignVCenter, elided)

        # ---- right-aligned git marker (keeps the status colour even when selected) ----
        if letter:
            marker_color = _STATUS_COLOR.get(letter, "#cccccc")
            mrect = qt.QRect(rect)
            mrect.setRight(band.right() - qt.px(8))
            # folders show a dot, EXCEPT submodules which show the letter 'S' (like VS Code)
            if index.data(_ISDIR_ROLE) and letter != "S":
                painter.setBrush(qt.QColor(marker_color))
                painter.setPen(qt.Qt.NoPen)
                rad = qt.px(3)
                painter.drawEllipse(qt.QPoint(mrect.right() - rad, mrect.center().y()), rad, rad)
            else:
                f = qt.QFont(option.font); f.setBold(True); painter.setFont(f)
                painter.setPen(qt.QColor(marker_color))
                painter.drawText(mrect, qt.Qt.AlignRight | qt.Qt.AlignVCenter, letter)
        painter.restore()

    def sizeHint(self, option, index) -> object:
        """Force a minimum row height of 22px.

        Returns:
            object: the item size hint, raised to at least 22px tall.
        """
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), qt.px(22)))
        return size


class WorkspacePanel(qt.QWidget):
    """The Workspace section body: several root folders shown as top-level nodes of ONE tree (VS Code style).

    Uses a single QStandardItemModel populated lazily (children are read on first expand), so many roots
    live in the same continuous tree instead of separate split views. Git status (M/A/U/D/R) is shown per
    file via a right-aligned letter, refreshed on add / save / manual refresh.
    """

    fileActivated = qt.signal(str)             # single click: open as a preview tab
    filePinned    = qt.signal(str)             # double click: keep the tab open for good
    runRequested    = qt.signal(str)           # context menu: execute this script in maya
    revealRequested = qt.signal(str)           # context menu: show it in the OS file browser
    searchRequested = qt.signal(str)           # context menu: search inside this folder
    pathRenamed     = qt.signal(str, str)      # (old, new) so open tabs can follow
    pathDeleted     = qt.signal(str)           # so a tab on a deleted file can be closed

    _PATH_ROLE   = _PATH_ROLE
    _LOADED_ROLE = _LOADED_ROLE
    _ISDIR_ROLE  = _ISDIR_ROLE
    _STATUS_ROLE = _STATUS_ROLE
    _DIM_ROLE    = _DIM_ROLE

    def __init__(self, parent:qt.QWidget=None) -> None:
        """Build the workspace panel: a single lazily-loaded tree over the root folders.

        Returns:
            None.
        """
        super().__init__(parent)
        self.setObjectName("codeWorkspace")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)
        self.roots = []                         # list of root folder paths (order preserved)
        # an explicit tree replaces the filesystem one when the host has a better structure to show:
        # the host feeds it the krig's process, so you browse actions in run order rather than
        # folders. None = the normal workspace, read from disk.
        self.virtual = None
        self._clipboard = None                  # (path, cut) remembered by Cut / Copy

        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.model = qt.QStandardItemModel(self)
        self.tree = qt.QTreeView()
        self.tree.setObjectName("codeWorkspaceTree")   # scopes the transparent-item rules to this tree
        self.tree.setModel(self.model)
        self.tree.setHeaderHidden(True)
        self.tree.setAnimated(True)
        self.tree.setIndentation(qt.px(14))
        self.tree.setIconSize(qt.QSize(qt.px(18), qt.px(18)))
        self.tree.setExpandsOnDoubleClick(False)   # double-click opens files; chevron toggles folders
        self.tree.setContextMenuPolicy(qt.Qt.CustomContextMenu)
        self.tree.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Expanding)
        # VS Code sidebar font: system UI sans-serif, ~14px
        tree_font = qt.QFont("Segoe UI")
        tree_font.setPixelSize(qt.px(14))
        self.tree.setFont(tree_font)
        self.delegate = GitStatusDelegate(self.tree)
        self.tree.setItemDelegate(self.delegate)
        self.tree.expanded.connect(self._on_expanded)
        self.tree.clicked.connect(self._on_clicked)           # single click: toggle folder / preview file
        self.tree.doubleClicked.connect(self._on_double_clicked)   # double click: pin the file
        self.tree.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.tree, 1)

        self._git = {}                          # {root_path: {abs_path: letter}} status cache per root
        self._submodules = set()                # absolute paths of submodule folders (shown as 'S')

        # a hint shown only while the workspace is empty
        self.empty_hint = qt.QLabel("  No folder in workspace.\n  File ▸ Add Folder to Workspace")
        self.empty_hint.setObjectName("codeHint")
        layout.addWidget(self.empty_hint, 0)

        self._restore()
        self._update_empty_hint()

    # ---- item helpers

    def _make_item(self, path:str, is_dir:bool, label:str=None) -> "qt.QStandardItem":
        """Build a tree item for `path`. Folders get no icon (just the chevron); files get a Seti icon.

        Returns:
            'qt.QStandardItem': the tree item built for `path`.
        """
        name = os.path.basename(path.rstrip("/\\")) or path
        item = qt.QStandardItem(label if label is not None else name)
        item.setEditable(False)
        item.setData(path, self._PATH_ROLE)
        item.setData(False, self._LOADED_ROLE)
        item.setData(is_dir, self._ISDIR_ROLE)
        letter = self._status_for(path, is_dir)
        item.setData(letter, self._STATUS_ROLE)
        item.setData(is_dim(name), self._DIM_ROLE)   # dim dotfiles/caches (the delegate colours the text)
        # tooltip: full path on disk, plus the git status when there is one (like VS Code)
        tip = os.path.normpath(path)
        if letter in _STATUS_TOOLTIP:
            tip += "  •  " + _STATUS_TOOLTIP[letter]
        item.setToolTip(tip)
        if is_dir:
            # no folder icon (matches the user's VS Code look); a placeholder child shows the chevron
            placeholder = qt.QStandardItem("")
            placeholder.setEditable(False)
            item.appendRow(placeholder)
        else:
            item.setIcon(file_icon(path))
        return item

    @staticmethod
    def _display_key(relative:str) -> tuple:
        """Sort key placing a path where the tree would draw it: folders before files, then alphabetical.

        Every segment but the last is a directory, so it gets group 0; the file itself gets group 1. That
        is what makes `core/compat.py` come before `__init__.py`, exactly like the tree shows them.

        Returns:
            tuple: a per-segment sort key placing folders (group 0) before files (group 1).
        """
        parts = relative.replace("\\", "/").split("/")
        return tuple([(0, part.lower()) for part in parts[:-1]] + [(1, parts[-1].lower())])

    def _status_for(self, path:str, is_dir:bool) -> str:
        """Status letter for `path`.

        A file reports its own status. A folder reports the status of the FIRST changed entry beneath it
        in tree order - not the most severe one - so a parent takes the colour of what you see first when
        you expand it, which is how VS Code decorates folders. Deleted files are skipped: they are no
        longer in the tree, and letting them win would paint whole branches red.

        Returns:
            str: the status letter (M/A/U/D/R/S), or None when unchanged.
        """
        norm = os.path.normpath(path)
        if is_dir and norm in self._submodules:
            return "S"
        if not is_dir:
            for statuses in self._git.values():
                if norm in statuses:
                    return statuses[norm]
            return None

        best_key, best_letter = None, None
        prefix = norm + os.sep
        for statuses in self._git.values():
            for changed, letter in statuses.items():
                if letter == "D" or not changed.startswith(prefix):
                    continue
                key = self._display_key(changed[len(prefix):])
                if best_key is None or key < best_key:
                    best_key, best_letter = key, letter
        return best_letter

    def _populate(self, parent_item:"qt.QStandardItem", folder:str) -> None:
        """Read `folder` and fill `parent_item` with its children (dirs first, then files, alphabetical).

        Returns:
            None.
        """
        parent_item.removeRows(0, parent_item.rowCount())   # drop the placeholder / stale rows
        try:
            entries = os.listdir(folder)
        except Exception:
            return
        dirs  = sorted([e for e in entries if os.path.isdir(os.path.join(folder, e))], key=str.lower)
        files = sorted([e for e in entries if not os.path.isdir(os.path.join(folder, e))], key=str.lower)
        for name in dirs:
            parent_item.appendRow(self._make_item(os.path.join(folder, name), True))
        for name in files:
            parent_item.appendRow(self._make_item(os.path.join(folder, name), False))
        parent_item.setData(True, self._LOADED_ROLE)

    # ---- git status

    def refresh_git(self) -> None:
        """Re-read git status + submodules for every root, then re-apply markers on all visible items.

        Returns:
            None.
        """
        self._git = {}
        self._submodules = set()
        for root in self.roots:
            try:
                if vcs.is_git_repo(root):
                    self._git[root] = vcs.status(root)
                    self._submodules |= vcs.submodules(root)
            except Exception:
                pass
        self._reapply_status(self.model.invisibleRootItem())
        self.tree.viewport().update()

    def _reapply_status(self, parent_item) -> None:
        """Walk the already-built items and refresh their status role. Only descends into LOADED folders,

        Returns:
            None.
        so a not-yet-expanded folder keeps its lazy-load placeholder intact (fixes non-expandable roots)."""
        for row in range(parent_item.rowCount()):
            item = parent_item.child(row)       # invisibleRootItem and QStandardItem both expose child()
            if item is None:
                continue
            path = item.data(self._PATH_ROLE)
            if not path:
                continue                        # skip lazy-load placeholders (no path)
            item.setData(self._status_for(path, bool(item.data(self._ISDIR_ROLE))), self._STATUS_ROLE)
            if item.data(self._LOADED_ROLE) and item.rowCount():
                self._reapply_status(item)

    def _on_expanded(self, index) -> None:
        """Lazy-load a folder's children the first time it is expanded.

        Returns:
            None.
        """
        item = self.model.itemFromIndex(index)
        if item is None or item.data(self._LOADED_ROLE):
            return
        path = item.data(self._PATH_ROLE)
        if path and os.path.isdir(path):
            self._populate(item, path)

    # ---- persistence

    def roots_setting(self) -> list:
        """The root folders saved from the last session.

        Returns:
            list: the root folder paths saved in the workspace optionVar.
        """
        try:
            import maya.cmds as cmds
            if cmds.optionVar(exists=WORKSPACE_OPTIONVAR):
                return json.loads(cmds.optionVar(query=WORKSPACE_OPTIONVAR) or "[]")
        except Exception:
            pass
        return []

    def _restore(self) -> None:
        """Re-add the root folders from the last session.

        Returns:
            None.
        """
        for path in self.roots_setting():
            self._add_root(path)
        self.refresh_git()

    def _save(self) -> None:
        """Persist the current root folders to the Maya optionVar.

        Returns:
            None.
        """
        try:
            import maya.cmds as cmds
            cmds.optionVar(stringValue=(WORKSPACE_OPTIONVAR, json.dumps(self.roots)))
        except Exception:
            pass

    def _update_empty_hint(self) -> None:
        # the "Add Folder to Workspace" hint only makes sense when the user owns the tree
        """Show the empty-workspace hint only when the personal tree has no roots.

        Returns:
            None.
        """
        self.empty_hint.setVisible(self.virtual is None and not self.roots)

    # ---- folders

    def add_folder(self) -> None:
        """Ask for a folder and add it as a new workspace root.

        Returns:
            None.
        """
        start = self.roots[-1] if self.roots else ""
        folder = qt.QFileDialog.getExistingDirectory(self, "Add Folder to Workspace", start, qt.QFileDialog.ShowDirsOnly)
        if folder and folder not in self.roots:
            self._add_root(folder)
            self._save()
            self._update_empty_hint()
            self.refresh_git()

    def _add_root(self, path:str, label:str=None) -> None:
        """Add `path` as a bold top-level node of the shared tree.

        Returns:
            None.
        """
        if not path or not os.path.isdir(path) or path in self.roots:
            return
        label = label or (os.path.basename(path.rstrip("/\\")) or path).upper()
        item = self._make_item(path, True, label=label)
        font = item.font(); font.setBold(True); item.setFont(font)
        self.model.appendRow(item)
        self.roots.append(path)

    def remove_folder(self, path:str) -> None:
        """Remove the workspace root at `path`.

        Returns:
            None.
        """
        for row in range(self.model.rowCount()):
            it = self.model.item(row)
            if it is not None and it.data(self._PATH_ROLE) == path:
                self.model.removeRow(row)
                break
        if path in self.roots:
            self.roots.remove(path)
        self._save()
        self._update_empty_hint()

    def set_tree(self, nodes:list=None) -> None:
        """Show an explicit tree instead of the folders on disk. None restores the normal workspace.

        Each node is {"label", "path", "children"}. A node with no `path` is a pure group (the host
        uses PRE and POST); a node WITH a path is a file that opens on click and can still have
        children, which is how a process action carries its sub-actions.

        An empty list is meaningful - "there is a host, it just has nothing to show" - and must render
        an empty tree rather than falling back to the personal roots of the standalone window.

        Returns:
            None.
        """
        self.virtual = nodes
        # the chevron-column pull-back is a filesystem idiom (a file lining up under a sibling folder's
        # chevron). In an explicit tree it would drop a child onto its own parent's column.
        self.delegate.align_leaves = nodes is None
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the tree: the host's explicit nodes when there are any, the saved roots otherwise.

        The open folders are remembered across the rebuild. Without that, anything that refreshes -
        creating a file, renaming, a git status poll - collapses the whole tree and drops you back at
        the roots, which is maddening when you were three levels deep.

        Returns:
            None.
        """
        opened = self._opened_folders()
        self.model.clear()
        self.roots = []
        if self.virtual is not None:
            for node in self.virtual:
                self.model.appendRow(self._make_virtual(node, top=True))
        else:
            for path in self.roots_setting():
                self._add_root(path)
        self._update_empty_hint()
        self.refresh_git()
        if self.virtual is not None:
            self.tree.expandAll()                        # a process tree is small: show it all at once
        else:
            self._reopen_folders(opened)

    def _opened_folders(self) -> set:
        """The paths of every folder currently expanded.

        Returns:
            set: the normcased paths of every currently expanded folder.
        """
        opened = set()

        def walk(item) -> None:
            """Recurse into `item`, collecting the paths of expanded folders.

            Returns:
                None.
            """
            for row in range(item.rowCount()):
                child = item.child(row)
                path = child.data(self._PATH_ROLE) if child is not None else None
                if not path:
                    continue
                if self.tree.isExpanded(self.model.indexFromItem(child)):
                    opened.add(os.path.normcase(path))
                    walk(child)

        walk(self.model.invisibleRootItem())
        return opened

    def _reopen_folders(self, opened:set) -> None:
        """Expand again what was expanded, loading each level on the way down.

        Children are read lazily, so a folder has to be populated before its own children can be
        expanded - hence the walk rather than a flat pass over the set.

        Returns:
            None.
        """
        if not opened:
            return

        def walk(item) -> None:
            """Recurse into `item`, re-expanding folders that were open before.

            Returns:
                None.
            """
            for row in range(item.rowCount()):
                child = item.child(row)
                path = child.data(self._PATH_ROLE) if child is not None else None
                if not path or os.path.normcase(path) not in opened:
                    continue
                if not child.data(self._LOADED_ROLE) and os.path.isdir(path):
                    self._populate(child, path)
                self.tree.setExpanded(self.model.indexFromItem(child), True)
                walk(child)

        walk(self.model.invisibleRootItem())

    def _make_virtual(self, node:dict, top:bool=False) -> "qt.QStandardItem":
        """Build one item of an explicit tree, and its children under it.

        Returns:
            'qt.QStandardItem': the item built for `node`, with its children attached.
        """
        path = node.get("path")
        item = qt.QStandardItem(node.get("label") or (os.path.basename(path) if path else ""))
        item.setEditable(False)
        item.setData(path, self._PATH_ROLE)
        item.setData(True, self._LOADED_ROLE)            # never read this one from disk
        item.setData(not path, self._ISDIR_ROLE)         # a group folds on click, a file opens
        item.setData(self._status_for(path, False) if path else "", self._STATUS_ROLE)
        item.setData(bool(node.get("dim")), self._DIM_ROLE)   # e.g. an action whose script is gone
        if path:
            item.setIcon(file_icon(path))
            item.setToolTip(os.path.normpath(path))
        elif node.get("tooltip"):
            item.setToolTip(node["tooltip"])
        if top:
            font = item.font(); font.setBold(True); item.setFont(font)
        for child in node.get("children") or []:
            item.appendRow(self._make_virtual(child))
        return item

    # ---- interaction

    def _clicked_path(self, pos) -> str:
        """The path of the tree item under `pos`, if any.

        Returns:
            str: the item's path under `pos`, or None.
        """
        index = self.tree.indexAt(pos)
        item  = self.model.itemFromIndex(index) if index.isValid() else None
        return item.data(self._PATH_ROLE) if item is not None else None

    def _on_clicked(self, index) -> None:
        """Single click: toggle a folder, or open a file as a PREVIEW tab (italic, reused).

        Returns:
            None.
        """
        item = self.model.itemFromIndex(index)
        if item is None:
            return
        path = item.data(self._PATH_ROLE)
        if item.data(self._ISDIR_ROLE):
            self.tree.collapse(index) if self.tree.isExpanded(index) else self.tree.expand(index)
        elif path and os.path.isfile(path):
            self.fileActivated.emit(path)

    def _on_double_clicked(self, index) -> None:
        """Double click: keep the file open for good (the click that preceded it opened the preview).

        Returns:
            None.
        """
        item = self.model.itemFromIndex(index)
        if item is None:
            return
        path = item.data(self._PATH_ROLE)
        if path and not item.data(self._ISDIR_ROLE) and os.path.isfile(path):
            self.filePinned.emit(path)

    def _context_menu(self, pos) -> None:
        """Right-click menu. A folder and a file get DIFFERENT menus, as they do in VS Code.

        Most entries only make sense for one of the two - New File belongs to a folder, Rename and
        Run belong to a file - and offering the union with half of it greyed out reads worse than
        offering the set that applies.

        Returns:
            None.
        """
        path = self._clicked_path(pos)
        if path is None and self.virtual is None and len(self.roots) == 1:
            # empty space belongs to the root folder, so it gets the root's menu - that is where a
            # New File would land anyway. With SEVERAL roots there is no answer to "which one", so
            # the reduced workspace menu stays.
            path = self.roots[0]
        is_dir = bool(path) and os.path.isdir(path)
        is_file = bool(path) and os.path.isfile(path)
        menu = qt.QMenu(self.tree)

        if is_file:
            menu.addAction("Open", lambda: self.fileActivated.emit(path))
            if path.lower().endswith((".py", ".pyw", ".mel")):
                menu.addAction("Run in Maya", lambda: self.runRequested.emit(path))
        elif is_dir:
            menu.addAction("New File...", lambda: self._new_entry(path, False))
            menu.addAction("New Folder...", lambda: self._new_entry(path, True))

        if path:
            menu.addAction("Reveal in File Explorer", lambda: self.revealRequested.emit(path))
        if is_dir:
            menu.addAction("Find in Folder...", lambda: self.searchRequested.emit(path))

        if self.virtual is None:         # an explicit tree is driven by the host, not by the user
            menu.addSeparator()
            menu.addAction("Add Folder to Workspace...", self.add_folder)
            if path in self.roots:
                menu.addAction("Remove Folder from Workspace", lambda: self.remove_folder(path))

        if path:
            menu.addSeparator()
            menu.addAction("Cut", lambda: self._clip(path, True))
            menu.addAction("Copy", lambda: self._clip(path, False))
            paste = menu.addAction("Paste", lambda: self._paste(path if is_dir
                                                                else os.path.dirname(path)))
            paste.setEnabled(bool(self._clipboard))
            menu.addSeparator()
            menu.addAction("Copy Path", lambda: compat.copy(os.path.normpath(path)))
            menu.addAction("Copy Relative Path", lambda: compat.copy(self._relative(path)))
            # a workspace ROOT gets neither: what you want there is Remove Folder from Workspace,
            # and a right-click on empty space offering to delete the whole repo is a footgun
            if path not in self.roots:
                menu.addSeparator()
                menu.addAction("Rename...", lambda: self._rename(path))
                menu.addAction("Delete", lambda: self._delete(path))

        menu.addSeparator()
        menu.addAction("Refresh", self.refresh)
        point = self.tree.viewport().mapToGlobal(pos)
        menu.exec_(point) if hasattr(menu, "exec_") else menu.exec(point)

    # ---- file operations

    def _relative(self, path:str) -> str:
        """`path` relative to the workspace root that holds it, or its bare name.

        Returns:
            str: the path relative to its workspace root, or its bare name.
        """
        for root in self.roots:
            try:
                relative = os.path.relpath(path, root)
            except ValueError:
                continue                                 # another drive: no relative form exists
            if not relative.startswith(".."):
                return relative.replace("\\", "/")
        return os.path.basename(path)

    def _new_entry(self, folder:str, make_folder:bool) -> None:
        """Create a file or a folder inside `folder`; a new file is opened straight away.

        Returns:
            None.
        """
        label = "Folder" if make_folder else "File"
        name = compat.message.prompt(title="New %s" % label, label="Name:", text="", parent=self)
        if not name:
            return
        target = os.path.join(folder, name)
        if os.path.exists(target):
            compat.message.critical(title="New %s" % label, buttons=["Close"],
                                    message_text='"%s" already exists.' % name, parent=self)
            return
        try:
            if make_folder:
                os.makedirs(target)
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)   # "sub/new.py" works too
                open(target, "a", encoding="utf-8").close()
        except OSError as error:
            compat.message.critical(title="New %s" % label, buttons=["Close"],
                                    message_text="Could not create it.", informative_text=str(error), parent=self)
            return
        self._reveal_after(target, folder)
        if not make_folder:
            self.filePinned.emit(target)

    def _reveal_after(self, target:str, parent_folder:str) -> None:
        """Refresh, keeping the folder we just created something in open so the result is visible.

        Returns:
            None.
        """
        opened = self._opened_folders()
        opened.add(os.path.normcase(parent_folder))
        self.model.clear()
        self.roots = []
        for path in self.roots_setting():
            self._add_root(path)
        self._update_empty_hint()
        self.refresh_git()
        self._reopen_folders(opened)
        self._select_path(target)

    def _select_path(self, path:str) -> None:
        """Put the selection on `path` if it is currently in the tree.

        Returns:
            None.
        """
        wanted = os.path.normcase(path)

        def walk(item) -> object:
            """Recurse into `item`, selecting the child whose path matches.

            Returns:
                object: True once the matching item was found and selected.
            """
            for row in range(item.rowCount()):
                child = item.child(row)
                current = child.data(self._PATH_ROLE) if child is not None else None
                if current and os.path.normcase(current) == wanted:
                    index = self.model.indexFromItem(child)
                    self.tree.setCurrentIndex(index)
                    self.tree.scrollTo(index)
                    return True
                if child is not None and walk(child):
                    return True
            return False

        walk(self.model.invisibleRootItem())

    def _clip(self, path:str, cut:bool) -> None:
        """Remember a path for the next Paste, and put it on the system clipboard too.

        Returns:
            None.
        """
        self._clipboard = (path, cut)
        compat.copy(os.path.normpath(path))

    def _paste(self, folder:str) -> None:
        """Copy or move whatever Cut/Copy remembered into `folder`.

        Returns:
            None.
        """
        if not self._clipboard or not os.path.isdir(folder):
            return
        source, cut = self._clipboard
        if not os.path.exists(source):
            self._clipboard = None                       # it was moved or deleted meanwhile
            return
        target = os.path.join(folder, os.path.basename(source))
        if os.path.normpath(target) == os.path.normpath(source):
            target = self._unique(target)                # pasting beside itself needs a new name
        try:
            import shutil
            if cut:
                shutil.move(source, target)
                self._clipboard = None                   # a cut can only be pasted once
            elif os.path.isdir(source):
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        except Exception as error:
            compat.message.critical(title="Paste", buttons=["Close"],
                                    message_text="Could not paste.", informative_text=str(error), parent=self)
        self.refresh()

    @staticmethod
    def _unique(path:str) -> str:
        """`path` with " copy" appended until nothing sits at that name.

        Returns:
            str: a variant of `path` with ' copy' appended until the name is free.
        """
        stem, extension = os.path.splitext(path)
        candidate, index = "%s copy%s" % (stem, extension), 2
        while os.path.exists(candidate):
            candidate = "%s copy %d%s" % (stem, index, extension)
            index += 1
        return candidate

    def _rename(self, path:str) -> None:
        """Rename a file or folder on disk, and tell the host so open tabs can follow.

        Returns:
            None.
        """
        old = os.path.basename(path)
        name = compat.message.prompt(title="Rename", label="New name:", text=old, parent=self)
        if not name or name == old:
            return
        target = os.path.join(os.path.dirname(path), name)
        if os.path.exists(target):
            compat.message.critical(title="Rename", buttons=["Close"],
                                    message_text='"%s" already exists.' % name, parent=self)
            return
        try:
            os.rename(path, target)
        except OSError as error:
            compat.message.critical(title="Rename", buttons=["Close"],
                                    message_text="Could not rename it.", informative_text=str(error), parent=self)
            return
        self.pathRenamed.emit(path, target)
        self.refresh()

    def _delete(self, path:str) -> None:
        """Delete a file or folder, after asking. There is no undo for this one.

        Returns:
            None.
        """
        name = os.path.basename(path)
        folder = os.path.isdir(path)
        count = sum(len(files) for _, _, files in os.walk(path)) if folder else 0
        answer = compat.message.warning(
            title="Delete", buttons=["Delete", "Cancel"],
            message_text='Delete "%s"?' % name,
            informative_text=("The folder and the %d file(s) inside it are removed from disk. "
                              "This cannot be undone." % count) if folder
            else "The file is removed from disk. This cannot be undone.", parent=self)
        if answer != "Delete":
            return
        try:
            if folder:
                import shutil
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError as error:
            compat.message.critical(title="Delete", buttons=["Close"],
                                    message_text="Could not delete it.", informative_text=str(error), parent=self)
            return
        self.pathDeleted.emit(path)
        self.refresh()


class SearchPanel(qt.QWidget):
    """Search view: find - and replace - a string across every text file under the workspace roots.

    The query bar is the SAME widget as the editor's find panel (core.find.OptionField). The toggles
    have to mean the same thing in both places, and one implementation is the only way to be sure
    they keep doing so.
    """

    resultActivated = qt.signal(str, int)      # absolute path, 1-based line number

    MAX_RESULTS = 400                          # stop early; the count label says when results were cut
    MAX_BYTES   = 2 * 1024 * 1024              # skip anything bigger, it is not a script
    SKIP_DIRS   = {".git", "__pycache__", "node_modules", ".vs", ".vscode",
                   ".mypy_cache", ".pytest_cache", ".idea"}
    DELAY       = 350                          # ms of quiet before a walk starts

    def __init__(self, roots_provider, parent:qt.QWidget=None) -> None:
        """Build the search view: query and replace fields, options and the results tree.

        `roots_provider` is a callable returning the current list of workspace root folders.

        Returns:
            None.
        """
        super().__init__(parent)
        self._roots = roots_provider
        self.scope = None                # a single folder to search instead of the whole workspace
        self.setObjectName("codeSearch")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)

        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(qt.px(4), qt.px(4), qt.px(6), 0)
        layout.setSpacing(qt.px(4))

        top = qt.QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(qt.px(3))

        self.toggle = qt.QToolButton()
        self.toggle.setObjectName("codeFindToggle")
        self.toggle.setCheckable(True)
        self.toggle.setAutoRaise(True)
        self.toggle.setIconSize(qt.QSize(qt.px(16), qt.px(16)))
        self.toggle.setFixedWidth(qt.px(22))
        self.toggle.setSizePolicy(qt.QSizePolicy.Fixed, qt.QSizePolicy.Expanding)
        self.toggle.setToolTip("Toggle Replace")
        self.toggle.toggled.connect(self._toggle_replace)
        top.addWidget(self.toggle, 0)

        # a grid, so the Search and Replace inputs share one stretching column and stay the same
        # width - two rows sized independently never line up
        rows = qt.QGridLayout()
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setHorizontalSpacing(qt.px(2))
        rows.setVerticalSpacing(qt.px(3))
        rows.setColumnStretch(0, 1)
        top.addLayout(rows, 1)
        layout.addLayout(top, 0)

        self.field = find.OptionField("Search")
        self.case = self.field.option("Aa", "Match Case")
        self.word = self.field.option("ab", "Match Whole Word")
        font = self.word.font(); font.setUnderline(True); self.word.setFont(font)
        self.regex = self.field.option(".*", "Use Regular Expression")
        rows.addWidget(self.field, 0, 0)

        self.replace_field = find.OptionField("Replace")
        self.preserve = self.replace_field.option("AB", "Preserve Case")
        rows.addWidget(self.replace_field, 1, 0)

        self.replace_all = qt.QToolButton()
        self.replace_all.setObjectName("codeFindNav")
        self.replace_all.setIcon(find.glyph("replace_all"))
        self.replace_all.setIconSize(qt.QSize(qt.px(16), qt.px(16)))
        self.replace_all.setFixedSize(qt.QSize(qt.px(24), qt.px(24)))
        self.replace_all.setToolTip("Replace All")
        self.replace_all.setAutoRaise(True)
        self.replace_all.setFocusPolicy(qt.Qt.NoFocus)
        self.replace_all.clicked.connect(self.run_replace_all)
        rows.addWidget(self.replace_all, 1, 1)

        self.replace_row = (self.replace_field, self.replace_all)
        for widget in self.replace_row:
            widget.setVisible(False)

        self.summary = qt.QLabel("")
        self.summary.setObjectName("codeHint")
        layout.addWidget(self.summary, 0)

        self.results = qt.QTreeWidget()
        self.results.setHeaderHidden(True)
        self.results.setIndentation(qt.px(12))
        self.results.itemActivated.connect(self._activate)
        self.results.itemClicked.connect(self._activate)
        layout.addWidget(self.results, 1)

        # walking the whole workspace on every keystroke would stall the window, so the search waits
        # for a pause in typing - the same shape as the problems and outline refresh
        self._timer = qt.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.DELAY)
        self._timer.timeout.connect(self.run_search)
        self.field.edit.textChanged.connect(lambda *_: self._timer.start())
        self.field.edit.returnPressed.connect(self.run_search)
        for option in (self.case, self.word, self.regex):
            option.toggled.connect(lambda *_: self._timer.start())
        self._refresh_chevron()

    # ---- replace row

    def _toggle_replace(self, on:bool) -> None:
        """Show or hide the replace row and refresh the toggle chevron.

        Returns:
            None.
        """
        for widget in self.replace_row:
            widget.setVisible(on)
        self._refresh_chevron()

    def _refresh_chevron(self) -> None:
        """The same chevron images the tree and the find panel use, not a text arrow.

        Returns:
            None.
        """
        self.toggle.setIcon(_icon("chevron_down.png" if self.toggle.isChecked()
                                  else "chevron_right.png"))

    # ---- searching

    def _pattern(self) -> object:
        """The compiled query, or None when it is empty or an invalid regular expression.

        Returns:
            object: the compiled query, or None when empty or an invalid regex.
        """
        return find.compiled_query(self.field.edit.text(), regex=self.regex.isChecked(),
                                   word=self.word.isChecked(), case=self.case.isChecked())

    def _walk(self) -> None:
        """Every file under the current scope: the workspace roots, or one folder.

        Returns:
            None.
        """
        for root in ([self.scope] if self.scope else (self._roots() or [])):
            for folder, dirs, names in os.walk(root):
                dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS]
                for name in names:
                    yield root, os.path.join(folder, name)

    def run_search(self) -> None:
        """Walk the scope and list every line matching the query.

        Returns:
            None.
        """
        self.results.clear()
        pattern = self._pattern()
        if pattern is None:
            self.summary.setText("Bad pattern." if self.field.edit.text() else "")
            return

        matches, files, truncated = 0, 0, False
        for root, path in self._walk():
            try:
                if os.path.getsize(path) > self.MAX_BYTES:
                    continue
                content = compat.folder.read(path)
            except Exception:
                continue                           # unreadable or binary: skip silently
            hits = [(n, line) for n, line in enumerate(content.splitlines(), 1)
                    if pattern.search(line)]
            if not hits:
                continue
            parent = qt.QTreeWidgetItem(self.results, [os.path.relpath(path, root)])
            parent.setIcon(0, file_icon(path))
            parent.setData(0, _PATH_ROLE, path)
            parent.setExpanded(True)
            files += 1
            for number, line in hits:
                child = qt.QTreeWidgetItem(parent, ["%d: %s" % (number, line.strip()[:160])])
                child.setData(0, _PATH_ROLE, path)
                child.setData(0, _LINE_ROLE, number)
                matches += 1
                if matches >= self.MAX_RESULTS:
                    truncated = True
                    break
            if truncated:
                break

        self.summary.setText(
            "%d result%s in %d file%s%s" % (matches, "" if matches == 1 else "s",
                                            files, "" if files == 1 else "s",
                                            "  (stopped at %d)" % self.MAX_RESULTS if truncated else ""))

    # ---- replacing

    def _replacer(self, replacement:str) -> object:
        """The substitution callable, honouring the AB (Preserve Case) toggle.

        Off, the text goes in verbatim. On, each hit keeps the casing it had - so replacing `node`
        with `joint` turns `Node` into `Joint` and `NODE` into `JOINT` in the same pass.

        Returns:
            object: the substitution callable passed to re.sub.
        """
        if not self.preserve.isChecked():
            return lambda match: replacement

        def cased(match) -> object:
            """Return the replacement cased to match the text that was found.

            Returns:
                object: the replacement in the matched text's case.
            """
            found = match.group()
            if found.isupper():
                return replacement.upper()
            if found[:1].isupper():
                return replacement[:1].upper() + replacement[1:]
            return replacement.lower()

        return cased

    def run_replace_all(self) -> None:
        """Rewrite every match across the scope, after saying exactly how much it will touch.

        The files go through session.write_atomic: a replace that fails midway must not leave a
        source truncated, and nothing here can be undone once dozens of files have been rewritten.

        Returns:
            None.
        """
        pattern = self._pattern()
        if pattern is None:
            return
        replacement = self.replace_field.edit.text()

        targets = []
        for _, path in self._walk():
            try:
                if os.path.getsize(path) > self.MAX_BYTES:
                    continue
                content = compat.folder.read(path)
            except Exception:
                continue
            new, count = pattern.subn(self._replacer(replacement), content)
            if count:
                targets.append((path, new, count))
        if not targets:
            self.summary.setText("Nothing to replace.")
            return

        total = sum(count for _, _, count in targets)
        answer = compat.message.warning(
            title="Replace All", buttons=["Replace", "Cancel"],
            message_text="Replace %d occurrence%s in %d file%s?"
                         % (total, "" if total == 1 else "s",
                            len(targets), "" if len(targets) == 1 else "s"),
            informative_text="The files are rewritten on disk. This cannot be undone.",
            parent=self)
        if answer != "Replace":
            return

        done = 0
        for path, new, _ in targets:
            try:
                session.write_atomic(path, new)
                done += 1
            except Exception:
                pass                               # locked or read-only: keep going, report the count
        self.summary.setText("Replaced in %d of %d file%s."
                             % (done, len(targets), "" if len(targets) == 1 else "s"))
        self.run_search()

    def _activate(self, item, column=0) -> None:
        """Emit resultActivated for the file and line of the clicked result.

        Returns:
            None.
        """
        path = item.data(0, _PATH_ROLE)
        if path:
            self.resultActivated.emit(path, int(item.data(0, _LINE_ROLE) or 1))


class GraphDelegate(qt.QStyledItemDelegate):
    """Commit row: the graph rail with its node on the left, subject, author, and ref pills."""

    RAIL   = "#4a8fd0"
    PILL   = "#0078d4"
    MUTED  = "#6a6f75"
    RAIL_X = 10                # design-space centre of the rail

    def sizeHint(self, option, index) -> object:
        """Force a minimum row height of 22px.

        Returns:
            object: the item size hint, raised to at least 22px tall.
        """
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), qt.px(22)))
        return size

    def paint(self, painter, option, index) -> None:
        """Paint one commit row: the graph rail and node, ref pills, author and subject.

        Returns:
            None.
        """
        painter.save()
        painter.setRenderHint(qt.QPainter.Antialiasing, True)
        rect = qt.QRect(option.rect)
        selected = bool(option.state & qt.QStyle.State_Selected)

        if selected:
            painter.fillRect(rect, qt.QColor(255, 255, 255, 28))
        elif option.state & qt.QStyle.State_MouseOver:
            painter.fillRect(rect, qt.QColor(255, 255, 255, 14))

        # ---- rail + node (linear history: one continuous line through every row)
        centre_x = rect.left() + qt.px(self.RAIL_X)
        centre_y = rect.center().y()
        pen = qt.QPen(qt.QColor(self.RAIL)); pen.setWidth(qt.px(1)); pen.setCosmetic(True)
        painter.setPen(pen)
        model = index.model()
        if index.row() > 0:
            painter.drawLine(centre_x, rect.top(), centre_x, centre_y)
        if index.row() < model.rowCount() - 1:
            painter.drawLine(centre_x, centre_y, centre_x, rect.bottom())

        radius = qt.px(4)
        head = bool(index.data(_HEAD_ROLE))
        painter.setBrush(qt.Qt.NoBrush if head else qt.QColor(self.RAIL))
        painter.drawEllipse(qt.QPoint(centre_x, centre_y), radius, radius)

        left = centre_x + radius + qt.px(8)
        right_limit = rect.right() - qt.px(6)
        metrics = option.fontMetrics

        # ---- ref pills (branch / tag names) drawn on the right
        for name in reversed(index.data(_REFS_ROLE) or []):
            width = metrics.horizontalAdvance(name) + qt.px(12)
            height = qt.px(15)
            pill = qt.QRect(right_limit - width, centre_y - height // 2, width, height)
            if pill.left() < left:
                break
            painter.setPen(qt.Qt.NoPen)
            painter.setBrush(qt.QColor(self.PILL))
            painter.drawRoundedRect(pill, height / 2.0, height / 2.0)
            painter.setPen(qt.QColor("#ffffff"))
            painter.drawText(pill, qt.Qt.AlignCenter, name)
            right_limit = pill.left() - qt.px(4)

        # ---- author, then subject in the remaining space
        author = index.data(_AUTHOR_ROLE) or ""
        if author:
            width = metrics.horizontalAdvance(author)
            if right_limit - width > left + qt.px(40):
                painter.setPen(qt.QColor(self.MUTED))
                painter.setFont(option.font)
                painter.drawText(qt.QRect(right_limit - width, rect.top(), width, rect.height()),
                                 qt.Qt.AlignLeft | qt.Qt.AlignVCenter, author)
                right_limit -= width + qt.px(8)

        subject = index.data(qt.Qt.DisplayRole) or ""
        font = qt.QFont(option.font); font.setBold(head)
        painter.setFont(font)
        painter.setPen(qt.QColor("#ffffff" if head else "#cccccc"))
        available = max(0, right_limit - left)
        painter.drawText(qt.QRect(left, rect.top(), available, rect.height()),
                         qt.Qt.AlignLeft | qt.Qt.AlignVCenter,
                         qt.QFontMetrics(font).elidedText(subject, qt.Qt.ElideRight, available))
        painter.restore()


class GraphPanel(qt.QWidget):
    """Commit history for the workspace repositories, VS Code's GRAPH section."""

    LIMIT = 60

    def __init__(self, roots_provider, parent:qt.QWidget=None) -> None:
        """Build the graph panel: a commit list view painted by the graph delegate.

        Returns:
            None.
        """
        super().__init__(parent)
        self._roots = roots_provider
        self.setObjectName("codeGraph")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)

        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.commits = qt.QListWidget()
        self.commits.setObjectName("codeGraphList")
        self.commits.setItemDelegate(GraphDelegate(self.commits))
        self.commits.setObjectName("codeGraphList")
        layout.addWidget(self.commits, 1)

    def refresh(self) -> None:
        """Re-read the log of the first repository among the workspace roots.

        Returns:
            None.
        """
        self.commits.clear()
        for root in (self._roots() or []):
            try:
                if not vcs.is_git_repo(root):
                    continue
                entries = vcs.log(root, limit=self.LIMIT)
            except Exception:
                continue
            for position, commit in enumerate(entries):
                item = qt.QListWidgetItem(commit["subject"])
                item.setData(_AUTHOR_ROLE, commit["author"])
                item.setData(_REFS_ROLE, commit["refs"])
                item.setData(_HEAD_ROLE, position == 0)
                item.setToolTip(self._tooltip(commit))
                self.commits.addItem(item)
            break                       # one repository at a time, like VS Code's graph

    @staticmethod
    def _tooltip(commit:dict) -> str:
        """Rich hover card for a commit: subject, refs, author, date, then the full body.

        Returns:
            str: the HTML hover card for the commit.
        """
        def escape(text) -> object:
            """HTML-escape `text`.

            Returns:
                object: the HTML-escaped text.
            """
            return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ["<div style='max-width:560px'>",
                 "<b>%s</b>" % escape(commit.get("subject"))]

        refs = commit.get("refs") or []
        if refs:
            lines.append("<div style='color:#8db9e2'>%s</div>" % escape("  ".join(refs)))

        lines.append(
            "<div style='color:#9d9d9d'>%s &nbsp;•&nbsp; %s &lt;%s&gt;</div>"
            % (escape(commit.get("short")), escape(commit.get("author")), escape(commit.get("email"))))
        lines.append(
            "<div style='color:#9d9d9d'>%s &nbsp;•&nbsp; %s</div>"
            % (escape(commit.get("date")), escape(commit.get("relative"))))

        body = (commit.get("body") or "").strip()
        if body:
            lines.append("<hr>")
            lines.append("<div style='color:#cccccc'>%s</div>"
                         % escape(body).replace("\n", "<br>"))
        lines.append("</div>")
        return "".join(lines)


class SourceControlDelegate(qt.QStyledItemDelegate):
    """Row painter for the source-control tree, matching VS Code.

    A group header shows its title with the change count in a blue bubble; a file row shows its icon,
    the file name, the folder path dimmed beside it, and the status letter right-aligned in colour.
    """

    BUBBLE      = qt.selection_blue
    SELECT      = "#5db4e5"
    GUIDE_COLOR = "#333333"
    MUTED       = "#6a6f75"
    TEXT        = "#cccccc"

    def __init__(self, tree, parent=None) -> None:
        """Store the tree reference used to compute the indent guide lines.

        Returns:
            None.
        """
        super().__init__(parent)
        self._tree = tree                    # needed for indentation() when drawing the guide lines

    def sizeHint(self, option, index) -> object:
        """Force a minimum row height of 24px.

        Returns:
            object: the item size hint, raised to at least 24px tall.
        """
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), qt.px(24)))
        return size

    def paint(self, painter, option, index) -> None:
        """Paint one source-control row: a group header with count bubble, or a file row.

        Returns:
            None.
        """
        painter.save()
        rect = qt.QRect(option.rect)
        selected = bool(option.state & qt.QStyle.State_Selected)
        hovered  = bool(option.state & qt.QStyle.State_MouseOver)

        # indent guides, drawn exactly like the explorer's
        depth = 0
        parent = index.parent()
        while parent.isValid():
            depth += 1
            parent = parent.parent()
        if depth > 0 and not selected:
            indent = self._tree.indentation()
            pen = qt.QPen(qt.QColor(self.GUIDE_COLOR)); pen.setWidth(1); pen.setCosmetic(True)
            painter.setPen(pen)
            for back in range(1, depth + 1):
                x = rect.left() - back * indent - qt.px(7)
                painter.drawLine(x, rect.top(), x, rect.bottom())

        if selected:
            painter.fillRect(rect, qt.QColor(255, 255, 255, 28))
            pen = qt.QPen(qt.QColor(self.SELECT)); pen.setWidth(1); pen.setCosmetic(True)
            painter.setPen(pen); painter.setBrush(qt.Qt.NoBrush)
            painter.drawRect(qt.QRect(rect.left(), rect.top(), rect.width() - 1, rect.height() - 1))
        elif hovered:
            painter.fillRect(rect, qt.QColor(255, 255, 255, 14))

        text = index.data(qt.Qt.DisplayRole) or ""
        count = index.data(_COUNT_ROLE)
        left = rect.left() + qt.px(4)
        right_limit = rect.right() - qt.px(8)

        if count is not None:
            # ---- header: optional icon, title, dimmed subtitle, then a blue count bubble on the right
            header_icon = index.data(qt.Qt.DecorationRole)
            if header_icon is not None and not header_icon.isNull():
                size = qt.px(16)
                header_icon.paint(painter, qt.QRect(left, rect.center().y() - size // 2, size, size),
                                  qt.Qt.AlignCenter)
                left += size + qt.px(6)

            metrics = option.fontMetrics
            label = str(count)
            width = metrics.horizontalAdvance(label) + qt.px(12)
            height = qt.px(16)
            bubble = qt.QRect(right_limit - width, rect.center().y() - height // 2, width, height)

            painter.setFont(option.font)
            painter.setPen(qt.QColor("#cccccc"))
            name_width = metrics.horizontalAdvance(text)
            painter.drawText(qt.QRect(left, rect.top(), name_width, rect.height()),
                             qt.Qt.AlignLeft | qt.Qt.AlignVCenter, text)
            left += name_width + qt.px(8)

            header_subtitle = index.data(_SUBTITLE_ROLE) or ""
            available = bubble.left() - qt.px(6) - left
            if header_subtitle and available > qt.px(20):
                painter.setPen(qt.QColor(self.MUTED))
                painter.drawText(qt.QRect(left, rect.top(), available, rect.height()),
                                 qt.Qt.AlignLeft | qt.Qt.AlignVCenter,
                                 metrics.elidedText(header_subtitle, qt.Qt.ElideLeft, available))

            painter.setPen(qt.Qt.NoPen)
            painter.setBrush(qt.QColor(self.BUBBLE))
            painter.drawRoundedRect(bubble, height / 2.0, height / 2.0)
            painter.setPen(qt.QColor("#ffffff"))
            painter.drawText(bubble, qt.Qt.AlignCenter, label)
            painter.restore()
            return

        # ---- an Open Editors row keeps a slot on the left for its close mark. The slot is ALWAYS
        # reserved, and only drawn on hover: showing it only when hovered without reserving it would
        # shove the whole row sideways under the pointer.
        if index.data(_CLOSE_ROLE):
            slot = qt.px(16)
            if hovered:
                mark = qt.QRectF(left + qt.px(3), rect.center().y() - qt.px(5),
                                 qt.px(10), qt.px(10))
                cross = qt.QPen(qt.QColor(self.TEXT))
                cross.setWidthF(max(1.0, qt.px(1)))
                cross.setCapStyle(qt.Qt.RoundCap)
                painter.setPen(cross)
                painter.drawLine(mark.topLeft(), mark.bottomRight())
                painter.drawLine(mark.topRight(), mark.bottomLeft())
            left += slot

        # ---- file row: icon, name, dimmed folder, coloured status letter
        icon = index.data(qt.Qt.DecorationRole)
        if icon is not None and not icon.isNull():
            size = qt.px(16)
            icon.paint(painter, qt.QRect(left, rect.center().y() - size // 2, size, size),
                       qt.Qt.AlignCenter)
            left += size + qt.px(6)

        letter = index.data(_STATUS_ROLE)
        colour = _STATUS_COLOR.get(letter, "#cccccc")
        metrics = option.fontMetrics
        letter_width = metrics.horizontalAdvance(letter or "") + qt.px(8)

        # a right-aligned dim trailer (the timeline's "2 wks"), measured first so the name and the
        # subtitle know how much room is actually left
        trail = index.data(_TRAIL_ROLE) or ""
        trail_width = 0
        if trail:
            small = qt.QFont(option.font)
            if option.font.pointSizeF() > 0:
                small.setPointSizeF(max(6.0, option.font.pointSizeF() - 1.0))
            else:
                small.setPixelSize(max(8, option.font.pixelSize() - 1))
            trail_width = qt.QFontMetrics(small).horizontalAdvance(trail) + qt.px(10)
            painter.setFont(small)
            painter.setPen(qt.QColor(self.MUTED))
            painter.drawText(qt.QRect(right_limit - letter_width - trail_width + qt.px(4),
                                      rect.top(), trail_width, rect.height()),
                             qt.Qt.AlignRight | qt.Qt.AlignVCenter, trail)
        right_limit -= trail_width

        painter.setFont(option.font)
        painter.setPen(qt.QColor("#cccccc"))
        # elide against what is actually left: right_limit already has the trailer subtracted, so a
        # long commit subject now stops before the "2 wks" instead of running underneath it
        room = max(0, right_limit - letter_width - left)
        name = metrics.elidedText(text, qt.Qt.ElideRight, room)
        name_width = min(metrics.horizontalAdvance(name), room)
        painter.drawText(qt.QRect(left, rect.top(), name_width, rect.height()),
                         qt.Qt.AlignLeft | qt.Qt.AlignVCenter, name)
        left += name_width + qt.px(8)

        subtitle = index.data(_SUBTITLE_ROLE) or ""
        if subtitle:
            small = qt.QFont(option.font)
            # a font set in pixels reports pointSizeF() == -1, so shrink whichever unit is in use
            if option.font.pointSizeF() > 0:
                small.setPointSizeF(max(6.0, option.font.pointSizeF() - 1.0))
            else:
                small.setPixelSize(max(8, option.font.pixelSize() - 1))
            painter.setFont(small)
            painter.setPen(qt.QColor(self.MUTED))
            available = right_limit - letter_width - left
            if available > qt.px(20):
                elided = qt.QFontMetrics(small).elidedText(subtitle, qt.Qt.ElideLeft, available)
                painter.drawText(qt.QRect(left, rect.top(), available, rect.height()),
                                 qt.Qt.AlignLeft | qt.Qt.AlignVCenter, elided)

        if letter:
            bold = qt.QFont(option.font); bold.setBold(True)
            painter.setFont(bold)
            painter.setPen(qt.QColor(colour))
            painter.drawText(qt.QRect(rect.left(), rect.top(), right_limit - rect.left(), rect.height()),
                             qt.Qt.AlignRight | qt.Qt.AlignVCenter, letter)
        painter.restore()


class SourceControlPanel(qt.QWidget):
    """Source control view: the changed files reported by git, across every workspace root."""

    fileActivated = qt.signal(str)
    countChanged  = qt.signal(int)             # total changes, drives the activity-bar badge
    diffRequested = qt.signal(str, str, bool, bool)  # path, root, staged?, preview?

    def __init__(self, roots_provider, parent:qt.QWidget=None) -> None:
        """Build the source-control view: a Changes tree and a Graph section in a splitter.

        Returns:
            None.
        """
        super().__init__(parent)
        self._roots = roots_provider
        self.setObjectName("codeScm")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)

        # full-width collapsible sections, exactly like Outline / Timeline in the explorer view
        self._layout = qt.QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(1)

        self.changes = qt.QTreeWidget()
        self.changes.setObjectName("codeScmTree")       # custom-painted: keep stylesheet items transparent
        self.changes.setHeaderHidden(True)
        self.changes.setIndentation(qt.px(14))          # same chevron gutter as the explorer tree
        self.changes.setIconSize(qt.QSize(qt.px(16), qt.px(16)))
        self.changes.setItemDelegate(SourceControlDelegate(self.changes))
        self.changes.itemClicked.connect(lambda item, col=0: self._activate(item, col, preview=True))
        self.changes.itemDoubleClicked.connect(lambda item, col=0: self._activate(item, col, preview=False))
        self.changes_section = CollapsibleSection("Changes", self.changes, expanded=True)

        self.graph = GraphPanel(roots_provider)
        self.graph_section = CollapsibleSection("Graph", self.graph, expanded=True)

        # a splitter, so the boundary between Changes and Graph can be dragged
        self.sections = SectionStack([self.changes_section, self.graph_section])
        self._layout.addWidget(self.sections, 1)
        self._layout.addStretch(0)      # takes the slack once every section is collapsed

    def refresh(self) -> None:
        """Re-query git and rebuild the change list, split into staged and unstaged like VS Code.

        Returns:
            None.
        """
        self.graph.refresh()
        self.changes.clear()
        total = 0
        for root in (self._roots() or []):
            try:
                if not vcs.is_git_repo(root):
                    continue
                staged, unstaged = vcs.changes(root)
            except Exception:
                continue

            # repository node carrying the branch name, then one group per git state, then the files
            label = os.path.basename(root.rstrip("/\\")) or root
            repo_item = qt.QTreeWidgetItem(self.changes, [label])
            font = repo_item.font(0); font.setBold(True); repo_item.setFont(0, font)
            repo_item.setData(0, _SUBTITLE_ROLE, vcs.branch(root))
            repo_item.setExpanded(True)
            repo_item.setData(0, _ROOT_ROLE, root)

            for title, group, is_staged in (("Staged Changes", staged, True),
                                            ("Changes", unstaged, False)):
                if not group:
                    continue
                header = qt.QTreeWidgetItem(repo_item, [title])
                header.setData(0, _COUNT_ROLE, len(group))     # drives the blue bubble
                header.setExpanded(True)
                for path in sorted(group):
                    letter = group[path]
                    relative = os.path.relpath(path, root)
                    child = qt.QTreeWidgetItem(header, [os.path.basename(path)])
                    child.setIcon(0, file_icon(path))
                    child.setData(0, _PATH_ROLE, path)
                    child.setData(0, _ROOT_ROLE, root)
                    child.setData(0, _STAGED_ROLE, is_staged)
                    child.setData(0, _STATUS_ROLE, letter)
                    child.setData(0, _SUBTITLE_ROLE, os.path.dirname(relative))
                    child.setToolTip(0, "%s  •  %s" % (path, _STATUS_TOOLTIP.get(letter, letter)))
                    total += 1

        self.countChanged.emit(total)

    def _activate(self, item, column=0, preview:bool=True) -> None:
        """A file row opens its diff (preview on single click, pinned on double); groups just fold.

        Returns:
            None.
        """
        path = item.data(0, _PATH_ROLE)
        if not path:
            item.setExpanded(not item.isExpanded())
            return
        self.diffRequested.emit(path, item.data(0, _ROOT_ROLE) or "",
                                bool(item.data(0, _STAGED_ROLE)), preview)


class ActivityBar(qt.QWidget):
    """The narrow vertical strip of view icons on the far left (VS Code's activity bar)."""

    viewChanged = qt.signal(str)
    manageClicked = qt.signal()                # the gear at the bottom
    WIDTH = 48

    def __init__(self, parent:qt.QWidget=None) -> None:
        """Build the activity bar: the view toggle buttons and the bottom gear.

        Returns:
            None.
        """
        super().__init__(parent)
        self.setObjectName("codeActivityBar")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)
        # same surface as the workspace side bar; the 1px rule to its right is a QFrame added by the
        # window (a stylesheet border on a bare QWidget is not reliably painted)
        self.setFixedWidth(qt.px(self.WIDTH))

        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(0, qt.px(4), 0, 0)
        layout.setSpacing(qt.px(2))

        self._buttons = {}
        self._badges = {}
        for name, base, tip in (("explorer", "act_explorer", "Explorer"),
                                ("search",   "act_search",   "Search"),
                                ("scm",      "act_scm",      "Source Control")):
            button = qt.QToolButton()
            button.setToolTip(tip)
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.setIconSize(qt.QSize(qt.px(24), qt.px(24)))
            button.setFixedHeight(qt.px(44))
            button.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
            button.setObjectName("codeActivityButton")
            button.clicked.connect(lambda *_, n=name: self.select(n))
            layout.addWidget(button, 0)
            self._buttons[name] = (button, base)

        layout.addStretch(1)

        # the gear sits BELOW the stretch, pinned to the bottom of the strip like VS Code's
        self.manage = qt.QToolButton()
        self.manage.setToolTip("Manage")
        self.manage.setAutoRaise(True)
        self.manage.setObjectName("codeActivityButton")
        self.manage.setIconSize(qt.QSize(qt.px(22), qt.px(22)))
        self.manage.setFixedHeight(qt.px(44))
        self.manage.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
        self.manage.setIcon(gear_icon())
        self.manage.clicked.connect(self.manageClicked)
        layout.addWidget(self.manage, 0)

        self.select("explorer")

    def select(self, name:str) -> None:
        """Activate `name`, refresh every icon, and announce the change.

        Returns:
            None.
        """
        for key, (button, base) in self._buttons.items():
            active = key == name
            button.setChecked(active)
            button.setIcon(_icon("%s_%s.png" % (base, "on" if active else "off")))
        self.viewChanged.emit(name)

    def current(self) -> str:
        """The name of the currently selected view.

        Returns:
            str: the checked view's name, or 'explorer' when none is checked.
        """
        for key, (button, _) in self._buttons.items():
            if button.isChecked():
                return key
        return "explorer"

    def set_badge(self, name:str, count:int) -> None:
        """Show a small count bubble on a view's icon (VS Code's source-control badge). 0 hides it.

        Returns:
            None.
        """
        entry = self._buttons.get(name)
        if not entry:
            return
        button = entry[0]
        badge = self._badges.get(name)
        if badge is None:
            badge = qt.QLabel(button)
            badge.setAlignment(qt.Qt.AlignCenter)
            badge.setObjectName("codeActivityBadge")
            self._badges[name] = badge
        if not count:
            badge.hide()
            return
        badge.setText(str(count) if count < 1000 else "999+")
        badge.adjustSize()
        badge.move(max(0, button.width() - badge.width() - qt.px(6)),
                   max(0, button.height() - badge.height() - qt.px(6)))
        badge.show()


class Sidebar(qt.QWidget):
    """VS-Code-style left sidebar: one view at a time (Explorer / Search / Source Control)."""

    fileActivated    = qt.signal(str)      # preview open (single click)
    filePinned       = qt.signal(str)      # permanent open (double click)
    resultActivated  = qt.signal(str, int)
    diffRequested    = qt.signal(str, str, bool, bool)
    runRequested     = qt.signal(str)      # explorer context menu, relayed to the window
    revealRequested  = qt.signal(str)
    editorActivated  = qt.signal(int)      # open editors: bring this tab forward
    editorClosed     = qt.signal(int)
    closeOthers      = qt.signal(int)
    closeAllEditors  = qt.signal()
    symbolActivated  = qt.signal(int)      # outline: jump to this line of the current file
    revisionActivated = qt.signal(str, str, str)   # timeline: path, repo root, commit sha
    variablePrint    = qt.signal(str)      # variables: echo a global's full repr to the output
    commitRequested   = qt.signal(str, str, str)
    pathRenamed      = qt.signal(str, str)
    pathDeleted      = qt.signal(str)

    def __init__(self, parent:qt.QWidget=None) -> None:
        """Build the sidebar: the Explorer, Search and Source Control views in a stack.

        Returns:
            None.
        """
        super().__init__(parent)
        self.setObjectName("codeSidebar")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)

        outer = qt.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # these containers need their own surface, otherwise the global "QWidget { background: #121314 }"
        # rule paints them darker than the side bar they live in
        self.views = qt.QStackedWidget()
        self.views.setObjectName("codeSidebarViews")
        self.views.setAttribute(qt.Qt.WA_StyledBackground, True)
        outer.addWidget(self.views, 1)

        # ---- Explorer view: the collapsible sections
        explorer = qt.QWidget()
        explorer.setObjectName("codeExplorerView")
        explorer.setAttribute(qt.Qt.WA_StyledBackground, True)
        self._layout = qt.QVBoxLayout(explorer)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(1)

        # WORKSPACE — the real, working section
        self.workspace = WorkspacePanel()
        self.workspace.fileActivated.connect(self.fileActivated)
        self.workspace.filePinned.connect(self.filePinned)
        self.workspace.runRequested.connect(self.runRequested)
        self.workspace.revealRequested.connect(self.revealRequested)
        self.workspace.pathRenamed.connect(self.pathRenamed)
        self.workspace.pathDeleted.connect(self.pathDeleted)
        self.workspace.searchRequested.connect(self._search_folder)
        self.workspace_section = CollapsibleSection("Workspace", self.workspace, expanded=True)

        # OPEN EDITORS — sits above the tree, as VS Code orders it
        self.open_editors = OpenEditorsPanel()
        self.open_editors.editorActivated.connect(self.editorActivated)
        self.open_editors.editorClosed.connect(self.editorClosed)
        self.open_editors.closeOthers.connect(self.closeOthers)
        self.open_editors.closeAll.connect(self.closeAllEditors)
        self.open_section = CollapsibleSection("Open Editors", self.open_editors, expanded=True)
        # never shares the column's free space: it is exactly as tall as its list asks to be, so
        # Workspace keeps every pixel below it
        self.open_section.set_resizable(False)
        self.open_section.refresh_height()

        # OUTLINE — the symbols of the current file, read from its syntax tree
        self.outline = OutlinePanel()
        self.outline.symbolActivated.connect(self.symbolActivated)
        self.outline_section = CollapsibleSection("Outline", self.outline, expanded=False)

        # VARIABLES — the user globals of the shared run namespace (what your scripts left behind)
        self.variables = VariablesPanel()
        self.variables.printRequested.connect(self.variablePrint)
        self.variables_section = CollapsibleSection("Variables", self.variables, expanded=False)

        # TIMELINE — the git history of the current file
        self.timeline = TimelinePanel()
        self.timeline.revisionActivated.connect(self.revisionActivated)
        self.timeline.commitRequested.connect(self.commitRequested)
        self.timeline_section = CollapsibleSection("Timeline", self.timeline, expanded=False)

        # a splitter, so Workspace / Outline / Timeline boundaries can be dragged
        # Open Editors is NOT in the stack: only the sections that share the free space belong there.
        # leads=False: Open Editors heads this column, so Workspace keeps the separator that tells
        # the two apart
        self.sections = SectionStack(
            [self.workspace_section, self.outline_section, self.variables_section, self.timeline_section], leads=False)
        self.open_section.set_leading(True)
        self.explorer_header = ViewHeader("Explorer")
        self.explorer_header.set_sections([("Open Editors", self.open_section),
                                           ("Workspace", self.workspace_section),
                                           ("Outline", self.outline_section),
                                           ("Variables", self.variables_section),
                                           ("Timeline", self.timeline_section)])
        self._layout.insertWidget(0, self.explorer_header, 0)
        self._layout.insertWidget(1, self.open_section, 0)
        self._layout.addWidget(self.sections, 1)
        self._layout.addStretch(0)      # takes the slack once every section is collapsed
        self.views.addWidget(explorer)                       # index 0

        # ---- Search view: one panel, so its header carries the title alone
        self.search = SearchPanel(lambda: self.workspace.roots)
        self.search.resultActivated.connect(self.resultActivated)
        self.search_header = ViewHeader("Search")
        self.views.addWidget(self._titled(self.search_header, self.search))   # index 1

        # ---- Source control view
        self.scm = SourceControlPanel(lambda: self.workspace.roots)
        self.scm.fileActivated.connect(self.fileActivated)
        self.scm.diffRequested.connect(self.diffRequested)
        self.scm_header = ViewHeader("Source Control")
        self.scm_header.set_sections([("Changes", self.scm.changes_section),
                                      ("Graph", self.scm.graph_section)])
        self.views.addWidget(self._titled(self.scm_header, self.scm))         # index 2

        self._view_index = {"explorer": 0, "search": 1, "scm": 2}

    @staticmethod
    def _titled(header, panel) -> qt.QWidget:
        """Stack a view header over a panel, as one widget for the stacked view.

        Returns:
            qt.QWidget: the wrapper holding the header above the panel.
        """
        wrapper = qt.QWidget()
        wrapper.setObjectName("codeSidebarViews")        # same surface as the stack it goes into
        wrapper.setAttribute(qt.Qt.WA_StyledBackground, True)
        layout = qt.QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header, 0)
        layout.addWidget(panel, 1)
        return wrapper

    def _search_folder(self, folder:str) -> None:
        """Point the Search view at one folder and bring it forward.

        Returns:
            None.
        """
        self.search.scope = folder
        self.search.field.setPlaceholderText("Search in %s" % os.path.basename(folder.rstrip("/\\")))
        self.show_view("search")
        self.search.field.setFocus()
        self.search.field.selectAll()

    def show_view(self, name:str) -> None:
        """Switch the sidebar to one of the activity-bar views.

        Returns:
            None.
        """
        self.views.setCurrentIndex(self._view_index.get(name, 0))
        if name == "search":
            self.search.field.setFocus()
        elif name == "scm":
            self.scm.refresh()

    def refresh_vcs(self) -> None:
        """Re-query git for both the tree markers and the source-control view (kept in step).

        Returns:
            None.
        """
        vcs.invalidate()                     # the branch may have moved since the last read
        self.workspace.refresh_git()
        self.scm.refresh()


class PatchPage(qt.QWidget):
    """A whole commit as `git show` prints it: the stat, then every hunk, tinted.

    A DiffPage cannot show this - it pairs two revisions of ONE file, and a commit usually spans
    several. A unified patch is the shape that fits, so it gets its own read-only page.
    """

    REMOVED = "#4b1818"
    ADDED   = "#123d1b"
    HUNK    = "#1e3a5f"

    def __init__(self, title:str, patch:str, path:str=None, parent:qt.QWidget=None) -> None:
        """Build the read-only patch page showing `patch` under a title header.

        Returns:
            None.
        """
        super().__init__(parent)
        self.file_path = path
        self._title = title

        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = qt.QLabel("  %s" % title)
        header.setObjectName("codePageHeader")
        layout.addWidget(header, 0)

        self.view = qt.QPlainTextEdit(readOnly=True)
        self.view.setObjectName("codeDiffView")
        self.view.setFont(qt.QFont("Consolas", 9))
        self.view.setWordWrapMode(qt.QTextOption.NoWrap)
        self.view.setStyleSheet("QPlainTextEdit QScrollBar { background: none; }")
        layout.addWidget(self.view, 1)

        self.view.setPlainText(patch or "")
        self._tint()

    def name(self) -> str:
        """The tab label of the patch page.

        Returns:
            str: the patch page's title.
        """
        return self._title

    def _tint(self) -> None:
        """Colour whole lines by their unified-diff prefix.

        Returns:
            None.
        """
        selections = []
        document = self.view.document()
        for number in range(document.blockCount()):
            block = document.findBlockByNumber(number)
            text = block.text()
            if text.startswith("@@"):
                colour = self.HUNK
            elif text.startswith("+") and not text.startswith("+++"):
                colour = self.ADDED
            elif text.startswith("-") and not text.startswith("---"):
                colour = self.REMOVED
            else:
                continue
            selection = qt.QTextEdit.ExtraSelection()
            selection.format.setBackground(qt.QColor(colour))
            selection.format.setProperty(qt.QTextFormat.FullWidthSelection, True)
            cursor = qt.QTextCursor(block)
            cursor.clearSelection()
            selection.cursor = cursor
            selections.append(selection)
        self.view.setExtraSelections(selections)


def _scope_statements(node) -> list:
    """The statements of `node`, descending through control flow.

    `if`, `try`, `for` and `with` do NOT open a scope in python: a name bound inside them belongs to
    the module or class around them. Reading only `node.body` would miss most of a file's constants,
    since so many of them sit inside a `try: import ...` or an `if` on the maya version.

    Returns:
        list: the flattened statements of `node`, descending through control flow.
    """
    found = []
    for child in getattr(node, "body", []):
        if isinstance(child, (ast.If, ast.Try, ast.With, ast.AsyncWith,
                              ast.For, ast.AsyncFor, ast.While)):
            found += _scope_statements(child)
            for branch in ("orelse", "finalbody"):
                found += _scope_statements(type("_", (), {"body": getattr(child, branch, [])}))
            for handler in getattr(child, "handlers", []):
                found += _scope_statements(handler)
        else:
            found.append(child)
    return found


def _assigned_names(node) -> list:
    """The names an assignment statement binds, tuple targets unpacked.

    Returns:
        list: the names the assignment binds, with tuple targets unpacked.
    """
    names = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    else:
        return names
    for target in targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            names += [e.id for e in target.elts if isinstance(e, ast.Name)]
    return names


class OpenEditorsPanel(qt.QWidget):
    """The tabs that are open, listed - VS Code's OPEN EDITORS.

    The tab bar shows the same set, but it elides and scrolls once a handful are open; this reads the
    whole list at a glance, with each file's folder beside it so two `__init__.py` are told apart.
    """

    editorActivated = qt.signal(int)           # tab index to bring forward
    editorClosed    = qt.signal(int)           # tab index to close
    closeOthers     = qt.signal(int)
    closeAll        = qt.signal()
    MUTED = "#6a6f75"

    def __init__(self, parent:qt.QWidget=None) -> None:
        """Build the open-editors panel: a single tree listing the open tabs.

        Returns:
            None.
        """
        super().__init__(parent)
        self.setObjectName("codeOpenEditors")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)
        # Fixed vertically, so the layout takes the height from sizeHint() below - one row per editor
        self.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)

        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tree = qt.QTreeWidget()
        # the tree asks for a default 256px and would floor the panel there; it takes what it is given
        self.tree.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Ignored)
        self.tree.setMinimumHeight(0)
        self.tree.setObjectName("codeScmTree")     # shares the custom-painted row styling
        self.tree.setHeaderHidden(True)
        self.tree.setFrameShape(qt.QFrame.NoFrame)
        self.tree.setIndentation(qt.px(4))
        self.tree.setIconSize(qt.QSize(qt.px(16), qt.px(16)))
        self.tree.setItemDelegate(SourceControlDelegate(self.tree))
        self.tree.itemClicked.connect(self._activate)
        self.tree.setContextMenuPolicy(qt.Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.viewport().installEventFilter(self)   # middle-click closes, as in the tab bar
        layout.addWidget(self.tree, 1)

    def set_editors(self, entries:list, current:int) -> None:
        """`entries` is [(index, icon, name, folder, modified, preview)] in tab order.

        Returns:
            None.
        """
        self.tree.clear()
        for index, icon, name, folder, modified, preview in entries:
            item = qt.QTreeWidgetItem(self.tree, [("● " + name) if modified else name])
            item.setIcon(0, icon)
            item.setData(0, _SUBTITLE_ROLE, folder)
            item.setData(0, _LINE_ROLE, index)
            item.setData(0, _CLOSE_ROLE, True)
            item.setToolTip(0, os.path.join(folder, name) if folder else name)
            if preview:                        # the same italic the tab bar gives a preview tab
                font = item.font(0)
                font.setItalic(True)
                item.setFont(0, font)
            if index == current:
                self.tree.setCurrentItem(item)

    def _index(self, item) -> int:
        """The tab index stored on `item`.

        Returns:
            int: the item's tab index, or -1 when it carries none.
        """
        value = item.data(0, _LINE_ROLE) if item is not None else None
        return int(value) if value is not None else -1

    CLOSE_SLOT = 16                            # must match the slot SourceControlDelegate reserves

    def _activate(self, item, column=0) -> None:
        """Bring the clicked editor's tab forward.

        Returns:
            None.
        """
        index = self._index(item)
        if index >= 0:
            self.editorActivated.emit(index)

    def eventFilter(self, watched, event) -> object:
        """Close on a middle click anywhere, or a left click inside the row's close slot.

        Returns:
            object: True when the click was consumed, otherwise the base result.
        """
        if watched is self.tree.viewport() and event.type() == qt.QEvent.MouseButtonRelease:
            index = self._index(self.tree.itemAt(event.pos()))
            if index < 0:
                return super().eventFilter(watched, event)
            if event.button() == qt.Qt.MiddleButton:
                self.editorClosed.emit(index)
                return True
            if event.button() == qt.Qt.LeftButton and event.pos().x() <= qt.px(self.CLOSE_SLOT):
                self.editorClosed.emit(index)
                return True                    # consumed, so the row is not activated as well
        return super().eventFilter(watched, event)

    def sizeHint(self) -> qt.QSize:
        """One row per open editor, capped so a long list cannot eat the whole side bar.

        The height is asked for rather than imposed: the section holding this panel carries a Fixed
        policy, so Qt reads this hint and re-reads it whenever the list changes. With nothing open it
        asks for NOTHING, and only the OPEN EDITORS header remains.

        Returns:
            qt.QSize: a size holding one row per open editor, capped at nine rows.
        """
        rows = min(self.tree.topLevelItemCount(), 9)
        return qt.QSize(super().sizeHint().width(), rows * qt.px(22) + (qt.px(6) if rows else 0))

    def _context_menu(self, pos) -> None:
        """Show the Close / Close Others / Close All menu for the clicked row.

        Returns:
            None.
        """
        index = self._index(self.tree.itemAt(pos))
        menu = qt.QMenu(self.tree)
        if index >= 0:
            menu.addAction("Close", lambda: self.editorClosed.emit(index))
            menu.addAction("Close Others", lambda: self.closeOthers.emit(index))
        menu.addAction("Close All", self.closeAll.emit)
        point = self.tree.viewport().mapToGlobal(pos)
        menu.exec_(point) if hasattr(menu, "exec_") else menu.exec(point)


class VariablesPanel(qt.QWidget):
    """The user globals of the shared run namespace - VS Code's Jupyter VARIABLES, for Maya scripting.

    It reads the same namespace the editor and the Debug Console execute in, so anything you run (a
    selection, the whole file, a console line) shows up here with its type and a short repr. Modules,
    dunders and the editor's own baseline are hidden; a double click echoes the full value to the output.
    """

    printRequested = qt.signal(str)            # a global's name, to echo in full to the output
    HIDDEN = {"__name__", "__builtins__", "__doc__", "__package__", "__loader__", "__spec__", "__file__"}

    def __init__(self, parent:qt.QWidget=None) -> None:
        """Build the variables panel: a single tree listing the namespace globals.

        Returns:
            None.
        """
        super().__init__(parent)
        self.setObjectName("codeVariables")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)
        self.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Expanding)

        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tree = qt.QTreeWidget()
        self.tree.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Expanding)
        self.tree.setMinimumHeight(0)
        self.tree.setObjectName("codeScmTree")     # shares the custom-painted row styling
        self.tree.setHeaderHidden(True)
        self.tree.setFrameShape(qt.QFrame.NoFrame)
        self.tree.setIndentation(qt.px(4))
        self.tree.setIconSize(qt.QSize(qt.px(16), qt.px(16)))
        self.tree.setItemDelegate(SourceControlDelegate(self.tree))
        self.tree.itemDoubleClicked.connect(self._print)
        layout.addWidget(self.tree, 1)

    def refresh(self, namespace:dict=None) -> None:
        """Rebuild the list from `namespace`, hiding modules, dunders and the editor's baseline.

        Args:
            namespace: (dict): - the run namespace to read.

        Returns:
            None.
        """
        import types
        self.tree.clear()
        rows = []
        for name, value in (namespace or {}).items():
            if name in self.HIDDEN or (name.startswith("__") and name.endswith("__")):
                continue
            if isinstance(value, types.ModuleType):
                continue
            rows.append((name, value))
        for name, value in sorted(rows, key=lambda row: row[0].lower()):
            try:
                short = repr(value).replace("\n", " ")
            except Exception:
                short = "<unrepresentable>"
            if len(short) > 200:
                short = short[:200] + "…"
            item = qt.QTreeWidgetItem(self.tree, [name])
            item.setData(0, _SUBTITLE_ROLE, "%s   %s" % (type(value).__name__, short))
            item.setData(0, _LINE_ROLE, name)
            item.setToolTip(0, "%s: %s = %s" % (name, type(value).__name__, short))
        self.tree.updateGeometry()
        self.updateGeometry()

    def _print(self, item, column:int=0) -> None:
        """Ask the window to echo the double-clicked global to the output.

        Returns:
            None.
        """
        name = item.data(0, _LINE_ROLE) if item is not None else None
        if name:
            self.printRequested.emit(name)


class OutlinePanel(qt.QWidget):
    """The symbols of the current file, read from its syntax tree.

    Built from `ast`, so it costs a parse and nothing else - no index, no language server. It follows
    the active tab, and re-reads on a pause in typing, which is why the parse has to stay cheap.
    """

    symbolActivated = qt.signal(int)           # 1-based line to jump to
    MUTED = "#6a6f75"

    def __init__(self, parent:qt.QWidget=None) -> None:
        """Build the outline panel: a tree of the current file's symbols.

        Returns:
            None.
        """
        super().__init__(parent)
        self.setObjectName("codeOutline")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)
        self._lines = []                       # (line, item) in source order, for caret tracking

        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tree = qt.QTreeWidget()
        self.tree.setObjectName("codeScmTree")     # shares the custom-painted row styling
        self.tree.setHeaderHidden(True)
        self.tree.setFrameShape(qt.QFrame.NoFrame)
        self.tree.setIndentation(qt.px(14))
        self.tree.setIconSize(qt.QSize(qt.px(16), qt.px(16)))
        self.tree.setItemDelegate(SourceControlDelegate(self.tree))
        self.tree.itemClicked.connect(self._activate)
        layout.addWidget(self.tree, 1)

    def set_source(self, path:str, source:str) -> None:
        """Rebuild from `source`. Anything that is not python gets an honest placeholder.

        Returns:
            None.
        """
        self.tree.clear()
        self._lines = []
        if not path:
            return self._hint("No file open.")
        if os.path.splitext(path)[1].lower() not in (".py", ".pyw"):
            return self._hint("Outline is available for python files.")
        try:
            tree = ast.parse(source or "")
        except SyntaxError as error:
            return self._hint("line %s: %s" % (error.lineno, error.msg))

        def add(parent, name, kind, line) -> object:
            """Add a symbol row under `parent` and remember its source line.

            Returns:
                object: the created tree item.
            """
            item = qt.QTreeWidgetItem(parent, [name])
            item.setIcon(0, symbol_icon(kind))
            item.setData(0, _LINE_ROLE, line)
            item.setExpanded(True)
            self._lines.append((line, item))
            return item

        def walk(node, parent, in_function=False) -> None:
            """Recurse the AST, adding a row per class, function and top-level name.

            Returns:
                None.
            """
            seen = set()
            for child in _scope_statements(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, add(parent, child.name, "def", child.lineno), True)
                elif isinstance(child, ast.ClassDef):
                    walk(child, add(parent, child.name, "class", child.lineno), False)
                elif not in_function:
                    # module- and class-level names: the constants and settings VS Code lists too.
                    # Assignments inside a FUNCTION are left out - every local of every method would
                    # bury the structure the outline exists to show.
                    for name in _assigned_names(child):
                        if name not in seen:     # `x` set in both a try and its except is one name
                            seen.add(name)
                            add(parent, name, "var", child.lineno)

        walk(tree, self.tree)
        if not self._lines:
            self._hint("No symbols in this file.")

    def _hint(self, text:str) -> None:
        """Show a single dim placeholder row carrying `text`.

        Returns:
            None.
        """
        item = qt.QTreeWidgetItem(self.tree, [text])
        item.setForeground(0, qt.QColor(self.MUTED))

    def follow(self, line:int) -> None:
        """Highlight the symbol the caret sits in - the LAST one starting at or before `line`.

        Returns:
            None.
        """
        current = None
        for start, item in self._lines:
            if start <= line:
                current = item
            else:
                break
        if current is not None and current is not self.tree.currentItem():
            self.tree.setCurrentItem(current)

    def _activate(self, item, column=0) -> None:
        """Jump to the line of the clicked symbol.

        Returns:
            None.
        """
        line = item.data(0, _LINE_ROLE)
        if line:
            self.symbolActivated.emit(int(line))


_GEAR_CACHE = []


def gear_icon() -> "qt.QIcon":
    """The Manage cog at the foot of the activity bar, drawn rather than shipped.

    The package has no cog asset, and the activity bar's other icons are PNGs of a fixed colour -
    drawing this one keeps it the same tone as the theme's dim foreground on both skins.

    Returns:
        'qt.QIcon': the cached cog icon.
    """
    if _GEAR_CACHE:
        return _GEAR_CACHE[0]
    size = qt.px(22)
    pixmap = qt.QPixmap(size, size)
    pixmap.fill(qt.Qt.transparent)
    painter = qt.QPainter(pixmap)
    painter.setRenderHint(qt.QPainter.Antialiasing, True)
    pen = qt.QPen(qt.QColor("#9d9d9d"))
    pen.setWidthF(max(1.0, size / 14.0))
    painter.setPen(pen)
    painter.setBrush(qt.Qt.NoBrush)

    centre = size / 2.0
    ring, tooth, hub = size * 0.30, size * 0.40, size * 0.13
    painter.drawEllipse(qt.QPointF(centre, centre), ring, ring)
    painter.drawEllipse(qt.QPointF(centre, centre), hub, hub)
    import math
    for step in range(8):                        # eight teeth, as a cog reads at this size
        angle = math.radians(step * 45)
        painter.drawLine(qt.QPointF(centre + ring * math.cos(angle),
                                    centre + ring * math.sin(angle)),
                         qt.QPointF(centre + tooth * math.cos(angle),
                                    centre + tooth * math.sin(angle)))
    painter.end()
    _GEAR_CACHE.append(qt.QIcon(pixmap))
    return _GEAR_CACHE[0]


_SEVERITY_CACHE = {}


def severity_glyph(kind:str, colour:str) -> "qt.QIcon":
    """The status bar's error / warning marks, drawn rather than typed.

    Written as text they were "\\u2297" and "\\u26a0", whose shape depends on the installed font -
    and on windows the warning sign is routinely substituted by a colour emoji, which is why they
    did not match VS Code. Drawn here they are monochrome and identical everywhere.

    The inner mark is CUT OUT rather than painted: a hole shows whatever surface the icon sits on,
    so one drawing works on both themes without being told the background.

    Returns:
        'qt.QIcon': the cached error/warning icon in `colour`.
    """
    key = (kind, colour)
    if key in _SEVERITY_CACHE:
        return _SEVERITY_CACHE[key]

    size = qt.px(14)
    pixmap = qt.QPixmap(size, size)
    pixmap.fill(qt.Qt.transparent)
    painter = qt.QPainter(pixmap)
    painter.setRenderHint(qt.QPainter.Antialiasing, True)
    painter.setPen(qt.Qt.NoPen)
    painter.setBrush(qt.QColor(colour))

    inset = qt.px(1)
    if kind == "warning":
        triangle = qt.QPolygonF([qt.QPointF(size / 2.0, inset),
                                 qt.QPointF(size - inset, size - inset),
                                 qt.QPointF(inset, size - inset)])
        painter.drawPolygon(triangle)
    else:
        painter.drawEllipse(qt.QRectF(inset, inset, size - 2 * inset, size - 2 * inset))

    painter.setCompositionMode(qt.QPainter.CompositionMode_Clear)
    pen = qt.QPen(qt.QColor(0, 0, 0))
    pen.setWidthF(max(1.0, size / 8.0))
    pen.setCapStyle(qt.Qt.RoundCap)
    painter.setPen(pen)
    if kind == "warning":
        painter.drawLine(qt.QPointF(size / 2.0, size * 0.38), qt.QPointF(size / 2.0, size * 0.66))
        painter.drawPoint(qt.QPointF(size / 2.0, size * 0.80))
    else:
        # 0.22, not 0.28: at 0.28 the endpoints land 0.46px from the rim, and a 1.75px round-capped
        # stroke reaches ~0.87px past them - the cross would cut through the circle's edge
        span = size * 0.22
        painter.drawLine(qt.QPointF(size / 2.0 - span, size / 2.0 - span),
                         qt.QPointF(size / 2.0 + span, size / 2.0 + span))
        painter.drawLine(qt.QPointF(size / 2.0 + span, size / 2.0 - span),
                         qt.QPointF(size / 2.0 - span, size / 2.0 + span))
    painter.end()

    _SEVERITY_CACHE[key] = qt.QIcon(pixmap)
    return _SEVERITY_CACHE[key]


_COMMIT_CACHE = []


def commit_icon() -> "qt.QIcon":
    """The little git-commit glyph: a ring on a vertical rail, as VS Code draws it.

    Returns:
        'qt.QIcon': the cached git-commit icon.
    """
    if _COMMIT_CACHE:
        return _COMMIT_CACHE[0]
    size = qt.px(16)
    pixmap = qt.QPixmap(size, size)
    pixmap.fill(qt.Qt.transparent)
    painter = qt.QPainter(pixmap)
    painter.setRenderHint(qt.QPainter.Antialiasing, True)
    pen = qt.QPen(qt.QColor("#9d9d9d"))
    pen.setWidth(max(1, qt.px(1)))
    painter.setPen(pen)
    middle, radius = size / 2.0, size / 5.0
    painter.drawLine(int(middle), 0, int(middle), int(middle - radius))
    painter.drawLine(int(middle), int(middle + radius), int(middle), size)
    painter.setBrush(qt.Qt.NoBrush)
    painter.drawEllipse(qt.QPointF(middle, middle), radius, radius)
    painter.end()
    _COMMIT_CACHE.append(qt.QIcon(pixmap))
    return _COMMIT_CACHE[0]


def _short_age(relative:str) -> str:
    """git's "2 weeks ago" as the compact "2 wks" VS Code shows on the right of a timeline row.

    Returns:
        str: the compact age string, e.g. '2 wks'.
    """
    units = {"second": "sec", "minute": "min", "hour": "hr", "day": "day",
             "week": "wk", "month": "mo", "year": "yr"}
    parts = (relative or "").split()
    if len(parts) < 2:
        return relative or ""
    count, unit = parts[0], parts[1].rstrip("s")
    short = units.get(unit, unit)
    try:
        plural = int(count) > 1
    except ValueError:
        return relative
    return "%s %s%s" % (count, short, "s" if plural and short not in ("min", "sec") else "")


class TimelinePanel(qt.QWidget):
    """The git history of the CURRENT file - what changed it, and when.

    The GRAPH view under Source Control answers "what happened in this repository"; this answers
    "what happened to this file", which is the question you have while reading it.
    """

    revisionActivated = qt.signal(str, str, str)   # path, repo root, commit sha
    commitRequested   = qt.signal(str, str, str)   # same, but the WHOLE commit rather than one diff
    MUTED = "#6a6f75"
    LIMIT = 40

    def __init__(self, parent:qt.QWidget=None) -> None:
        """Build the timeline panel: a tree of the current file's commit history.

        Returns:
            None.
        """
        super().__init__(parent)
        self.setObjectName("codeTimeline")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)
        self._path = ""

        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tree = qt.QTreeWidget()
        self.tree.setObjectName("codeScmTree")
        self.tree.setHeaderHidden(True)
        self.tree.setFrameShape(qt.QFrame.NoFrame)
        self.tree.setIndentation(qt.px(4))
        self.tree.setIconSize(qt.QSize(qt.px(16), qt.px(16)))
        self.tree.setItemDelegate(SourceControlDelegate(self.tree))
        self.tree.itemClicked.connect(self._activate)
        self.tree.setContextMenuPolicy(qt.Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.tree, 1)

    def set_file(self, path:str) -> None:
        """Load the commits that touched `path`, newest first.

        Returns:
            None.
        """
        self.tree.clear()
        self._path = path or ""
        if not path or not os.path.isfile(path):
            return self._hint("No file open.")
        root = vcs.repo_root(os.path.dirname(path))
        if not root:
            return self._hint("Not in a git repository.")
        try:
            commits = vcs.log(os.path.dirname(path), limit=self.LIMIT, path=path)
        except Exception:
            commits = []
        if not commits:
            return self._hint("No history for this file.")

        for commit in commits:
            item = qt.QTreeWidgetItem(self.tree, [commit["subject"] or commit["short"]])
            item.setIcon(0, commit_icon())
            # the age is pinned to the right edge, as VS Code does it; the author belongs in the
            # tooltip - inline it just squeezes the subject that you actually read
            item.setData(0, _TRAIL_ROLE, _short_age(commit["relative"]))
            item.setData(0, _SUBTITLE_ROLE, commit["subject"])   # kept for Copy Commit Message
            item.setData(0, _PATH_ROLE, commit["hash"])
            item.setData(0, _ROOT_ROLE, root)
            item.setToolTip(0, self._describe(commit))

    @staticmethod
    def _describe(commit:dict) -> str:
        """The hover card, as rich text.

        Plain text in a Qt tooltip is never wrapped: a commit body becomes one line running off the
        screen, which is exactly what made it unreadable. Any markup switches the tooltip to rich
        text, where it word-wraps - so the card is built as html, with the width set explicitly.

        Returns:
            str: the HTML hover card for the commit.
        """
        def escape(value) -> object:
            """HTML-escape `value`.

            Returns:
                object: the HTML-escaped value.
            """
            return (value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        parts = ["<div style='width:420px'>",
                 "<b>%s</b> &nbsp;<span style='color:#8a8a8a'>%s &nbsp;&middot;&nbsp; %s (%s)</span>"
                 % (escape(commit["author"]), escape(commit["short"]),
                    escape(commit["date"]), escape(commit["relative"])),
                 "<br><br>%s" % escape(commit["subject"])]
        if commit.get("body"):
            parts.append("<br><br><span style='color:#c8c8c8'>%s</span>"
                         % escape(commit["body"]).replace("\n", "<br>"))
        parts.append("</div>")
        return "".join(parts)

    def _context_menu(self, pos) -> None:
        """Right-click a revision: see it, compare it, copy it, open it on GitHub.

        Returns:
            None.
        """
        item = self.tree.itemAt(pos)
        sha = item.data(0, _PATH_ROLE) if item is not None else None
        if not sha:
            return
        root = item.data(0, _ROOT_ROLE) or ""
        menu = qt.QMenu(self.tree)
        menu.addAction("Open Changes", lambda: self.revisionActivated.emit(self._path, root, sha))
        menu.addAction("Open Commit", lambda: self.commitRequested.emit(self._path, root, sha))
        menu.addSeparator()
        menu.addAction("Copy Commit ID", lambda: compat.copy(sha))
        menu.addAction("Copy Commit Message",
                       lambda: compat.copy(item.data(0, _SUBTITLE_ROLE) or item.text(0)))
        url = vcs.commit_url(root, sha)
        if url:                                  # only for a host whose url layout we actually know
            menu.addSeparator()
            menu.addAction("Open on %s" % ("GitHub" if "github" in url else "Remote"),
                           lambda: qt.QDesktopServices.openUrl(qt.QtCore.QUrl(url)))
        point = self.tree.viewport().mapToGlobal(pos)
        menu.exec_(point) if hasattr(menu, "exec_") else menu.exec(point)

    def _hint(self, text:str) -> None:
        """Show a single dim placeholder row carrying `text`.

        Returns:
            None.
        """
        item = qt.QTreeWidgetItem(self.tree, [text])
        item.setForeground(0, qt.QColor(self.MUTED))

    def _activate(self, item, column=0) -> None:
        """Open the diff for the clicked revision.

        Returns:
            None.
        """
        sha = item.data(0, _PATH_ROLE)
        if sha:
            self.revisionActivated.emit(self._path, item.data(0, _ROOT_ROLE) or "", sha)



# what the status bar calls a file, by extension. Anything unlisted falls back to the extension
# itself, which is more honest than guessing at a name for it.
_LANGUAGES = {".py": "Python", ".pyw": "Python", ".mel": "MEL", ".md": "Markdown",
              ".json": "JSON", ".ps1": "PowerShell", ".cpp": "C++", ".h": "C++",
              ".c": "C", ".xml": "XML", ".yaml": "YAML", ".yml": "YAML",
              ".ini": "INI", ".txt": "Plain Text", ".vcxproj": "XML"}


class HelpRowDelegate(qt.QStyledItemDelegate):
    """Row band for the Quick Help table, painted the way the Explorer paints its rows: a faint
    fill with a thin accent outline, instead of the solid blue bar a stylesheet gives.

    Drawn per CELL, because Qt clips each one to its own rect - a band started in the first column
    would stop at that column's edge. Adjacent fills join into a single band, and the outline is
    reassembled from the pieces: top and bottom in every cell, the left edge only on the first
    column and the right edge only on the last.
    """

    def paint(self, painter, option, index) -> None:
        """Paint one Quick Help cell's row band and its reassembled outline.

        Returns:
            None.
        """
        selected = bool(option.state & qt.QStyle.State_Selected)
        hovered = bool(option.state & qt.QStyle.State_MouseOver)
        rect = option.rect

        if selected:
            painter.fillRect(rect, qt.QColor(255, 255, 255, 28))
        elif hovered:
            painter.fillRect(rect, qt.QColor(255, 255, 255, 14))

        if selected:
            painter.save()
            painter.setPen(qt.QPen(qt.QColor(qt.theme()["selection"]), 1))
            painter.drawLine(rect.left(), rect.top(), rect.right(), rect.top())
            painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
            if index.column() == 0:
                painter.drawLine(rect.left(), rect.top(), rect.left(), rect.bottom())
            if index.column() == index.model().columnCount() - 1:
                painter.drawLine(rect.right(), rect.top(), rect.right(), rect.bottom())
            painter.restore()

        # the base class draws the text - with the highlight state removed, or it would paint its
        # own solid background straight over the band we just drew
        plain = qt.QStyleOptionViewItem(option)
        plain.state &= ~qt.QStyle.State_Selected
        plain.state &= ~qt.QStyle.State_MouseOver
        super().paint(painter, plain, index)


class BusySpinner(qt.QWidget):
    """A small rotating arc, drawn rather than loaded, like the rest of this package's icons.

    Only useful if the work it covers lets the event loop breathe - a spinner on a blocked main
    thread is a still image. See Editor.run_steps for how the work is broken up so it can turn.
    """

    def __init__(self, parent:qt.QWidget=None) -> None:
        """Set up the hidden spinner widget and its rotation timer.

        Returns:
            None.
        """
        super().__init__(parent)
        self.setObjectName("codeSpinner")
        self._angle = 0
        self._timer = qt.QTimer(self)
        self._timer.setInterval(70)
        self._timer.timeout.connect(self._step)
        self.setFixedSize(qt.px(13), qt.px(13))
        self.hide()

    def start(self) -> None:
        """Show the spinner and start its rotation timer.

        Returns:
            None.
        """
        self.show()
        self._timer.start()

    def stop(self) -> None:
        """Stop the rotation timer and hide the spinner.

        Returns:
            None.
        """
        self._timer.stop()
        self.hide()

    def _step(self) -> None:
        """Advance the rotation angle by one step and repaint.

        Returns:
            None.
        """
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, event) -> None:
        """Draw the rotating three-quarter arc.

        Returns:
            None.
        """
        painter = qt.QPainter(self)
        painter.setRenderHint(qt.QPainter.Antialiasing, True)
        pen = qt.QPen(qt.QColor(qt.theme()["accent_on"]), max(1, qt.px(2)))
        pen.setCapStyle(qt.Qt.RoundCap)
        painter.setPen(pen)
        inset = qt.px(2)
        box = qt.QRectF(inset, inset, self.width() - inset * 2, self.height() - inset * 2)
        # a three-quarter arc, so the gap makes the rotation readable
        painter.drawArc(box, -self._angle * 16, 270 * 16)
        painter.end()


class StatusBar(qt.QWidget):
    """The strip along the bottom: branch, problem tally, and what the current file is.

    Every field is a button rather than a label. In VS Code they act - the caret position opens Go to
    Line, the tally opens Problems - and a strip of dead text at the bottom of the window would be
    pure decoration taking a row of height.
    """

    problemsClicked = qt.signal()
    lineClicked     = qt.signal()
    branchClicked   = qt.signal()

    GLYPH = "#9d9d9d"                          # status-bar foreground: the marks are monochrome here,
                                               # exactly as VS Code renders them in this strip

    def __init__(self, parent:qt.QWidget=None) -> None:
        """Build the status bar: branch, problem tallies and the file-info fields.

        Returns:
            None.
        """
        super().__init__(parent)
        self.setObjectName("codeStatus")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)

        row = qt.QHBoxLayout(self)
        row.setContentsMargins(qt.px(6), 0, qt.px(6), 0)
        row.setSpacing(qt.px(2))

        self.branch = self._field("", "Current branch", self.branchClicked)
        # two buttons rather than one string: each carries its own drawn glyph, the way VS Code
        # pairs an icon with its count
        self.errors = self._field("0", "Errors in the open files", self.problemsClicked)
        self.warnings = self._field("0", "Warnings in the open files", self.problemsClicked)
        for button, kind in ((self.errors, "error"), (self.warnings, "warning")):
            button.setToolButtonStyle(qt.Qt.ToolButtonTextBesideIcon)
            button.setIconSize(qt.QSize(qt.px(13), qt.px(13)))
            button.setIcon(severity_glyph(kind, self.GLYPH))
        # transient notices (why F12 found nothing, say) sit left of the stretch, as in VS Code
        self.message = self._field("", "")
        self.message.hide()
        self._message_timer = qt.QTimer(self)
        self._message_timer.setSingleShot(True)
        self._message_timer.timeout.connect(self.message.hide)

        self.spinner = BusySpinner()

        row.addWidget(self.branch, 0)
        row.addWidget(self.errors, 0)
        row.addWidget(self.warnings, 0)
        row.addWidget(self.spinner, 0)
        row.addWidget(self.message, 0)
        row.addStretch(1)

        self.position = self._field("Ln 1, Col 1", "Go to Line/Column", self.lineClicked)
        self.indent = self._field("Spaces: 4", "Indentation")
        self.encoding = self._field("UTF-8", "File encoding")
        self.eol = self._field("LF", "End of line sequence")
        self.language = self._field("", "Language of the current file")
        self.runtime = self._field("", "Python running this editor")
        for widget in (self.position, self.indent, self.encoding, self.eol,
                       self.language, self.runtime):
            row.addWidget(widget, 0)

        self.runtime.setText("Python %s" % sys.version.split()[0])
        self.set_file(None, "")

    def minimumSizeHint(self) -> qt.QSize:
        """Never gate the window's width - see Breadcrumbs.minimumSizeHint.

        Ten fields side by side (branch, tallies, position, indentation, encoding, EOL, language,
        runtime) add up to several hundred pixels of minimum. The fields on the right clip away
        instead; the ones that matter sit on the left.

        Returns:
            qt.QSize: a size with zero width and the layout's own minimum height.
        """
        return qt.QSize(0, super().minimumSizeHint().height())

    def set_busy(self, busy:bool, text:str="") -> None:
        """Spin, with a word for what is going on. Stopping clears the word too.

        Returns:
            None.
        """
        self.spinner.start() if busy else self.spinner.stop()
        self.message.setText(text or "")
        self.message.setVisible(bool(text))
        if not busy:
            self._message_timer.stop()

    def set_message(self, text:str="", seconds:int=5) -> None:
        """Show a transient notice in the status bar, then let it fade out of the way.

        Returns:
            None.
        """
        self.message.setText(text or "")
        self.message.setVisible(bool(text))
        self._message_timer.stop()
        if text:
            self._message_timer.start(max(1, seconds) * 1000)

    def _field(self, text:str, tip:str, signal=None) -> qt.QToolButton:
        """Build a status-bar field button, made clickable when `signal` is given.

        Returns:
            qt.QToolButton: the configured status-bar field button.
        """
        button = qt.QToolButton()
        button.setObjectName("codeStatusField")
        button.setText(text)
        button.setToolTip(tip)
        button.setAutoRaise(True)
        button.setFocusPolicy(qt.Qt.NoFocus)
        if signal is not None:
            button.clicked.connect(signal)
        else:
            button.setEnabled(False)          # informative only: no hover, no click
        return button

    # ---- the parts that follow the current file

    def set_file(self, path:str, source:str) -> None:
        """Refresh what depends on the file's CONTENT. Cheap: no process is spawned here.

        The branch lives in set_branch instead, because it depends on the folder rather than the
        text - and this runs again on every pause in typing.

        Returns:
            None.
        """
        name = os.path.splitext(path or "")[1].lower()
        self.language.setText(_LANGUAGES.get(name, name.lstrip(".").upper() or "Plain Text"))
        # CRLF or LF is read from the CONTENT, not from the platform: a file written on windows and
        # opened on linux keeps its endings, and saying otherwise would be a lie about the file
        self.eol.setText("CRLF" if "\r\n" in (source or "") else "LF")
        self.indent.setText("Tab Size: 4" if "\n\t" in (source or "") else "Spaces: 4")

    def set_branch(self, folder:str) -> None:
        """Read the branch for `folder`. Cached in vcs, so asking again costs nothing.

        Separate from set_file because it depends on the FOLDER, not on the text - and set_file runs
        again on every pause in typing, where two process spawns are not affordable.

        Returns:
            None.
        """
        try:
            branch = vcs.branch_cached(vcs.repo_root_cached(folder)) if folder else ""
        except Exception:
            branch = ""
        self.branch.setText(u"⑂ %s" % branch if branch else "")
        self.branch.setVisible(bool(branch))

    def set_position(self, line:int, column:int) -> None:
        """Update the line and column field.

        Returns:
            None.
        """
        self.position.setText("Ln %d, Col %d" % (line, column))

    def set_tally(self, errors:int, warnings:int) -> None:
        """Update the error and warning count fields.

        Returns:
            None.
        """
        self.errors.setText(str(errors))
        self.warnings.setText(str(warnings))


class SecondaryPanel(qt.QWidget):
    """Right-hand column, VS Code's secondary side bar: QUICK HELP for the call you are inside.

    Maya's own Quick Help only knows `cmds`. This takes whichever source is richer - a python
    signature with annotations and defaults for your own functions, `cmds.help` flags for a maya
    command - and marks the argument the caret is currently on.
    """

    argumentPicked = qt.signal(str)        # a row was double-clicked: insert it into the call

    def __init__(self, parent:qt.QWidget=None) -> None:
        """Build the Quick Help panel: the title, the parameter table and the doc view.

        Returns:
            None.
        """
        super().__init__(parent)
        self.setObjectName("codeSecondary")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)
        self._showing = None                   # (name, argument) currently drawn

        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = qt.QLabel("  QUICK HELP")
        self.header.setObjectName("codeSectionLabel")
        layout.addWidget(self.header, 0)

        self.title = qt.QLabel()
        self.title.setObjectName("codeHelpTitle")
        self.title.setWordWrap(True)
        layout.addWidget(self.title, 0)

        # a real table, the way the cmds documentation lists flags: name, type, and the short form
        self.tree = qt.QTreeWidget()
        self.tree.setObjectName("codeHelpTable")
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setMouseTracking(True)                    # so hover reaches the delegate
        self.tree.setItemDelegate(HelpRowDelegate(self.tree))
        self.tree.setFrameShape(qt.QFrame.NoFrame)
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Name", "Type", ""])
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, qt.QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, qt.QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, qt.QHeaderView.ResizeToContents)
        # a type column sized to its contents ("Length Length Length") would otherwise set a floor
        # the panel could not be dragged below
        self.tree.header().setMinimumSectionSize(qt.px(24))
        self.tree.itemDoubleClicked.connect(self._picked)
        self.tree.currentItemChanged.connect(self._row_changed)
        layout.addWidget(self.tree, 1)

        # the TABLE owns the height; the prose underneath is capped and scrolls inside itself, so a
        # long docstring cannot push the list it belongs to off the panel
        self.view = qt.QTextBrowser()
        self.view.setObjectName("codeQuickHelp")
        self.view.setFrameShape(qt.QFrame.NoFrame)
        self.view.setOpenExternalLinks(False)
        self.view.setMaximumHeight(qt.px(150))
        layout.addWidget(self.view, 0)
        self.clear()

    def clear(self) -> None:
        """Empty the panel. Nothing is written in its place - an idle panel should be quiet.

        Returns:
            None.
        """
        self._showing = None
        self.title.setText("")
        self.title.setVisible(False)
        self.tree.clear()
        self._doc = ""
        self.view.setHtml("")

    @staticmethod
    def _syntax(role:str, fallback:str) -> str:
        """A colour from the python highlighter, so the title reads like the code it describes.

        Returns:
            str: the highlighter colour for `role`, or `fallback` on failure.
        """
        try:
            return editor_module.python_syntax_styles(role).foreground().color().name()
        except Exception:
            return fallback

    def _title_html(self, name:str, returns:str) -> str:
        """`cmds.circle() -> string[]`, coloured the way the editor would colour it.

        Returns:
            str: the coloured HTML for the call signature.
        """
        prefix, _, call = name.rpartition(".")
        palette = qt.theme()
        method = self._syntax("method", "#dcdcaa")
        kind = self._syntax("class", "#4ec9b0")
        operator = self._syntax("operator", "#cccccc")
        bracket = editor_module.PythonHighlighter.BRACKET_COLOURS[0]

        html = ""
        if prefix:
            html += "<span style='color:%s'>%s.</span>" % (palette["text"], self._escape(prefix))
        html += "<span style='color:%s'>%s</span>" % (method, self._escape(call))
        html += "<span style='color:%s'>()</span>" % bracket
        if returns:
            html += "<span style='color:%s'> -&gt; </span>" % operator
            html += "<span style='color:%s'>%s</span>" % (kind, self._escape(returns))
        return html

    def _picked(self, item, column=0) -> None:
        """Double-click: hand the name back so the editor can type it into the call.

        Returns:
            None.
        """
        if item is not None and item.data(0, _PATH_ROLE):
            self.argumentPicked.emit(item.data(0, _PATH_ROLE))

    def _row_changed(self, current, _previous=None) -> None:
        """Show the selected argument's own description, falling back to the whole docstring.

        Returns:
            None.
        """
        note = current.data(0, _SUBTITLE_ROLE) if current is not None else ""
        self._render_doc(note or self._doc)

    def _render_doc(self, text:str) -> None:
        """Render `text` as the dim, word-wrapped documentation body.

        Returns:
            None.
        """
        palette = qt.theme()
        body = self._escape((text or "").strip()[:1400])
        self.view.setHtml("<div style='color:%s;font-size:11px;padding:6px 8px;"
                          "white-space:pre-wrap'>%s</div>" % (palette["dim"], body))

    def set_call(self, name:str, info:dict, argument:int, palette:dict) -> None:
        """Show `info` for the call `name`, with parameter number `argument` marked.

        Returns:
            None.
        """
        if not info:
            self.clear()
            return
        key = (name, argument, len(info.get("params", [])))
        if self._showing == key:
            return                              # same call, same argument: leave the table alone
        self._showing = key

        self.title.setText(self._title_html(name, info.get("returns", "")))
        self.title.setVisible(True)
        self._doc = info.get("doc") or ""
        self.tree.clear()
        bold = qt.QFont(self.tree.font())
        bold.setBold(True)

        for index, row in enumerate(info.get("params", [])):
            # padded rather than unpacked: this runs on every caret move, and a row of the wrong
            # length - a half-reloaded package, say - would raise on each keystroke instead of just
            # showing one blank column
            label, kind, extra, description = (list(row) + ["", "", "", ""])[:4]
            shown = label + ("=" + extra if extra and info.get("kind") == "python" else "")
            item = qt.QTreeWidgetItem(self.tree, [shown, kind or "",
                                                  "" if info.get("kind") == "python" else extra])
            item.setData(0, _PATH_ROLE, label.lstrip("*"))     # what a double-click inserts
            item.setData(0, _SUBTITLE_ROLE, description or "")
            if description:
                item.setToolTip(0, description)
            item.setForeground(1, qt.QColor(palette["dim"]))
            item.setForeground(2, qt.QColor(palette["muted"]))
            if index == argument and info.get("kind") == "python":
                item.setFont(0, bold)                          # the argument being typed
                item.setForeground(0, qt.QColor(palette["bright"]))
                self.tree.setCurrentItem(item)
        self._render_doc(self._doc)

    @staticmethod
    def _escape(text:str) -> str:
        """HTML-escape `text`.

        Returns:
            str: the HTML-escaped text.
        """
        return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class ProblemsPanel(qt.QWidget):
    """Problems found in the open tabs, grouped per file like VS Code's PROBLEMS view.

    Each file is a node carrying its problem count in a bubble; each child is one problem with a
    severity icon, its message and the line it sits on. Clicking a child jumps there.
    """

    problemActivated = qt.signal(str, int)      # path, 1-based line
    countChanged     = qt.signal(int)           # drives the count shown on the panel tab
    tallyChanged     = qt.signal(int, int)      # (errors, warnings) for the status bar
    MUTED            = "#6a6f75"                # the "no problems" placeholder

    def __init__(self, tabs_provider, parent:qt.QWidget=None) -> None:
        """Build the problems panel: a tree grouping problems per file.

        `tabs_provider` returns the list of (path, source) pairs to check.

        Returns:
            None.
        """
        super().__init__(parent)
        self._tabs = tabs_provider
        self.setObjectName("codeProblems")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)

        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tree = qt.QTreeWidget()
        self.tree.setObjectName("codeScmTree")     # shares the custom-painted styling
        self.tree.setHeaderHidden(True)
        self.tree.setFrameShape(qt.QFrame.NoFrame)
        self.tree.setIndentation(qt.px(14))
        self.tree.setIconSize(qt.QSize(qt.px(16), qt.px(16)))
        self.tree.setItemDelegate(SourceControlDelegate(self.tree))
        self.tree.itemClicked.connect(self._activate)
        self.tree.setContextMenuPolicy(qt.Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.tree, 1)

        # safe to bind here: Ctrl+C is in the window's NATIVE_KEYS, so it installs no shortcut of its
        # own and leaves the key to whichever widget holds focus
        copy = qt.QShortcut(qt.QKeySequence.Copy, self.tree)
        copy.setContext(qt.Qt.WidgetWithChildrenShortcut)
        copy.activated.connect(self._copy_selected)

    def _copy_selected(self) -> None:
        """Ctrl+C in the list: the selected problem, in the same one-line form as Copy.

        Returns:
            None.
        """
        item = self.tree.currentItem()
        if item is not None:
            self._copy(item.data(0, _DETAIL_ROLE) or item.text(0))

    def _context_menu(self, position) -> None:
        """VS Code's PROBLEMS right-click menu, minus what only an extension could offer.

        Its Fix / Explain / "suppress this warning" entries belong to Pylance and Copilot, which are
        not here - offering them would open a dialog that could not do anything. What is left is what
        actually works: go there, and copy.

        Returns:
            None.
        """
        item = self.tree.itemAt(position)
        menu = qt.QMenu(self.tree)
        detail = item.data(0, _DETAIL_ROLE) if item is not None else None

        if detail:                                 # a problem row
            menu.addAction("Go to Problem", lambda: self._activate(item))
            menu.addSeparator()
            menu.addAction("Copy\tCtrl+C", lambda: self._copy(detail))
            menu.addAction("Copy Message", lambda: self._copy(item.text(0)))
        elif item is not None and item.childCount():       # a file header
            folder = item.data(0, _SUBTITLE_ROLE) or ""
            full = os.path.join(folder, item.text(0)) if folder else item.text(0)
            menu.addAction("Open File", lambda: self.problemActivated.emit(full, 1))
            menu.addAction("Copy Path", lambda: self._copy(full))
            menu.addSeparator()
            menu.addAction("Copy All Problems in File",
                           lambda: self._copy("\n".join(
                               item.child(row).data(0, _DETAIL_ROLE) or ""
                               for row in range(item.childCount()))))
        if not menu.isEmpty():
            menu.addSeparator()
        menu.addAction("Expand All", self.tree.expandAll)
        menu.addAction("Collapse All", self.tree.collapseAll)
        menu.addAction("Copy All", lambda: self._copy(self._all_details()))

        point = self.tree.viewport().mapToGlobal(position)
        menu.exec_(point) if hasattr(menu, "exec_") else menu.exec(point)

    def _all_details(self) -> str:
        """Every problem currently listed, one per line.

        Returns:
            str: every listed problem, one per line.
        """
        lines = []
        for row in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(row)
            for child_row in range(parent.childCount()):
                detail = parent.child(child_row).data(0, _DETAIL_ROLE)
                if detail:
                    lines.append(detail)
        return "\n".join(lines)

    def _copy(self, text:str) -> None:
        """Put `text` on the clipboard.

        Returns:
            None.
        """
        if text:
            qt.QApplication.clipboard().setText(text)

    def refresh(self) -> int:
        """Re-analyse every open python tab and rebuild the tree. Returns the problem count.

        Returns:
            int: the total number of problems found.
        """
        self.tree.clear()
        found = errors = 0
        for path, source in (self._tabs() or []):
            if os.path.splitext(path or "")[1].lower() not in (".py", ".pyw"):
                continue                       # the checker only understands python
            problems = lint.check(path, source)
            if not problems:
                continue

            parent = qt.QTreeWidgetItem(self.tree, [os.path.basename(path or "untitled")])
            parent.setIcon(0, file_icon(path or "untitled.py"))
            parent.setData(0, _SUBTITLE_ROLE, os.path.dirname(path or ""))
            parent.setData(0, _COUNT_ROLE, len(problems))
            parent.setExpanded(True)

            for problem in problems:
                child = qt.QTreeWidgetItem(parent, [problem["message"]])
                child.setIcon(0, _icon("severity_%s.png" % problem["severity"]))
                child.setData(0, _SUBTITLE_ROLE, "line %d" % problem["line"])
                child.setData(0, _PATH_ROLE, path)
                child.setData(0, _LINE_ROLE, problem["line"])
                child.setToolTip(0, "%s\nline %d  •  %s" % (path or "", problem["line"],
                                                            problem["code"]))
                # what Copy puts on the clipboard: the same shape a compiler reports, so it can be
                # pasted straight into a traceback search or a bug report
                child.setData(0, _DETAIL_ROLE, "%s:%d:%d  %s: %s (%s)"
                              % (path or "untitled", problem["line"], problem["column"],
                                 problem["severity"], problem["message"], problem["code"]))
                found += 1
                errors += problem["severity"] == "error"

        if not found:
            empty = qt.QTreeWidgetItem(self.tree, ["No problems have been detected."])
            empty.setForeground(0, qt.QColor(self.MUTED))
        self.countChanged.emit(found)
        self.tallyChanged.emit(errors, found - errors)
        return found

    def _activate(self, item, column=0) -> None:
        """Jump to the clicked problem, or fold a file header.

        Returns:
            None.
        """
        path = item.data(0, _PATH_ROLE)
        if path:
            self.problemActivated.emit(path, int(item.data(0, _LINE_ROLE) or 1))
        else:
            item.setExpanded(not item.isExpanded())


class DebugConsole(qt.QWidget):
    """Interactive python prompt bound to the window's execution namespace.

    Run a script from a tab, then inspect or tweak its variables here: both share one namespace, which
    is what makes this useful inside Maya.
    """

    executed = qt.signal()                     # a prompt line ran: the namespace may have changed

    def __init__(self, namespace:dict, parent:qt.QWidget=None) -> None:
        """Build the debug console: a read-only output view over an input prompt.

        Returns:
            None.
        """
        super().__init__(parent)
        self.namespace = namespace
        self._history = []
        self._cursor = 0

        self.setObjectName("codeDebugConsole")
        self.setAttribute(qt.Qt.WA_StyledBackground, True)

        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.view = qt.QTextEdit(readOnly=True)
        self.view.setFont(qt.QFont("Consolas", 9))
        self.view.setObjectName("codeDebugView")
        layout.addWidget(self.view, 1)

        self.prompt = qt.QLineEdit()
        self.prompt.setFont(qt.QFont("Consolas", 9))
        self.prompt.setPlaceholderText(">>>")
        self.prompt.setObjectName("codePrompt")
        self.prompt.returnPressed.connect(self._run)
        self.prompt.installEventFilter(self)
        layout.addWidget(self.prompt, 0)

    def eventFilter(self, watched, event) -> object:
        """Up/Down walk the command history, like a real prompt.

        Returns:
            object: True when an Up/Down history key was handled, else the base result.
        """
        if watched is self.prompt and event.type() == qt.QEvent.KeyPress and self._history:
            key = event.key()
            if key == qt.Qt.Key_Up:
                self._cursor = max(0, self._cursor - 1)
                self.prompt.setText(self._history[self._cursor])
                return True
            if key == qt.Qt.Key_Down:
                self._cursor = min(len(self._history), self._cursor + 1)
                self.prompt.setText("" if self._cursor >= len(self._history)
                                    else self._history[self._cursor])
                return True
        return super().eventFilter(watched, event)

    def _append(self, text:str, colour:str) -> None:
        """Append `text` to the output view in `colour`.

        Returns:
            None.
        """
        cursor = self.view.textCursor()
        cursor.movePosition(qt.QTextCursor.End)
        char_format = qt.QTextCharFormat()
        char_format.setForeground(qt.QColor(colour))
        cursor.insertText(text + "\n", char_format)
        self.view.setTextCursor(cursor)

    def _run(self) -> None:
        """Evaluate the line as an expression, falling back to a statement.

        Returns:
            None.
        """
        import io
        import contextlib
        import traceback

        source = self.prompt.text()
        if not source.strip():
            return
        self.prompt.clear()
        self._history.append(source)
        self._cursor = len(self._history)
        self._append(">>> " + source, "#6a9955")

        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                try:
                    result = eval(source, self.namespace)          # expression: show its value
                    if result is not None:
                        print(repr(result))
                except SyntaxError:
                    exec(source, self.namespace)                   # statement: just run it
        except Exception:
            self._append(traceback.format_exc().rstrip(), "#c74e39")
        printed = buffer.getvalue().rstrip()
        if printed:
            self._append(printed, "#cccccc")
        self.executed.emit()


# -------------------------------------------------------------------------------------------- window

class Editor(MayaQWidgetDockableMixin, qt.QWidget):
    """Standalone VS-Code-style code editor window."""

    window_instance = None
    title           = "Code Editor"
    version         = "0.2.2"

    sessionStored = qt.signal(object)      # the layout that was just written, for embedders to mirror

    def __init__(self, parent:qt.QWidget=None, chrome:bool=True, session_name:str="standalone", session_folder:str=None, session_enabled:bool=True, **kwargs) -> None:
        """Build the editor.

        Args:
            parent:         (QWidget): - parent widget.
            chrome:         (bool):    - False strips the menu bar, activity bar and side bars, leaving
                                         only the tabs over the panel. That is the shape the host
                                         embeds: same editor, no file browser of its own.
            session_name:   (str):     - name of the state file, so embedded editors do not collide.
            session_folder: (str):     - where tabs and backups are stored. None = per-user.
            session_enabled:(bool):    - False remembers nothing until a store is set later. An
                                         embedded editor starts this way: it has no project yet, and
                                         must not restore (nor write) the per-user state meanwhile.

        Returns:
            None.
        """
        super().__init__(parent=parent)

        self.chrome = chrome
        # the embedded editor must NOT answer to the standalone's name: dock.delete_workspace_instances
        # resolves "Code Editor" through MQtUtil.findControl, which matches ANY widget with that object
        # name - so opening the standalone window used to close and destroy the one inside the host.
        self.setObjectName(Editor.title if chrome else "CodeEditorPanel")
        self.setWindowTitle(Editor.title)
        if chrome:
            Editor.window_instance = self       # only the standalone window is the singleton
        # lock_theme pins the palette (the host embeds the editor at a fixed theme and a saved session
        # must not override it); when set it is also the starting theme.
        self.locked_theme = kwargs.get("lock_theme")
        self.theme_name = self.locked_theme or kwargs.get("theme") or "vscode"
        self.theme = qt.theme(self.theme_name)
        self.setStyleSheet(qt.stylesheet(self.theme_name))

        # this window's own execution namespace (isolated from the host console)
        self.namespace = {"__name__": "__main__", "__builtins__": builtins}

        main_layout = qt.QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # no the host title header — this is a standalone interface. Just the menu bar on top.
        self.menu_bar = qt.QMenuBar()
        # keep the menu bar at its natural height, never stretching down over the window
        self.menu_bar.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
        main_layout.addWidget(self.menu_bar, 0)

        # left = sidebar, right = OUTPUT console (top) over the script tabs (bottom)
        self.h_splitter = qt.QSplitter(qt.Qt.Horizontal)
        self.h_splitter.setChildrenCollapsible(False)

        self.sidebar = Sidebar()
        # single click previews (italic, reused tab), double click keeps the file open for good
        self.sidebar.fileActivated.connect(lambda p: self.open_file(p, preview=True))
        self.sidebar.filePinned.connect(lambda p: self.open_file(p, preview=False))
        self.sidebar.resultActivated.connect(self._goto_error)   # search hit: open the file at that line
        self.sidebar.diffRequested.connect(self.open_diff)
        # a lambda, not self.tabs.setCurrentIndex: the sidebar is wired BEFORE the tab widget is
        # built, so binding the method here would look it up on an Editor that has no `tabs` yet
        self.sidebar.editorActivated.connect(self._activate_open_editor)
        self.sidebar.editorClosed.connect(self._close_open_editor)
        self.sidebar.closeOthers.connect(self._close_other_open_editors)
        self.sidebar.closeAllEditors.connect(self.close_all_tabs)
        self.sidebar.symbolActivated.connect(self._goto_line)
        self.sidebar.variablePrint.connect(self._print_variable)
        self.sidebar.revisionActivated.connect(self.open_revision)
        self.sidebar.commitRequested.connect(self.open_commit)
        self.sidebar.runRequested.connect(self.run_path)
        self.sidebar.revealRequested.connect(self.reveal_path)
        self.sidebar.pathRenamed.connect(self._path_renamed)
        self.sidebar.pathDeleted.connect(self._path_deleted)
        self.h_splitter.addWidget(self.sidebar)

        right = qt.QWidget()
        right_layout = qt.QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.v_splitter = qt.QSplitter(qt.Qt.Vertical)
        self.v_splitter.setChildrenCollapsible(False)

        # script tabs (or a welcome page when nothing is open) ON TOP, like VS Code
        self.stack = qt.QStackedWidget()
        self.stack.setObjectName("codeStack")
        self.welcome = qt.QLabel("Open a file from the Workspace, or use  File ▸ New / Open.")
        self.welcome.setAlignment(qt.Qt.AlignCenter)
        self.welcome.setObjectName("codeWelcome")
        self._preview_page = None                    # the single tab shown in italic, if any
        self._pinned = set()                         # pages kept left, without a close button
        self.recent = []                             # recently opened paths, newest first
        self.autosave = False                        # File > Auto Save
        self.word_wrap = False                       # View > Appearance > Word Wrap
        self.minimap_on = True                       # the strip is on by default
        self.sticky_on = True                        # View > Appearance > Sticky Scroll
        self.trim_on_save = False                    # Selection > Transform > ... on Save
        self._history = []                           # (path, line) jump history for Back / Forward
        self._history_at = -1
        self._last_edit = None                       # where the last edit happened
        self._closed = []                            # (path, line) of closed tabs, for Reopen Closed
        self._page_cache = {}                        # norm-path -> (EditorPage, mtime): a closed tab is kept
        self._page_cache_order = []                  # alive (hidden) so its native undo/redo survives a
        #                                              close+reopen, VS Code style; bounded LRU, see _cache_page
        # early: the tab callbacks built below already record into it
        self._session = session.Session(session_name, session_folder, session_enabled)
        self._panel_sizes = {}                       # widget -> the extent it was last dragged to
        self._mru = []                               # open pages, most recently looked at first

        # problems follow the code: re-analysed on tab switch, and shortly after you stop typing
        self._problems_timer = qt.QTimer(self)
        self._problems_timer.setSingleShot(True)
        self._problems_timer.setInterval(600)
        self._problems_timer.timeout.connect(lambda: self.problems.refresh())
        # the outline re-parses on the same pause as the problems: both cost one ast.parse
        self._problems_timer.timeout.connect(self._refresh_side_views)

        # editor groups: one QTabWidget per split, laid out in a horizontal splitter. `self.tabs` is a
        # property pointing at the ACTIVE group, so every single-group operation keeps working unchanged;
        # only the enumerate-all sites go through _all_pages(). Split Right adds a second group.
        self._groups = []
        self.editor_split = qt.QSplitter(qt.Qt.Horizontal)
        self.editor_split.setChildrenCollapsible(False)
        self._active_group = self._make_group()
        application = qt.QApplication.instance()
        if application is not None:
            application.focusChanged.connect(self._on_focus_changed)   # clicking a pane's body activates it

        self.stack.addWidget(self.welcome)          # index 0
        self.stack.addWidget(self.editor_split)     # index 1
        self.v_splitter.addWidget(self.stack)

        # Maya's log stream. It is read-only, so it is an OUTPUT channel, not a terminal: the interactive
        # prompt is the Debug Console next to it.
        self.output = output_widget.Widget()
        self.output.errorClicked.connect(self._goto_error)
        try:
            self.output.setObjectName("codeOutput")
            self.output.editor.setStyleSheet("")      # drop the console's own hardcoded surface
            self.output.bar.setStyleSheet("background-color: transparent;")
            self.output.label.hide()                  # the panel tab already reads OUTPUT
        except Exception:
            pass

        # bottom panel: the log stream, the syntax problems, and an interactive prompt
        self.problems = ProblemsPanel(self._open_sources)
        self.problems.problemActivated.connect(self._goto_error)
        self.problems.countChanged.connect(self._problems_count_changed)
        self.problems.tallyChanged.connect(lambda e, w: self.status.set_tally(e, w))
        self.console = DebugConsole(self.namespace)
        self.console.executed.connect(self._refresh_variables)

        self.panel = qt.QTabWidget()
        self.panel.setObjectName("codePanel")
        self.panel.setDocumentMode(True)
        # the strip BETWEEN the last tab and the corner controls belongs to the QTabWidget, not to
        # the tab bar. Without WA_StyledBackground its stylesheet rule is never painted and that gap
        # keeps the generic near-black QWidget colour.
        self.panel.setAttribute(qt.Qt.WA_StyledBackground, True)
        self.panel.setTabBar(BadgeTabBar())      # lets PROBLEMS carry a count bubble
        self.panel.addTab(self.problems, "PROBLEMS")
        self.panel.addTab(self.output, "OUTPUT")
        self.panel.addTab(self.console, "DEBUG CONSOLE")
        self.panel.currentChanged.connect(self._panel_changed)
        # reserve the badge room straight away, so the row has its final geometry on the first paint
        self.panel.tabBar().set_badge(self.panel.indexOf(self.problems), 0)
        try:
            # lift the console's own toolbar (.py/.mel, echo, clear) into the tab row's right corner.
            # it needs its own surface: the corner area is not painted by the tab bar and would
            # otherwise fall back to the dark global QWidget rule.
            bar = self.output.bar
            bar.setObjectName("codePanelCorner")
            bar.setAttribute(qt.Qt.WA_StyledBackground, True)
            bar.setStyleSheet("")                   # its rules now live in the editor's stylesheet
            self.output.echo.setStyleSheet("")      # drop the console's inline grey background
            # the console ships the switch as two radios; a combo reads better next to Echo All and
            # matches the flat look. The radios stay as the state, just hidden.
            self.output.python.hide()
            self.output.mel.hide()
            self.language = qt.QComboBox()
            self.language.addItems(["MEL", "Python", "Python (longname)"])
            self.language.setFixedHeight(qt.px(20))
            self.language.setToolTip("Show Maya's command echo as MEL, or translated to python\n"
                                     "with short (r=1) or long (radius=1) flag names")
            self._languages = ["mel", "python", "python_long"]
            self.language.setCurrentIndex(
                self._languages.index(getattr(self.output, "output_language", "python_long")))
            self.language.currentIndexChanged.connect(
                lambda index: setattr(self.output, "output_language", self._languages[index]))
            bar.layout().insertWidget(max(0, bar.layout().count() - 2), self.language)
            # the bottom margin lifts the row off the tab underline; the spacing separates the two
            # combos, which the console packs edge to edge
            bar.layout().setContentsMargins(0, 0, qt.px(6), qt.px(5))
            bar.layout().setSpacing(qt.px(6))
            bar.layout().setAlignment(qt.Qt.AlignVCenter)
            self.panel.setCornerWidget(bar, qt.Qt.TopRightCorner)
        except Exception:
            pass
        # OUTPUT is the tab you actually watch while working; PROBLEMS keeps its place first in the row
        # and its badge already tells you when it has something to say.
        self.panel.setCurrentWidget(self.output)
        self._panel_changed(self.panel.currentIndex())    # sync the corner with the starting tab
        self.v_splitter.addWidget(self.panel)

        self.v_splitter.setSizes([qt.px(600), qt.px(180)])
        self.v_splitter.setStretchFactor(0, 1)  # editor tabs: grow
        self.v_splitter.setStretchFactor(1, 0)  # output terminal: stay short at the bottom

        right_layout.addWidget(self.v_splitter)
        self.h_splitter.addWidget(right)

        # secondary side bar on the far right, hidden until toggled
        self.secondary = SecondaryPanel()
        self.secondary.argumentPicked.connect(
            lambda name: self._to_code("insert_argument", name))
        self.secondary.setMinimumWidth(qt.px(160))
        self.secondary.setVisible(False)
        self.h_splitter.addWidget(self.secondary)

        # side columns stay narrow, the editor area takes the rest (VS Code proportions)
        self.sidebar.setMinimumWidth(qt.px(160))
        self.h_splitter.setStretchFactor(0, 0)   # primary side bar: don't grow
        self.h_splitter.setStretchFactor(1, 1)   # editor area: take all extra width
        self.h_splitter.setStretchFactor(2, 0)   # secondary side bar: don't grow
        self.h_splitter.setSizes([qt.px(230), qt.px(900), qt.px(230)])
        # activity bar pinned to the far left, outside the splitter so it keeps a fixed width
        self.activity_bar = ActivityBar()
        self.activity_bar.viewChanged.connect(self._activity_view_changed)
        self.activity_bar.manageClicked.connect(self.show_manage)
        # source-control count bubble, kept in step with the panel
        self.sidebar.scm.countChanged.connect(lambda n: self.activity_bar.set_badge("scm", n))

        body = qt.QWidget()
        body_row = qt.QHBoxLayout(body)
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.setSpacing(0)
        activity_rule = qt.QFrame()                  # the thin separator, same as every other in the UI
        activity_rule.setFixedWidth(1)
        activity_rule.setObjectName("codeRule")

        body_row.addWidget(self.activity_bar, 0)
        body_row.addWidget(activity_rule, 0)
        body_row.addWidget(self.h_splitter, 1)

        # one continuous 1px rule under the menu bar, spanning activity bar + side bar + editor
        menu_rule = qt.QFrame()
        menu_rule.setFixedHeight(1)
        menu_rule.setObjectName("codeRule")
        main_layout.addWidget(menu_rule, 0)
        main_layout.addWidget(body, 1)

        status_rule = qt.QFrame()
        status_rule.setFixedHeight(1)
        status_rule.setObjectName("codeRule")
        self.status = StatusBar()
        self.status.problemsClicked.connect(lambda: self._show_panel(self.problems))
        self.status.lineClicked.connect(lambda: self._to_code("go_to_line"))
        self.status.branchClicked.connect(lambda: self._show_view("scm"))
        main_layout.addWidget(status_rule, 0)
        main_layout.addWidget(self.status, 0)

        self.palette_widget = palette.CommandPalette(self, self._palette_actions)
        self._build_menus()
        self._build_layout_toggles()
        self._apply_theme()          # the hand-painted parts the stylesheet cannot reach
        self._show_welcome_if_empty()

        if not chrome:
            # embedded mode: the tabs over the panel, and nothing else. The side bars are still BUILT
            # (the tab code talks to them for git status and search) but never shown, so the two
            # editors stay one codebase instead of drifting apart.
            for widget in (self.activity_bar, activity_rule):
                widget.setVisible(False)
            self.secondary.setVisible(False)
            # the Explorer stays - scoped to the krig's process scripts by the host - but Search and
            # Source Control do not: with no activity bar there is no way to reach them anyway.
            self.sidebar.workspace_section.setVisible(True)
            # the other three are hidden, not removed: the view header's "..." brings any of them
            # back for whoever wants them inside the host
            self.sidebar.open_section.setVisible(False)
            self.sidebar.outline_section.setVisible(False)
            self.sidebar.timeline_section.setVisible(False)
            self.h_splitter.setSizes([qt.px(230), qt.px(900), 0])
            # the host owns File/Edit/...; here the menu bar carries no menus. Rather than keep an empty
            # strip just to hold the layout toggles, drop the menu bar entirely and move the toggles into
            # the code tabs' own right corner - which is where they belong inside the host.
            self.menu_bar.clear()
            self.menu_bar.setCornerWidget(None)
            self.menu_bar.setVisible(False)
            self.tabs.setCornerWidget(self._corner, qt.Qt.TopRightCorner)
            self._corner.setVisible(True)
            # lift the toggles a touch so they sit up on the tab row (bottom margin raises them)
            self._corner.layout().setContentsMargins(0, 0, qt.px(6), qt.px(6))

        # reopen where the last run left off, and keep that record in step from now on
        self._session_timer = qt.QTimer(self)
        self._session_timer.setSingleShot(True)
        self._session_timer.setInterval(1200)          # debounced: typing must not hit the disk per key
        self._session_timer.timeout.connect(self._store_session)
        self._autosave_timer = qt.QTimer(self)           # File > Auto Save, "after delay"
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(1500)
        self._autosave_timer.timeout.connect(lambda: self.autosave and self.save_all())
        self.tabs.currentChanged.connect(lambda *_: self._session_timer.start())
        self.sidebar.open_section.on_expand = self._refresh_open_editors
        self.sidebar.outline_section.on_expand = self._load_outline
        self.sidebar.timeline_section.on_expand = self._load_timeline
        self._restore_session()
        self._refresh_side_views()
        # git runs on the first turn of the event loop, not inside __init__: a synchronous scan of a
        # large repository here would hold the window back before anything is on screen. Deferred, it
        # opens at once and the change list and its badge fill in a moment later.
        qt.QTimer.singleShot(0, self._first_scan)

    # ------------------------------------------------------------------ tabs

    @property
    def tabs(self):
        """The active editor group (a QTabWidget). Single-group code keeps working unchanged.

        Returns:
            object: the currently focused editor group.
        """
        return self._active_group

    def _make_group(self):
        """Build one editor group (a QTabWidget), wired like the original single tab widget.

        Returns:
            object: the new group, already added to the editor splitter and the _groups list.
        """
        group = qt.QTabWidget()
        group.setAttribute(qt.Qt.WA_StyledBackground, True)   # paint the strip past the last tab
        bar = PreviewTabBar()
        bar.is_preview = lambda index, g=group: g.widget(index) is self._preview_page
        bar.is_pinned = lambda index, g=group: g.widget(index) in self._pinned
        bar.mover = self._drop_tab
        group.setTabBar(bar)
        group.setTabsClosable(True)
        group.setMovable(True)
        group.setIconSize(qt.QSize(qt.px(20), qt.px(20)))
        group.setDocumentMode(True)
        group.tabCloseRequested.connect(lambda index, g=group: (self._set_active(g), self.close_tab(index)))
        group.tabBarClicked.connect(lambda index, g=group: self._set_active(g))
        group.currentChanged.connect(lambda index, g=group: self._on_group_current(g, index))
        bar.setContextMenuPolicy(qt.Qt.CustomContextMenu)
        bar.customContextMenuRequested.connect(lambda pos, g=group: (self._set_active(g), self._tab_context_menu(pos)))
        self._groups.append(group)
        self.editor_split.addWidget(group)
        return group

    def _set_active(self, group=None) -> None:
        """Make `group` the active editor group that single-group operations target.

        Args:
            group: (object): - the editor group to activate.

        Returns:
            None.
        """
        if group is not None and group in self._groups:
            self._active_group = group

    def _on_group_current(self, group=None, index:int=None) -> None:
        """React to a tab-switch inside `group`: activate it, then run the usual current-tab refreshes.

        Args:
            group: (object): - the group whose current tab changed.
            index:    (int): - the newly selected tab index.

        Returns:
            None.
        """
        self._set_active(group)
        self._touch_mru()
        self._update_menu_targets()
        self._mark_location()
        self._refresh_side_views()
        self._refresh_open_editors()
        if getattr(self, "_problems_timer", None) is not None:
            self._problems_timer.start()

    def _all_pages(self) -> list:
        """Every open page across all editor groups, in group then tab order.

        Returns:
            list: the pages held by every group.
        """
        return [group.widget(index) for group in self._groups for index in range(group.count())]

    def _focus_page(self, page=None) -> bool:
        """Activate the group that holds `page` and select it. Returns True when found.

        Args:
            page: (object): - the page to focus.

        Returns:
            bool: True when the page was found and focused.
        """
        for group in self._groups:
            index = group.indexOf(page)
            if index >= 0:
                self._set_active(group)
                group.setCurrentIndex(index)
                return True
        return False

    def _group_index_of(self, page=None) -> int:
        """The index of the pane holding `page`, or 0 when it is not found.

        Args:
            page: (object): - the page to locate.

        Returns:
            int: the pane index.
        """
        for position, group in enumerate(self._groups):
            if group.indexOf(page) >= 0:
                return position
        return 0

    def _split_right(self, index:int=None) -> None:
        """Move the tab at `index` into a second editor group on the right, creating it when needed.

        Args:
            index: (int): - the tab in the active group to split off.

        Returns:
            None.
        """
        source = self._active_group
        page = source.widget(index)
        if page is None:
            return
        target = next((group for group in self._groups if group is not source), None) or self._make_group()
        text, icon = source.tabText(index), source.tabIcon(index)
        source.removeTab(index)
        placed = target.addTab(page, icon, text)
        self._set_active(target)
        target.setCurrentIndex(placed)
        self._collapse_empty_groups()
        self._show_welcome_if_empty()

    def _collapse_empty_groups(self) -> None:
        """Remove any empty group beyond the first, so a closed split folds back to one pane.

        Returns:
            None.
        """
        for group in list(self._groups):
            if len(self._groups) > 1 and group.count() == 0:
                self._groups.remove(group)
                group.setParent(None)
                group.deleteLater()
        if self._active_group not in self._groups:
            self._active_group = self._groups[0]

    def _drop_tab(self, source_bar=None, source_index:int=None, target_bar=None, drop_index:int=None) -> None:
        """Move the tab dragged from `source_bar` into the pane owning `target_bar` at `drop_index`.

        Args:
            source_bar:   (object): - the tab bar the drag started on.
            source_index:    (int): - the dragged tab's index in the source pane.
            target_bar:   (object): - the tab bar dropped onto.
            drop_index:      (int): - the position under the cursor (-1 to append).

        Returns:
            None.
        """
        source = next((group for group in self._groups if group.tabBar() is source_bar), None)
        target = next((group for group in self._groups if group.tabBar() is target_bar), None)
        if source is None or target is None or source_index < 0:
            return
        if source is target:
            dest = max(0, min(drop_index, source.count() - 1)) if drop_index >= 0 else source.count() - 1
            source.tabBar().moveTab(source_index, dest)
            self._reconcile_pins(source)
            return
        page = source.widget(source_index)
        text, icon = source.tabText(source_index), source.tabIcon(source_index)
        source.removeTab(source_index)
        placed = target.insertTab(drop_index, page, icon, text) if 0 <= drop_index <= target.count() \
            else target.addTab(page, icon, text)
        self._set_active(target)
        target.setCurrentIndex(placed)
        self._collapse_empty_groups()
        self._show_welcome_if_empty()
        for group in self._groups:
            self._reconcile_pins(group)

    def _is_pinned(self, page=None) -> bool:
        """Whether `page` is a pinned tab.

        Returns:
            bool: True when the page is pinned.
        """
        return page in self._pinned

    def _toggle_pin(self, index:int=None) -> None:
        """Pin or unpin the tab at `index` in the active pane.

        Args:
            index: (int): - the tab to toggle.

        Returns:
            None.
        """
        page = self.tabs.widget(index)
        if page is None:
            return
        self._pinned.discard(page) if page in self._pinned else self._pinned.add(page)
        self._reconcile_pins(self.tabs)
        self._refresh_open_editors()
        self._touch_session()

    def _unpin(self, page=None) -> None:
        """Unpin `page` (used by its pin button) and reflow its pane.

        Args:
            page: (object): - the page to unpin.

        Returns:
            None.
        """
        self._pinned.discard(page)
        for group in self._groups:
            if group.indexOf(page) >= 0:
                self._reconcile_pins(group)
                break
        self._refresh_open_editors()
        self._touch_session()

    def _reconcile_pins(self, group=None) -> None:
        """Reorder `group` so pinned tabs sit first (their pin glyph is painted by the tab bar).

        Args:
            group: (object): - the pane to reflow.

        Returns:
            None.
        """
        if group is None or not qt.is_valid(group):
            return
        pinned = [group.widget(i) for i in range(group.count()) if group.widget(i) in self._pinned]
        others = [group.widget(i) for i in range(group.count()) if group.widget(i) not in self._pinned]
        for target_pos, page in enumerate(pinned + others):
            current = group.indexOf(page)
            if current >= 0 and current != target_pos:
                group.tabBar().moveTab(current, target_pos)
        group.tabBar().update()                       # repaint so the pin glyphs follow the new order

    def _on_focus_changed(self, old=None, new=None) -> None:
        """Activate the pane whose body just took focus (only matters once a split exists).

        Args:
            old: (object): - the widget that lost focus.
            new: (object): - the widget that gained focus.

        Returns:
            None.
        """
        if new is None or len(getattr(self, "_groups", [])) < 2 or not qt.is_valid(self):
            return
        widget = new
        while widget is not None:
            if widget in self._groups:
                self._set_active(widget)
                return
            widget = widget.parentWidget()

    def _close_open_editor_page(self, page=None) -> None:
        """Close `page` wherever it lives, activating its pane first.

        Args:
            page: (object): - the page to close.

        Returns:
            None.
        """
        for group in self._groups:
            index = group.indexOf(page)
            if index >= 0:
                self._set_active(group)
                self.close_tab(index)
                return

    def _activate_open_editor(self, flat:int=None) -> None:
        """OPEN EDITORS click: focus the page at flat position `flat` across all panes.

        Args:
            flat: (int): - the position in the flat all-panes list.

        Returns:
            None.
        """
        pages = getattr(self, "_open_pages", [])
        if 0 <= flat < len(pages):
            self._focus_page(pages[flat])

    def _close_open_editor(self, flat:int=None) -> None:
        """OPEN EDITORS close: close the page at flat position `flat`.

        Args:
            flat: (int): - the position in the flat all-panes list.

        Returns:
            None.
        """
        pages = getattr(self, "_open_pages", [])
        if 0 <= flat < len(pages):
            self._close_open_editor_page(pages[flat])

    def _close_other_open_editors(self, flat:int=None) -> None:
        """OPEN EDITORS 'close others': keep the page at `flat`, close every other across all panes.

        Args:
            flat: (int): - the position of the page to keep.

        Returns:
            None.
        """
        pages = getattr(self, "_open_pages", [])
        if not (0 <= flat < len(pages)):
            return
        keep = pages[flat]
        for page in [candidate for candidate in self._all_pages() if candidate is not keep]:
            self._close_open_editor_page(page)

    def current_page(self) -> "EditorPage":
        """The active EditorPage, or None.

        Returns:
            'EditorPage': the current EditorPage, or None when the active tab is not one.
        """
        w = self.tabs.currentWidget()
        return w if isinstance(w, EditorPage) else None

    def _show_welcome_if_empty(self) -> None:
        """Show the welcome page when no tab is open, the tabs otherwise.

        Returns:
            None.
        """
        self.stack.setCurrentIndex(1 if self._all_pages() else 0)

    def _new_editor_page(self, path:str=None) -> "EditorPage":
        """An EditorPage with every window-level signal already wired.

        One place to do it, so a page built by New File, by an open, or by a session restore all behave
        the same: dirty marker, preview pinning, problems re-analysis and session bookkeeping.

        Returns:
            'EditorPage': the newly built, fully-wired page.
        """
        page = EditorPage(file_path=path, namespace=self.namespace, surface=self.theme["editor"],
                          palette=self.theme)
        page.code.textChanged.connect(lambda p=page: self._mark_dirty(p))
        page.code.textChanged.connect(lambda p=page: self._pin_page(p))   # a real edit pins the preview
        page.code.textChanged.connect(lambda: self._problems_timer.start())
        page.code.textChanged.connect(self._touch_session)
        # the engine's own right-click menu only EMITS; without these the Save it offers does nothing
        page.code.cursorPositionChanged.connect(self._follow_caret)
        page.code.savingScript.connect(lambda *_: self.save_file())
        page.code.savingScriptAs.connect(lambda *_: self.save_file_as())
        page.code.changesReverted.connect(lambda *_, p=page: self._refresh_changes(p))
        if self.word_wrap:
            page.code.setLineWrapMode(qt.QPlainTextEdit.WidgetWidth)
        # a tab opened after the setting was changed has to inherit it, or turning either off would
        # only hold until the next file was opened
        page.code.sticky_scroll = self.sticky_on
        page.code.argument_completion = self.secondary.isVisibleTo(self)
        if page.code.minimap is not None:
            page.code.minimap.setVisible(self.minimap_on)
        page.crumbs.set_path(path, self.sidebar.workspace.roots)
        page.crumbs.revealRequested.connect(self.reveal_path)
        page.crumbs.fileActivated.connect(self.open_file)
        return page

    def new_file(self) -> None:
        """Open a fresh untitled tab.

        Returns:
            None.
        """
        page = self._new_editor_page()
        index = self.tabs.addTab(page, page.name())
        self.tabs.setCurrentIndex(index)
        self._show_welcome_if_empty()
        self._touch_session()

    def open_file(self, path:str=None, preview:bool=False) -> None:
        """Open `path` in a tab, asking for one when None.

        `preview` reproduces VS Code's single-click behaviour: the tab title is italic and the NEXT
        preview reuses that same slot. Opening the same file without preview pins it for good.

        Returns:
            None.
        """
        if not path:
            path = compat.message.file(title="Open File", filter="Python/MEL (*.py *.mel);;All Files (*.*)", parent=self)
        if not path or not os.path.isfile(path):
            return

        # already open: focus it, and pin it when this was a real open
        for page in self._all_pages():
            if isinstance(page, (EditorPage, ImagePage)) and page.file_path \
                    and os.path.normpath(page.file_path) == os.path.normpath(path):
                self._focus_page(page)
                if not preview and self._preview_page is page:
                    self._preview_page = None
                    self.tabs.tabBar().update()
                return

        if languages.is_image(path):
            self._remember(path)
            self._place_tab(ImagePage(path), file_icon(path), os.path.basename(path), preview)
            return

        self._remember(path)
        # reuse the page kept alive when this file's tab was closed, so its undo/redo history comes back
        page = self._take_cached_page(path) or self._new_editor_page(path)
        self._place_tab(page, file_icon(path), os.path.basename(path), preview)

    def _place_tab(self, page, icon, label:str, preview:bool) -> int:
        """Add `page` as a tab, reusing the preview slot when `preview` is set. Returns its index.

        Returns:
            int: the index of the placed tab.
        """
        stale = self._preview_page if preview else None
        slot = self.tabs.indexOf(stale) if stale is not None else -1
        if slot >= 0:
            self.tabs.removeTab(slot)                # reuse the preview slot
            stale.deleteLater()
            index = self.tabs.insertTab(slot, page, icon, label)
        else:
            index = self.tabs.addTab(page, icon, label)

        self._preview_page = page if preview else None
        self.tabs.setCurrentIndex(index)
        self.tabs.tabBar().update()
        self._show_welcome_if_empty()
        self._touch_session()
        self._refresh_open_editors()
        return index

    def _pin_page(self, page) -> None:
        """Promote a preview tab to a permanent one, on the first REAL edit.

        textChanged also fires for things the user did not type - a highlighter re-running, an encoding
        fallback - so the document's modified flag is what decides: syntax highlighting preserves it.

        Returns:
            None.
        """
        if self._preview_page is page and page.is_modified():
            self._preview_page = None
            self.tabs.tabBar().update()
            self._touch_session()

    def save_file(self) -> None:
        """Save the current tab (Save As when untitled).

        Returns:
            None.
        """
        page = self.current_page()
        if page is None:
            return
        if not page.file_path:
            return self.save_file_as()
        if self.trim_on_save:
            page.code.trim_trailing()        # before the write, so what lands on disk is clean
        if page.save():
            self._refresh_tab_title(page)
            # The WRITE is done and safe at this point - it is atomic and takes milliseconds. What
            # follows is the slow part: relinting every open tab, and two git subprocesses. Running
            # them one per event-loop turn lets the window repaint between them, which is the only
            # reason the spinner can actually turn.
            self.status.set_busy(True, "Saving...")
            self.run_steps([
                (self.sidebar.refresh_vcs if self.chrome else None),   # git status
                self.problems.refresh,                                 # a save can fix an error
                lambda: self._refresh_changes(page),                   # hunks against HEAD
                self._store_session,                                   # the file is the truth again
            ], done=lambda: self.status.set_busy(False))

    def run_steps(self, steps:list, done=None) -> None:
        """Run `steps` one per turn of the event loop, then call `done`.

        Not a thread: these touch widgets, and Qt only allows that from the main one. Yielding
        between them is what a thread would buy us here anyway - the window gets to repaint, so a
        busy indicator moves instead of sitting frozen.

        A step that raises does not strand the indicator: the chain carries on and `done` still runs.

        Returns:
            None.
        """
        queue = [step for step in steps if step is not None]

        def pump() -> None:
            """Pump.

            Returns:
                None.
            """
            if not self._alive():
                return
            if not queue:
                if callable(done):
                    done()
                return
            step = queue.pop(0)
            try:
                step()
            except Exception:
                traceback.print_exc()
            qt.QTimer.singleShot(0, pump)

        qt.QTimer.singleShot(0, pump)

    def save_file_as(self) -> None:
        """Save the current tab under a chosen path.

        Returns:
            None.
        """
        page = self.current_page()
        if page is None:
            return
        start = page.file_path or ""
        path = compat.message.save(title="Save As", directory=start, filter="Python (*.py);;MEL (*.mel);;All Files (*.*)", parent=self)
        if path and page.save(path):
            self.tabs.setTabText(self.tabs.currentIndex(), page.name())
            self._refresh_tab_title(page)
            if self.chrome:
                self.sidebar.refresh_vcs()
            self.problems.refresh()          # a save can fix or introduce a syntax error
            stale = getattr(page, "_session_key", None)   # the backup it had while it was untitled
            if stale:
                self._session.discard(stale)
                page._session_key = None
            self._store_session()

    # ------------------------------------------------------------------ file commands

    RECENT_MAX = 12

    def _remember(self, path:str) -> None:
        """Push `path` to the front of the recent list, without duplicates.

        Returns:
            None.
        """
        if not path:
            return
        target = os.path.normpath(path)
        self.recent = [p for p in self.recent if os.path.normpath(p) != target]
        self.recent.insert(0, target)
        del self.recent[self.RECENT_MAX:]
        self._touch_session()

    def save_all(self) -> None:
        """Save every modified tab that has a path. Untitled ones are left alone.

        Prompting for a location per untitled buffer would turn one menu click into a queue of
        dialogs; their content is in the backup either way, so nothing is at risk.

        Returns:
            None.
        """
        saved = 0
        for page in self._all_pages():
            if isinstance(page, EditorPage) and page.file_path and page.is_modified():
                saved += bool(page.save())
                self._refresh_tab_title(page)
        if saved:
            self.problems.refresh()
            self._store_session()

    def revert_file(self) -> None:
        """Throw away the current tab's edits and reload it from disk.

        Returns:
            None.
        """
        page = self.current_page()
        if page is None or not page.file_path or not os.path.isfile(page.file_path):
            return
        if page.is_modified():
            answer = compat.message.warning(
                title="Revert File", buttons=["Revert", "Cancel"],
                message_text='Discard the changes to "%s"?' % page.name(),
                informative_text="The file on disk will replace what is in the editor.", parent=self)
            if answer != "Revert":
                return
        position = page.code.textCursor().position()
        page.code.setPlainText(compat.folder.read(page.file_path))
        page.code.document().setModified(False)
        cursor = page.code.textCursor()
        cursor.setPosition(min(position, len(page.code.toPlainText())))
        page.code.setTextCursor(cursor)
        self._session.discard(self._page_key(page))     # the file is the truth again
        self._refresh_tab_title(page)
        self.problems.refresh()

    def toggle_autosave(self, on:bool) -> None:
        """Turn File > Auto Save on or off.

        Off by default on purpose: the editor's whole model is that the file on disk is untouched
        until you ask. Auto Save trades that away for convenience, so it has to be a decision.

        Returns:
            None.
        """
        self.autosave = bool(on)
        self._touch_session()
        if self.autosave:
            self.save_all()

    def close_all_tabs(self) -> None:
        """Close every tab, asking about each one that has unsaved changes.

        Returns:
            None.
        """
        while self.tabs.count():
            before = self.tabs.count()
            self.close_tab(self.tabs.count() - 1)
            if self.tabs.count() == before:
                return                                   # the user cancelled: stop there

    def reopen_closed(self) -> None:
        """Reopen the last tab that was closed, back at the line it was on.

        Returns:
            None.
        """
        while self._closed:
            path, line = self._closed.pop()
            if path and os.path.isfile(path):
                self.open_file(path, preview=False)
                if line:
                    self._goto_error(path, line)
                return

    def run_path(self, path:str) -> None:
        """Open a script and execute it, straight from the explorer's context menu.

        Returns:
            None.
        """
        if not path or not os.path.isfile(path):
            return
        self.open_file(path, preview=False)
        page = self.current_page()
        if page is not None:
            page.code.execute(page.code.toPlainText())

    def _path_renamed(self, old:str, new:str) -> None:
        """Follow a rename made in the explorer: retitle the open tab and move its backup.

        Returns:
            None.
        """
        target = os.path.normpath(old)
        self._drop_cached_page(old)                   # a closed-tab undo cache under the old name is stale now
        for page in self._all_pages():
            path = getattr(page, "file_path", None)
            if not path or os.path.normpath(path) != target:
                continue
            self._session.discard(self._page_key(page))   # the old key no longer points anywhere
            page.file_path = new
            if isinstance(page, EditorPage):
                page.code.file_path = new
                self.tabs.setTabIcon(index, file_icon(new))
                self._refresh_tab_title(page)
        self._store_session()

    def _path_deleted(self, path:str) -> None:
        """Close the tabs whose file the explorer just removed, without prompting to save it.

        Returns:
            None.
        """
        target = os.path.normpath(path)
        prefix = self._cache_key(path)                # also forget any closed-tab undo cache under it
        for key in list(self._page_cache_order):
            if prefix and (key == prefix or key.startswith(prefix + os.sep)):
                entry = self._page_cache.pop(key, None)
                self._page_cache_order.remove(key)
                if entry is not None:
                    entry[0].deleteLater()
        for group in list(self._groups):
            for index in reversed(range(group.count())):
                page = group.widget(index)
                open_path = getattr(page, "file_path", None)
                if not open_path:
                    continue
                normalised = os.path.normpath(open_path)
                if normalised == target or normalised.startswith(target + os.sep):
                    self._session.discard(self._page_key(page))
                    if self._preview_page is page:
                        self._preview_page = None
                    group.removeTab(index)      # not close_tab: asking to save a deleted file is absurd
                    page.deleteLater()
        self._collapse_empty_groups()
        self._show_welcome_if_empty()
        self._store_session()

    def reveal_path(self, path:str) -> None:
        """Show `path` in the OS file browser.

        Returns:
            None.
        """
        if not path or not os.path.exists(path):
            return
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception:
            pass

    def reveal_file(self) -> None:
        """File > Reveal in File Explorer: the current tab's file.

        Returns:
            None.
        """
        self.reveal_path(getattr(self.tabs.currentWidget(), "file_path", None))

    def copy_path(self) -> None:
        """Put the current file's full path on the clipboard.

        Returns:
            None.
        """
        path = getattr(self.tabs.currentWidget(), "file_path", None)
        if path:
            compat.copy(os.path.normpath(path))

    PAGE_CACHE_MAX = 25                                # how many closed editors keep their undo history alive

    def _cache_key(self, path:str=None) -> str:
        """The normalised path used to key the closed-page (undo) cache.

        Returns:
            str: the normcased, normalised path, or None when no path was given.
        """
        return os.path.normcase(os.path.normpath(path)) if path else None

    def _cache_page(self, page) -> bool:
        """Keep a just-closed EditorPage alive (hidden) so its native undo/redo survives a reopen.

        Only a clean, saved-to-disk page is cached: a modified page the user chose NOT to save must not
        come back with its discarded edits. Bounded LRU - the oldest cached page is dropped for good.
        Returns True when the page was taken over (caller must NOT deleteLater it).

        Returns:
            bool: True when the page was cached and adopted, False otherwise.
        """
        key = self._cache_key(getattr(page, "file_path", None))
        if key is None or not isinstance(page, EditorPage) or page.is_modified():
            return False
        try:
            mtime = os.path.getmtime(page.file_path)
        except OSError:
            return False
        old = self._page_cache.pop(key, None)         # replace any stale entry for the same file
        if old is not None:
            old[0].deleteLater()
        if key in self._page_cache_order:
            self._page_cache_order.remove(key)
        page.setParent(self)                          # removeTab orphans it; own it so Qt keeps it alive
        page.hide()
        self._page_cache[key] = (page, mtime)
        self._page_cache_order.append(key)
        while len(self._page_cache_order) > self.PAGE_CACHE_MAX:
            evicted = self._page_cache.pop(self._page_cache_order.pop(0), None)
            if evicted is not None:
                evicted[0].deleteLater()
        return True

    def _take_cached_page(self, path:str=None) -> "EditorPage":
        """Pop the cached EditorPage for `path` when its content still matches disk, else None (dropping a

        Returns:
            'EditorPage': the cached page still matching disk, or None.
        stale one). Reusing it restores the exact undo/redo stack it had when the tab was closed."""
        key = self._cache_key(path)
        entry = self._page_cache.pop(key, None) if key else None
        if key in self._page_cache_order:
            self._page_cache_order.remove(key)
        if entry is None:
            return None
        page, cached_mtime = entry
        try:
            current_mtime = os.path.getmtime(path)
        except OSError:
            current_mtime = cached_mtime
        if current_mtime != cached_mtime:             # edited elsewhere since we cached it: undo is stale
            page.deleteLater()
            return None
        return page

    def _drop_cached_page(self, path:str=None) -> None:
        """Forget (and destroy) any cached page for `path` - used when the file is renamed or deleted.

        Returns:
            None.
        """
        page = self._take_cached_page(path)
        if page is not None:
            page.deleteLater()

    def close_tab(self, index:int=None) -> None:
        """Close a tab, prompting if it has unsaved changes.

        Returns:
            None.
        """
        page = self.tabs.widget(index)
        if isinstance(page, EditorPage) and page.is_modified():
            answer = compat.message.warning(title="Unsaved Changes", buttons=["Save", "Don't Save", "Cancel"],
                                            message_text='Save changes to "%s"?' % page.name(), informative_text="", parent=self)
            if answer in (None, "Cancel"):
                return
            if answer == "Save":
                self.tabs.setCurrentIndex(index)
                self.save_file()
        if self._preview_page is page:
            self._preview_page = None
        path = getattr(page, "file_path", None)
        if path:
            line = (page.code.textCursor().blockNumber() + 1) if isinstance(page, EditorPage) else 1
            self._closed.append((path, line))
            del self._closed[:-20]                    # a short history is enough, and bounded
        self._session.discard(self._page_key(page))   # closed on purpose: nothing left to recover
        self._pinned.discard(page)
        self.tabs.removeTab(index)
        if not self._cache_page(page):                # kept alive for its undo history, or...
            page.deleteLater()                        # ...a dirty/untitled page really goes away
        self._collapse_empty_groups()                 # a split that just emptied folds back to one pane
        self._show_welcome_if_empty()
        self._refresh_open_editors()
        self._store_session()                         # immediate: a close must not be lost to the delay

    def _mark_dirty(self, page:"EditorPage") -> None:
        """Refresh the tab title and, when auto-save is on, arm its timer.

        Returns:
            None.
        """
        self._refresh_tab_title(page)
        if page.file_path:
            self._last_edit = (page.file_path, page.code.textCursor().blockNumber() + 1)
        if self.autosave:
            self._autosave_timer.start()     # debounced: never a write per keystroke

    def _refresh_tab_title(self, page:"EditorPage") -> None:
        """Update the tab label, adding the dirty dot when the page is modified.

        Returns:
            None.
        """
        index = self.tabs.indexOf(page)
        if index >= 0:
            name = page.name()
            self.tabs.setTabText(index, ("● " + name) if page.is_modified() else name)
            self._refresh_open_editors()

    # ------------------------------------------------------------------ session

    def _apply_theme(self) -> None:
        """Re-skin everything the editor paints BY HAND rather than through the stylesheet.

        Delegates and the two custom tab bars draw with plain QColor, so no stylesheet reaches them.
        The values are written onto the INSTANCES, never the classes: both editors can be open at
        once, and a class-level write would repaint the other one too.

        Returns:
            None.
        """
        palette = self.theme
        roles = (("GUIDE_COLOR", "guide"), ("SELECT", "selection"), ("BUBBLE", "selection"),
                 ("TEXT", "text"), ("TEXT_ON", "bright"), ("TEXT_DIM", "muted"),
                 ("MUTED", "muted"), ("PILL", "accent"),
                 ("LABEL", "dim"), ("LABEL_ON", "bright"))

        # collected from the widget tree, not from attribute paths: several delegates are handed
        # straight to setItemDelegate without being stored, and a path-based walk would miss them
        painters = [view.itemDelegate() for view in self.findChildren(qt.QAbstractItemView)]
        painters += [self.tabs.tabBar(), self.panel.tabBar(), self.problems]

        for painter in painters:
            if painter is None:
                continue
            for name, key in roles:
                if hasattr(type(painter), name):     # only the roles this painter actually uses
                    setattr(painter, name, palette[key])

    def _touch_session(self, *_) -> None:
        """Ask for a (debounced) session write. Safe before the timer exists, e.g. during a reload.

        Returns:
            None.
        """
        timer = getattr(self, "_session_timer", None)
        if timer is not None and qt.is_valid(timer):
            timer.start()

    # ---- interface state
    #
    # This rides in the session store rather than in a pile of optionVars. It is the same thing a
    # Maya optionVar would give you - a value that outlives the window - but it is already atomic,
    # already per-editor, and it keeps the standalone window's layout from being the same slot as
    # the panel embedded in the host. The font size stays on its own optionVar, in the engine,
    # because the engine is used outside this window too.

    def _layout_state(self) -> dict:
        """The proportions and visibility worth restoring next time.

        Returns:
            dict: the layout proportions and visibility to restore next time.
        """
        try:
            return {
                # position as well as size: reopening in the same place is half of "where I left it"
                "window": [self.x(), self.y(), self.width(), self.height()],
                # a hidden panel measures 0, and storing that would reopen it collapsed next launch
                "panels": {key: int(self._panel_sizes.get(self._panel_named(key), 0) or
                                    self._panel_extent(self._panel_named(key)))
                           for key in self.PANEL_KEYS},
                "columns": [int(v) for v in self.h_splitter.sizes()],
                "rows": [int(v) for v in self.v_splitter.sizes()],
                "sidebar": self.sidebar.isVisibleTo(self),
                "panel": self.panel.isVisibleTo(self),
                "secondary": self.secondary.isVisibleTo(self),
                "panel_tab": self.panel.currentIndex(),
                "view": self.activity_bar.current(),
                "theme": self.theme_name,
                "minimap": self.minimap_on,
                "sticky": self.sticky_on,
                "trim_on_save": self.trim_on_save,
            }
        except Exception:
            return {}

    def _restore_layout(self, state:dict) -> None:
        """Put the interface back where it was. Anything missing keeps the built-in default.

        The side bar's SECTION sizes are deliberately not among them: apply_sizes re-derives that
        stack on every toggle and every resize, so a stored set would be overwritten moments after
        being applied - and while it lasted it fought the collapsed/expanded policy.

        Returns:
            None.
        """
        if not state:
            return
        try:
            # remembered extents first: a panel restored hidden still needs its width for the day
            # it is reopened
            for key, extent in (state.get("panels") or {}).items():
                panel = self._panel_named(key)
                if panel is not None and int(extent or 0) > 0:
                    self._panel_sizes[panel] = int(extent)
            columns = state.get("columns")
            if columns and len(columns) == self.h_splitter.count():
                self.h_splitter.setSizes(columns)
            rows = state.get("rows")
            if rows and len(rows) == self.v_splitter.count():
                self.v_splitter.setSizes(rows)
            for widget, key in ((self.sidebar, "sidebar"), (self.panel, "panel"),
                                (self.secondary, "secondary")):
                if key in state:
                    widget.setVisible(bool(state[key]))
            tab = state.get("panel_tab")
            if isinstance(tab, int) and 0 <= tab < self.panel.count():
                self.panel.setCurrentIndex(tab)
            if state.get("view"):
                self.sidebar.show_view(state["view"])
                self.activity_bar.select(state["view"])
            if state.get("theme") and not self.locked_theme:   # a locked theme ignores the session's
                self.set_theme(state["theme"])
            if "minimap" in state and bool(state["minimap"]) != self.minimap_on:
                self._toggle_minimap(bool(state["minimap"]))
            if "sticky" in state and bool(state["sticky"]) != self.sticky_on:
                self._toggle_sticky(bool(state["sticky"]))
            if "trim_on_save" in state:
                self._toggle_trim(bool(state["trim_on_save"]))
            self._sync_argument_completion()     # the restored panel decides it
            # only the standalone owns its geometry: embedded, the host decides the size
            window = state.get("window") or []
            if self.chrome and len(window) == 4:
                x, y, width, height = (int(v) for v in window)
                if width > 200 and height > 200:
                    self.resize(width, height)
                    self._move_onto_screen(x, y)
            elif self.chrome and len(window) == 2 and all(v > 200 for v in window):
                self.resize(int(window[0]), int(window[1]))   # a state saved before positions
        except Exception:
            pass                                 # a stale or partial state must not block the open
        self._refresh_layout_toggles()

    def _move_onto_screen(self, x:int, y:int) -> None:
        """Move to (x, y) only if that lands on a screen that still exists.

        A saved position outlives the monitor it was saved on: unplug the second display and the
        window would reopen off-canvas, with no way to drag it back.

        Returns:
            None.
        """
        try:
            target = qt.QRect(x, y, max(1, self.width()), max(1, self.height()))
            for screen in qt.QApplication.screens():
                if screen.availableGeometry().intersects(target):
                    self.move(x, y)
                    return
        except Exception:
            pass                                 # no screen info: keep the default placement

    def _page_key(self, page) -> str:
        """The backup id of a page: derived from its path, or minted once for an untitled buffer.

        Returns:
            str: the page's backup id.
        """
        path = getattr(page, "file_path", None)
        if path:
            return session.Session.key(path)
        key = getattr(page, "_session_key", None)
        if key is None:
            key = session.Session.key(None, fallback=str(id(page)))
            page._session_key = key
        return key

    def _store_session(self) -> None:
        """Record the open tabs, and back up every buffer that has unsaved work in it.

        Returns:
            None.
        """
        store = getattr(self, "_session", None)
        if store is None:
            return
        tabs, keep = [], set()
        active_page = self.tabs.currentWidget()
        for page in self._all_pages():
            entry = {"preview": page is self._preview_page, "group": self._group_index_of(page),
                     "pinned": page in self._pinned}
            if isinstance(page, EditorPage):
                cursor = page.code.textCursor()
                entry.update(kind="editor", path=page.file_path,
                             line=cursor.blockNumber(), column=cursor.columnNumber())
                if page.is_modified() or not page.file_path:
                    key = self._page_key(page)
                    store.backup(key, page.code.toPlainText())
                    entry["backup"] = key
                    keep.add(key)
                else:
                    store.discard(self._page_key(page))   # saved: the file itself is the truth again
            elif isinstance(page, ImagePage):
                entry.update(kind="image", path=page.file_path)
            elif isinstance(page, DiffPage):
                root = getattr(page, "vcs_root", "")
                if not root:
                    continue                              # nothing to rebuild it from
                entry.update(kind="diff", path=page.file_path, root=root,
                             staged=bool(getattr(page, "vcs_staged", False)))
            else:
                continue
            tabs.append(entry)
        pages = self._all_pages()
        data = {"version": 1, "active": pages.index(active_page) if active_page in pages else -1, "tabs": tabs,
                "recent": self.recent, "autosave": self.autosave, "wrap": self.word_wrap,
                "layout": self._layout_state()}
        store.save(data)
        store.sweep(keep)                                 # drop backups of tabs that are gone
        self.sessionStored.emit(data)                     # let an embedder mirror it (the host)

    def set_session_folder(self, folder:str, name:str=None, enabled:bool=True) -> None:
        """Point the editor at another state store and reopen from it.

        the host calls this on every krig switch. The order matters: the tabs of the workspace you
        are LEAVING are flushed to its own store first, so coming back to it later restores them - open
        tabs, carets and unsaved buffers alike - even though nothing was saved or closed.

        `enabled` False means "remember nothing" (no project loaded): the tabs close and no store is
        touched at all, rather than spilling into the per-user one.

        Returns:
            None.
        """
        self._store_session()                            # flush what belongs to the previous store
        while self.tabs.count():
            page = self.tabs.widget(0)
            self.tabs.removeTab(0)
            page.deleteLater()
        self._preview_page = None
        self._session = session.Session(name or self._session.name, folder, enabled)
        self._show_welcome_if_empty()
        self._restore_session()

    def _restore_session(self) -> None:
        """Reopen the tabs of the previous run, unsaved work included.

        Returns:
            None.
        """
        data = self._session.load()
        self.recent = [p for p in (data.get("recent") or []) if isinstance(p, str)][:self.RECENT_MAX]
        self.autosave = bool(data.get("autosave"))
        if bool(data.get("wrap")) != self.word_wrap:
            self.toggle_word_wrap(bool(data.get("wrap")))
        self._restore_layout(data.get("layout") or {})
        for entry in data.get("tabs") or []:
            try:
                self._restore_tab(entry)
            except Exception:
                continue                                  # one unreadable entry must not stop the rest
        self._collapse_empty_groups()                     # drop a pane the session recreated but left empty
        for group in self._groups:
            self._reconcile_pins(group)                   # restore the pinned lane + strip their close button
        active = data.get("active", -1)
        pages = self._all_pages()
        if isinstance(active, int) and 0 <= active < len(pages):
            self._focus_page(pages[active])
        self._show_welcome_if_empty()
        self.tabs.tabBar().update()

    def _restore_tab(self, entry:dict) -> None:
        """Rebuild one tab from its session entry, into the pane it was saved from.

        Returns:
            None.
        """
        group_index = int(entry.get("group", 0) or 0)
        while len(self._groups) <= group_index:
            self._make_group()                            # recreate the split the session was saved with
        self._set_active(self._groups[group_index])
        kind, path, preview = entry.get("kind"), entry.get("path"), bool(entry.get("preview"))

        if kind == "diff":
            if path and entry.get("root"):
                self.open_diff(path, entry["root"], bool(entry.get("staged")), preview)
            return
        if kind == "image":
            if path and os.path.isfile(path):
                self.open_file(path, preview=preview)
            return
        if kind != "editor":
            return

        recovered = self._session.recover(entry["backup"]) if entry.get("backup") else None
        if recovered is None and not (path and os.path.isfile(path)):
            return                                        # file gone and nothing backed up

        page = self._new_editor_page(path if path and os.path.isfile(path) else None)
        page.file_path = path or page.file_path           # keep the path even if the file vanished
        if recovered is not None and recovered != page.code.toPlainText():
            # done BEFORE the tab exists, so the edit is not mistaken for the user pinning a preview
            page.code.setPlainText(recovered)
            page.code.document().setModified(True)
        if entry.get("backup"):
            page._session_key = entry["backup"]           # keep untitled buffers on their own backup

        self._place_tab(page, file_icon(path or ""), page.name(), preview)
        self._refresh_tab_title(page)

        cursor = page.code.textCursor()
        cursor.movePosition(qt.QTextCursor.Start)
        cursor.movePosition(qt.QTextCursor.Down, qt.QTextCursor.MoveAnchor, int(entry.get("line") or 0))
        cursor.movePosition(qt.QTextCursor.Right, qt.QTextCursor.MoveAnchor, int(entry.get("column") or 0))
        page.code.setTextCursor(cursor)
        if entry.get("pinned"):
            self._pinned.add(page)

    def closeEvent(self, event) -> None:
        """Flush the session on the way out, so a close never costs the layout or unsaved work.

        Returns:
            None.
        """
        try:
            self._store_session()
        except Exception:
            pass
        super().closeEvent(event)

    # ------------------------------------------------------------------ menus

    # sequences the focused widget must keep handling itself: a window-level shortcut would steal them
    # from whatever has focus (the search field, the debug console) and route them to the code tab
    NATIVE_KEYS = {"Ctrl+Z", "Ctrl+Shift+Z", "Ctrl+X", "Ctrl+C", "Ctrl+V", "Ctrl+A"}

    def _build_menus(self) -> None:
        """Build the menu bar. Run/Edit actions act on the current tab.

        Returns:
            None.
        """
        self._shortcuts = []                 # QShortcut objects die with no python reference
        self.file_menu = file_menu = self.menu_bar.addMenu("File")
        self._add(file_menu, "New File\tCtrl+N",         self.new_file)
        self._add(file_menu, "Open File...\tCtrl+O",     lambda: self.open_file(None))
        self.recent_menu = file_menu.addMenu("Open Recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        file_menu.addSeparator()

        self._add(file_menu, "Add Folder to Workspace...", self.sidebar.workspace.add_folder)
        self.folders_menu = file_menu.addMenu("Remove Folder from Workspace")
        self.folders_menu.aboutToShow.connect(self._fill_folders)
        file_menu.addSeparator()

        self.action_save = self._add(file_menu, "Save\tCtrl+S",             self.save_file)
        self.action_save_as = self._add(file_menu, "Save As...\tCtrl+Shift+S", self.save_file_as)
        self.action_save_all = self._add(file_menu, "Save All\tCtrl+Alt+S", self.save_all)
        self.action_autosave = self._add(file_menu, "Auto Save", self.toggle_autosave, check=True)
        self.action_revert = self._add(file_menu, "Revert File", self.revert_file)
        file_menu.addSeparator()

        self.action_reveal = self._add(file_menu, "Reveal in File Explorer", self.reveal_file)
        self.action_copy_path = self._add(file_menu, "Copy Path", self.copy_path)
        file_menu.addSeparator()

        self.action_close = self._add(file_menu, "Close Editor\tCtrl+W",
                                      lambda: self.close_tab(self.tabs.currentIndex()))
        self._add(file_menu, "Close All Editors\tCtrl+K W", self.close_all_tabs)
        self.action_reopen = self._add(file_menu, "Reopen Closed Editor\tCtrl+Shift+T",
                                       self.reopen_closed)
        if self.chrome:
            file_menu.addSeparator()
            self._add(file_menu, "Close Window\tAlt+F4", self.close)
        file_menu.aboutToShow.connect(self._sync_file_menu)

        edit_menu = self.menu_bar.addMenu("Edit")
        self._add(edit_menu, "Undo\tCtrl+Z",        lambda: self._to_code("undo"))
        self._add(edit_menu, "Redo\tCtrl+Shift+Z",  lambda: self._to_code("redo"))
        edit_menu.addSeparator()
        self._add(edit_menu, "Cut\tCtrl+X",         lambda: self._to_code("cut"))
        self._add(edit_menu, "Copy\tCtrl+C",        lambda: self._to_code("copy"))
        self._add(edit_menu, "Paste\tCtrl+V",       lambda: self._to_code("paste"))
        edit_menu.addSeparator()
        self._add(edit_menu, "Find\tCtrl+F",        lambda: self.find_in_file(False))
        self._add(edit_menu, "Replace\tCtrl+H",     lambda: self.find_in_file(True))
        self._add(edit_menu, "Find in Files\tCtrl+Shift+F", self.find_in_files)
        edit_menu.addSeparator()
        self._add(edit_menu, "Toggle Comment\tCtrl+/", lambda: self._to_code("toggle_comment"))
        # Ctrl+D goes to the multi-cursor, as in VS Code. Duplicating a line already has
        # Shift+Alt+Down under Selection > Copy Line Down, so nothing was lost.
        self._add(edit_menu, "Duplicate Line", lambda: self._to_code("duplicate_line"))
        self._add(edit_menu, "Delete Line\tCtrl+Shift+K", self.delete_line)
        edit_menu.addSeparator()
        self._add(edit_menu, "Rename Symbol\tF2", self.rename_symbol)
        edit_menu.addSeparator()
        # bound as Ctrl+= : QKeySequence("Ctrl++") resolves to Ctrl+Shift+= on most layouts, so the
        # hint would advertise a key that does nothing
        self._add(edit_menu, "Zoom In\tCtrl+=",     lambda: self._to_code("zoom_in_text"))
        self._add(edit_menu, "Zoom Out\tCtrl+-",    lambda: self._to_code("zoom_out_text"))

        selection_menu = self.menu_bar.addMenu("Selection")
        self._add(selection_menu, "Select All\tCtrl+A", lambda: self._to_code("selectAll"))
        self._add(selection_menu, "Expand Selection\tShift+Alt+Right",
                  lambda: self._to_code("expand_selection"))
        self._add(selection_menu, "Shrink Selection\tShift+Alt+Left",
                  lambda: self._to_code("shrink_selection"))
        selection_menu.addSeparator()
        self._add(selection_menu, "Copy Line Up\tShift+Alt+Up",
                  lambda: self._to_code("copy_line", -1))
        self._add(selection_menu, "Copy Line Down\tShift+Alt+Down",
                  lambda: self._to_code("copy_line", 1))
        self._add(selection_menu, "Move Line Up\tAlt+Up",   lambda: self._to_code("move_line", -1))
        self._add(selection_menu, "Move Line Down\tAlt+Down", lambda: self._to_code("move_line", 1))
        self._add(selection_menu, "Duplicate Selection",
                  lambda: self._to_code("duplicate_selection"))
        selection_menu.addSeparator()
        self._add(selection_menu, "Expand Line Selection\tCtrl+L",
                  lambda: self._to_code("select_line"))
        selection_menu.addSeparator()
        # A QPlainTextEdit has exactly one QTextCursor, so the extra carets are kept and painted by
        # the editor itself - see CodeTextEdit's multiple-cursor section.
        self._add(selection_menu, "Add Cursor Above\tCtrl+Alt+Up",
                  lambda: self._to_code("add_cursor_vertically", -1))
        self._add(selection_menu, "Add Cursor Below\tCtrl+Alt+Down",
                  lambda: self._to_code("add_cursor_vertically", 1))
        self._add(selection_menu, "Add Next Occurrence\tCtrl+D",
                  lambda: self._to_code("select_next_occurrence", False))
        self._add(selection_menu, "Select All Occurrences\tCtrl+Shift+L",
                  lambda: self._to_code("select_next_occurrence", True))
        self._add(selection_menu, "Cancel Multiple Cursors\tEsc",
                  lambda: self._to_code("clear_extra_cursors"), bind=False)
        selection_menu.addSeparator()
        self._add(selection_menu, "Find All Occurrences", self.find_occurrences)

        transform = selection_menu.addMenu("Transform")
        self._add(transform, "Sort Lines Ascending", lambda: self._to_code("sort_lines", False))
        self._add(transform, "Sort Lines Descending", lambda: self._to_code("sort_lines", True))
        self._add(transform, "Join Lines\tCtrl+Shift+J", self._join_lines)
        transform.addSeparator()
        self._add(transform, "Transform to Uppercase", lambda: self._to_code("transform_case", "upper"))
        self._add(transform, "Transform to Lowercase", lambda: self._to_code("transform_case", "lower"))
        self._add(transform, "Transform to Title Case", lambda: self._to_code("transform_case", "title"))
        transform.addSeparator()
        self._add(transform, "Trim Trailing Whitespace", lambda: self._to_code("trim_trailing"))
        self.action_trim = self._add(transform, "Trim Trailing Whitespace on Save",
                                     self._toggle_trim, check=True)
        self.action_trim.setChecked(self.trim_on_save)

        view_menu = self.menu_bar.addMenu("View")
        self._add(view_menu, "Command Palette...\tCtrl+Shift+P", self.show_palette)
        view_menu.addSeparator()

        appearance = view_menu.addMenu("Appearance")
        self._add(appearance, "Toggle Primary Side Bar\tCtrl+B",   self._toggle_sidebar)
        self._add(appearance, "Toggle Panel\tCtrl+J",              self._toggle_output)
        self._add(appearance, "Toggle Secondary Side Bar\tCtrl+Alt+B", self._toggle_secondary)
        appearance.addSeparator()
        self.action_minimap = self._add(appearance, "Minimap", self._toggle_minimap, check=True)
        self.action_minimap.setChecked(self.minimap_on)
        self.action_sticky = self._add(appearance, "Sticky Scroll", self._toggle_sticky, check=True)
        self.action_sticky.setChecked(self.sticky_on)
        self.action_wrap = self._add(appearance, "Word Wrap\tAlt+Z", self.toggle_word_wrap,
                                     check=True)
        view_menu.addSeparator()

        folding = view_menu.addMenu("Folding")
        self._add(folding, "Fold\tCtrl+Shift+[", lambda: self._to_code("fold_at_cursor", True))
        self._add(folding, "Unfold\tCtrl+Shift+]", lambda: self._to_code("fold_at_cursor", False))
        folding.addSeparator()
        self._add(folding, "Fold All", lambda: self._to_code("fold_all"))
        self._add(folding, "Unfold All", lambda: self._to_code("unfold_all"))
        view_menu.addSeparator()

        self._add(view_menu, "Explorer\tCtrl+Shift+E", lambda: self._show_view("explorer"))
        # Search is bound in Edit as Find in Files - the same command, so it is only SHOWN here
        self._add(view_menu, "Search\tCtrl+Shift+F", self.find_in_files, bind=False)
        self._add(view_menu, "Source Control\tCtrl+Shift+G", lambda: self._show_view("scm"))
        view_menu.addSeparator()

        self._add(view_menu, "Problems\tCtrl+Shift+M", lambda: self._show_panel(self.problems))
        self._add(view_menu, "Output\tCtrl+Shift+U", lambda: self._show_panel(self.output))
        self._add(view_menu, "Debug Console\tCtrl+Shift+Y", lambda: self._show_panel(self.console))

        go_menu = self.menu_bar.addMenu("Go")
        self._add(go_menu, "Back\tAlt+Left",     lambda: self.navigate(-1))
        self._add(go_menu, "Forward\tAlt+Right", lambda: self.navigate(1))
        self._add(go_menu, "Last Edit Location", self.goto_last_edit)
        go_menu.addSeparator()
        self.editors_menu = go_menu.addMenu("Switch Editor")
        # the fixed commands are built ONCE, here; _fill_editors only refreshes the file rows under
        # them, so their shortcuts are installed a single time
        self._add(self.editors_menu, "Next Editor\tCtrl+PageDown", lambda: self._step_editor(1))
        self._add(self.editors_menu, "Previous Editor\tCtrl+PageUp", lambda: self._step_editor(-1))
        self.editors_menu.addSeparator()
        self._add(self.editors_menu, "Next Used Editor", lambda: self._step_used_editor(1))
        self._add(self.editors_menu, "Previous Used Editor", lambda: self._step_used_editor(-1))
        self._editor_entries = []          # the file rows, rebuilt on every showing
        self.editors_menu.aboutToShow.connect(self._fill_editors)
        go_menu.addSeparator()
        self._add(go_menu, "Go to File...\tCtrl+P", self.goto_file)
        self._add(go_menu, "Go to Symbol in Editor...\tCtrl+Shift+O", self.goto_symbol)
        self._add(go_menu, "Go to Definition\tF12", self.goto_definition)
        go_menu.addSeparator()
        self._add(go_menu, "Go to Line/Column...\tCtrl+G", lambda: self._to_code("go_to_line"))
        self._add(go_menu, "Go to Bracket\tCtrl+Shift+Backslash", self.goto_bracket)
        go_menu.addSeparator()
        self._add(go_menu, "Next Problem\tF8", lambda: self.step_problem(1))
        self._add(go_menu, "Previous Problem\tShift+F8", lambda: self.step_problem(-1))
        go_menu.addSeparator()
        self._add(go_menu, "Next Change\tAlt+F3", lambda: self.step_change(1))
        self._add(go_menu, "Previous Change\tShift+Alt+F3", lambda: self.step_change(-1))

        # Go to Definition resolves inside the CURRENT FILE, off the same scope map the sticky band
        # uses. Declaration / Type Definition / Implementations / References are still absent: each
        # needs a resolver that follows imports and infers types across the workspace, and guessing
        # at "the first def with that name anywhere" would send you to the wrong file often enough
        # to be worse than not offering it.

        run_menu = self.menu_bar.addMenu("Run")
        self._add(run_menu, "Run Selection\tCtrl+Return", self._run_selection)
        self._add(run_menu, "Run Line",                   lambda: self._to_code("clicked_menu_execute_line"))
        self._add(run_menu, "Run All\tCtrl+Shift+Return", self._run_all)
        run_menu.addSeparator()
        self._add(run_menu, "Save to Shelf", lambda: self._to_code("clicked_save_to_shelf"))
        # No debugger: stepping, breakpoints and a call stack need a tracer driving maya's python
        # from another thread. Everything above runs in maya's interpreter directly.

        help_menu = self.menu_bar.addMenu("Help")
        self._add(help_menu, "Show All Commands\tCtrl+Shift+P", self.show_palette, bind=False)
        self._add(help_menu, "Keyboard Shortcuts Reference", self.show_shortcuts)
        help_menu.addSeparator()
        self._add(help_menu, "About", self.show_about)

    def _add(self, menu, label:str, slot, check:bool=False, bind:bool=True) -> object:
        """Add a menu entry; a "Name\tCtrl+X" label also installs the shortcut for real.

        The tab in the label only right-aligns a hint in the menu - it binds nothing. Without a live
        QShortcut the key falls straight through to Maya, where Ctrl+S saves the SCENE.

        `bind` False shows the hint but installs nothing: the same command can appear in two menus
        (VS Code lists Search under both Edit and View), and binding it twice makes Qt call the
        sequence ambiguous and fire neither.

        Returns the QAction, so a caller can enable or check it later.

        Returns:
            object: the created QAction.
        """
        sequence = label.partition("\t")[2]
        action = qt.QAction(label, self)
        if check:
            action.setCheckable(True)
            action.toggled.connect(lambda state: slot(state))
        else:
            action.triggered.connect(lambda *_: slot())
        menu.addAction(action)
        # a multi-stroke hint like "Ctrl+K W" is shown but not bound: QKeySequence would take it as a
        # chord, and a half-typed chord swallows the next key
        if bind and sequence and sequence not in self.NATIVE_KEYS and " " not in sequence:
            shortcut = qt.QShortcut(qt.QKeySequence(sequence), self)
            shortcut.setContext(qt.Qt.WindowShortcut)     # active whenever this window has focus
            # a checkable entry has to go through the ACTION, or the key would flip the state
            # without the tick in the menu ever following
            shortcut.activated.connect(action.toggle if check else (lambda *_: slot()))
            self._shortcuts.append(shortcut)
        return action

    # ---- File menu, kept in step with what is actually open

    def _alive(self) -> bool:
        """True while this instance's Qt side is still there.

        A menu's aboutToShow can reach a STALE editor: reloading window.py leaves the previous
        instance alive on the python side with its C++ widgets already destroyed, and touching one
        raises. The layout toggles carry the same guard for the same reason.

        Returns:
            bool: True while this instance and its tab widget are still valid.
        """
        return qt.is_valid(self) and qt.is_valid(self.tabs)

    def _sync_file_menu(self) -> None:
        """Grey out what cannot act right now, the way VS Code does.

        Returns:
            None.
        """
        if not self._alive():
            return
        try:
            page = self.current_page()
            path = getattr(self.tabs.currentWidget(), "file_path", None)
            modified = any(isinstance(page, EditorPage) and page.file_path and page.is_modified()
                           for page in self._all_pages())
            self.action_save.setEnabled(page is not None)
            self.action_save_as.setEnabled(page is not None)
            self.action_save_all.setEnabled(modified)
            self.action_revert.setEnabled(bool(page and page.file_path
                                               and os.path.isfile(page.file_path)))
            self.action_reveal.setEnabled(bool(path and os.path.exists(path)))
            self.action_copy_path.setEnabled(bool(path))
            self.action_close.setEnabled(self.tabs.count() > 0)
            self.action_reopen.setEnabled(bool(self._closed))
            self.action_autosave.blockSignals(True)      # reflecting state must not re-trigger it
            self.action_autosave.setChecked(self.autosave)
            self.action_autosave.blockSignals(False)
            self.recent_menu.setEnabled(bool(self.recent))
            self.folders_menu.setEnabled(bool(self.sidebar.workspace.roots))
        except RuntimeError:
            pass          # a widget died between the validity check and the call

    def _fill_recent(self) -> None:
        """Rebuild Open Recent from the stored list, dropping paths that no longer exist.

        Returns:
            None.
        """
        if not self._alive() or not qt.is_valid(self.recent_menu):
            return
        self.recent_menu.clear()
        self.recent = [p for p in self.recent if os.path.isfile(p)]
        for path in self.recent:
            action = self.recent_menu.addAction(os.path.basename(path))
            action.setToolTip(path)
            action.triggered.connect(lambda *_, p=path: self.open_file(p, preview=False))
        if self.recent:
            self.recent_menu.addSeparator()
            self.recent_menu.addAction("Clear Recently Opened", self._clear_recent)

    def _clear_recent(self) -> None:
        """Clear recent.

        Returns:
            None.
        """
        self.recent = []
        self._touch_session()

    def _fill_folders(self) -> None:
        """Rebuild the Remove Folder submenu from the workspace roots.

        Returns:
            None.
        """
        if not self._alive() or not qt.is_valid(self.folders_menu):
            return
        self.folders_menu.clear()
        for root in list(self.sidebar.workspace.roots):
            action = self.folders_menu.addAction(os.path.basename(root.rstrip("/\\")) or root)
            action.setToolTip(root)
            action.triggered.connect(lambda *_, r=root: self.sidebar.workspace.remove_folder(r))

    # ---- Edit menu commands

    def find_in_file(self, replace:bool=False) -> None:
        """Open the floating find panel on the current tab.

        Returns:
            None.
        """
        page = self.current_page()
        if page is not None:
            page.search.show_panel(replace=replace)

    def find_in_files(self) -> None:
        """Switch the side bar to Search and put the caret in its field.

        Returns:
            None.
        """
        self.sidebar.show_view("search")
        if not self.sidebar.isVisibleTo(self):
            self.sidebar.setVisible(True)
            self._refresh_layout_toggles()
        self.sidebar.search.field.setFocus()
        self.sidebar.search.field.selectAll()

    def find_occurrences(self) -> None:
        """Highlight every hit of the word under the caret, through the find panel.

        Returns:
            None.
        """
        page = self.current_page()
        if page is None:
            return
        cursor = page.code.textCursor()
        if not cursor.hasSelection():
            cursor.select(qt.QTextCursor.WordUnderCursor)
            page.code.setTextCursor(cursor)
        page.search.show_panel()
        page.search.word.setChecked(True)     # a bare word, not a substring of a longer one
        page.search.search()

    def delete_line(self) -> None:
        """Remove the lines the selection touches, in one undo step.

        Returns:
            None.
        """
        page = self.current_page()
        if page is None:
            return
        cursor = page.code.textCursor()
        document = page.code.document()
        first = document.findBlock(cursor.selectionStart())
        last = document.findBlock(cursor.selectionEnd())
        cursor.beginEditBlock()
        cursor.setPosition(first.position())
        cursor.setPosition(last.position() + last.length() - 1, qt.QTextCursor.KeepAnchor)
        cursor.removeSelectedText()
        cursor.deleteChar()                      # take the newline the lines left behind
        cursor.endEditBlock()
        page.code.setTextCursor(cursor)

    def _to_code(self, method:str, *args) -> None:
        """Call `method` on the current editor, if any.

        Returns:
            None.
        """
        page = self.current_page()
        if page is not None:
            getattr(page.code, method)(*args)    # move_line takes a direction, the rest take nothing

    def _join_lines(self) -> None:
        """Ctrl+Shift+J - VS Code's Join Lines. Ctrl+J is already Toggle Panel here.

        Returns:
            None.
        """
        self._to_code("join_lines")

    def _toggle_trim(self, on:bool=None) -> None:
        """Whether a save strips trailing whitespace first.

        Returns:
            None.
        """
        self.trim_on_save = (not self.trim_on_save) if on is None else bool(on)
        action = getattr(self, "action_trim", None)
        if action is not None and action.isChecked() != self.trim_on_save:
            action.setChecked(self.trim_on_save)
        self._touch_session()

    def step_change(self, step:int) -> None:
        """Alt+F3 - jump to the next git hunk in this file, and say so when there is none.

        Returns:
            None.
        """
        code = getattr(self.current_page(), "code", None)
        if code is not None and not code.step_change(step):
            self.status.set_message("No changes against HEAD in this file.")

    def goto_definition(self) -> None:
        """F12 - jump to where the symbol under the caret is defined, within this file.

        Returns:
            None.
        """
        self._symbol_action("go_to_definition", "Go to Definition")

    def rename_symbol(self) -> None:
        """F2 - rename the symbol under the caret everywhere in this file.

        Returns:
            None.
        """
        self._symbol_action("rename_symbol", "Rename Symbol")

    def _symbol_action(self, method:str, title:str) -> None:
        """Run a symbol command and SAY why when it declines - a key that silently does nothing

        Returns:
            None.
        reads as broken."""
        code = getattr(self.current_page(), "code", None)
        if code is None:
            return                               # an image or a diff tab has no symbols
        problem = getattr(code, method)()
        if problem:
            self.status.set_message(problem)

    # ---- side views that follow the current file

    # ---- Open Editors

    def _refresh_open_editors(self, *_) -> None:
        """Rebuild the OPEN EDITORS list from the tab bar it mirrors.

        Returns:
            None.
        """
        if not self.sidebar.open_section.is_expanded():
            return                               # collapsed: nothing to keep in step
        entries, self._open_pages, current = [], [], -1
        active_page = self.tabs.currentWidget()
        for group in self._groups:
            for index in range(group.count()):
                page = group.widget(index)
                path = getattr(page, "file_path", None) or ""
                name = os.path.basename(path) or group.tabText(index).lstrip("● ")
                modified = isinstance(page, EditorPage) and page.is_modified()
                if page is active_page:
                    current = len(self._open_pages)
                entries.append((len(self._open_pages), group.tabIcon(index), name,
                                os.path.basename(os.path.dirname(path)) if path else "",
                                modified, page is self._preview_page))
                self._open_pages.append(page)
        self.sidebar.open_editors.set_editors(entries, current)
        self.sidebar.open_section.refresh_height()   # the list just changed height

    def _close_other_tabs(self, keep:int) -> None:
        """Close every tab but `keep`, from the end so the earlier indexes stay valid.

        Returns:
            None.
        """
        page = self.tabs.widget(keep)
        for index in reversed(range(self.tabs.count())):
            if self.tabs.widget(index) is not page and self.tabs.widget(index) not in self._pinned:
                self.close_tab(index)

    def _tab_context_menu(self, position=None) -> None:
        """Right-click menu on a tab: close group, copy path variants, reveal, side-bar select, save.

        Args:
            position: (object): - local position on the tab bar where the menu was requested.

        Returns:
            None.
        """
        bar   = self.tabs.tabBar()
        index = bar.tabAt(position)
        if index < 0:
            return
        page    = self.tabs.widget(index)
        path    = getattr(page, "file_path", None)
        count   = self.tabs.count()
        on_disk = bool(path) and os.path.exists(path)

        editable = isinstance(page, EditorPage)

        menu = qt.QMenu(self)
        if self._preview_page is page:                                          # a preview (italic) tab
            menu.addAction("Keep Open", lambda: self._keep_tab_open(index))
            menu.addSeparator()
        menu.addAction("Unpin" if self._is_pinned(page) else "Pin", lambda: self._toggle_pin(index))
        menu.addSeparator()
        menu.addAction("Close", lambda: self.close_tab(index))
        action = menu.addAction("Close Other Tabs", lambda: self._close_other_tabs(index))
        action.setEnabled(count > 1)
        action = menu.addAction("Close Tabs to the Left", lambda: self._close_tabs_to_left(index))
        action.setEnabled(index > 0)
        action = menu.addAction("Close Tabs to the Right", lambda: self._close_tabs_to_right(index))
        action.setEnabled(index < count - 1)
        menu.addAction("Close Saved Tabs", self._close_saved_tabs)
        menu.addAction("Close All Tabs", self.close_all_tabs)
        menu.addSeparator()
        action = menu.addAction("Split Right", lambda: self._split_right(index))
        action.setEnabled(count > 1 or len(self._groups) > 1)
        menu.addSeparator()
        for label, value in (("Copy Path", os.path.normpath(path) if path else None),
                             ("Copy Relative Path", self._relative_path(path)),
                             ("Copy File Name", os.path.basename(path) if path else None),
                             ("Copy Directory Path", os.path.dirname(path) if path else None)):
            action = menu.addAction(label, lambda text=value: compat.copy(text))
            action.setEnabled(bool(value))
        menu.addSeparator()
        action = menu.addAction("Reveal in File Explorer", lambda: self.reveal_path(path))
        action.setEnabled(on_disk)
        action = menu.addAction("Select in Side Bar", lambda: self._select_in_sidebar(path))
        action.setEnabled(on_disk)
        menu.addSeparator()
        action = menu.addAction("Compare with Saved", lambda: self._compare_with_saved(index))
        action.setEnabled(editable and on_disk)
        action = menu.addAction("Save", lambda: (self.tabs.setCurrentIndex(index), self.save_file()))
        action.setEnabled(editable and page.is_modified())
        menu.addAction("Save All", self.save_all)
        action = menu.addAction("Reopen Closed Tab", self.reopen_closed)
        action.setEnabled(bool(self._closed))
        menu.exec_(bar.mapToGlobal(position))

    def _close_tabs_to_right(self, index:int=None) -> None:
        """Close every tab positioned after `index`, from the end so indexes stay valid.

        Args:
            index: (int): - the tab whose right-hand neighbours are closed.

        Returns:
            None.
        """
        for other in reversed(range(index + 1, self.tabs.count())):
            if self.tabs.widget(other) not in self._pinned:
                self.close_tab(other)

    def _close_tabs_to_left(self, index:int=None) -> None:
        """Close every tab positioned before `index`, from the end so `index` stays valid.

        Args:
            index: (int): - the tab whose left-hand neighbours are closed.

        Returns:
            None.
        """
        for other in reversed(range(index)):
            if self.tabs.widget(other) not in self._pinned:
                self.close_tab(other)

    def _keep_tab_open(self, index:int=None) -> None:
        """Promote a preview (italic) tab to a permanent one, like a double click does.

        Args:
            index: (int): - the tab to keep open.

        Returns:
            None.
        """
        if self._preview_page is self.tabs.widget(index):
            self._preview_page = None
            self.tabs.tabBar().update()

    def _compare_with_saved(self, index:int=None) -> None:
        """Open a side-by-side diff of a tab's editor content against its file on disk.

        Args:
            index: (int): - the editor tab to compare.

        Returns:
            None.
        """
        page = self.tabs.widget(index)
        path = getattr(page, "file_path", None)
        if not (isinstance(page, EditorPage) and path and os.path.isfile(path)):
            return
        old_text, new_text = compat.folder.read(path), page.code.toPlainText()
        label = "%s (Saved)" % os.path.basename(path)
        for other in range(self.tabs.count()):
            diff = self.tabs.widget(other)
            if isinstance(diff, DiffPage) and diff.file_path == path and diff.name() == label:
                self.tabs.setCurrentIndex(other)
                return
        self._place_tab(DiffPage(path, old_text, new_text, title=label), file_icon(path), label, preview=True)

    def _close_saved_tabs(self) -> None:
        """Close every tab that has no unsaved changes.

        Returns:
            None.
        """
        for index in reversed(range(self.tabs.count())):
            page = self.tabs.widget(index)
            if page not in self._pinned and not (isinstance(page, EditorPage) and page.is_modified()):
                self.close_tab(index)

    def _relative_path(self, path:str=None) -> str:
        """Return `path` relative to its nearest workspace root, or None when it has none.

        Args:
            path: (str): - the absolute file path to shorten.

        Returns:
            str: the path relative to a workspace root (forward slashes), or None.
        """
        if not path:
            return None
        for root in self.sidebar.workspace.roots:
            try:
                relative = os.path.relpath(path, root)
            except ValueError:
                continue
            if not relative.startswith(".."):
                return relative.replace("\\", "/")
        return None

    def _select_in_sidebar(self, path:str=None) -> None:
        """Show the Explorer view and select `path` in the workspace tree.

        Args:
            path: (str): - the file path to reveal in the side bar.

        Returns:
            None.
        """
        if not path:
            return
        try:
            self.sidebar.select("explorer")
        except Exception:
            pass
        self.sidebar.workspace._select_path(path)

    def _first_scan(self) -> None:
        """The initial git read, once the window exists. Runs in both chrome modes.

        The embedded panel skipped this before, on the grounds that its side bar is hidden - but the
        tree's own change markers come from the same scan, and those ARE visible there.

        Returns:
            None.
        """
        if not self._alive():
            return
        try:
            self.sidebar.refresh_vcs()
        except Exception:
            pass                             # no git, no repository, or none on PATH: not an error

    def _refresh_side_views(self, *_) -> None:
        """Point Outline, Timeline and the status bar at whatever tab is in front.

        Nothing is computed for a section that is COLLAPSED. Both are collapsed by default, and the
        timeline's `git log --follow` alone took a fifth of a second per tab click to fill a panel
        nobody could see. Each one loads when it opens (CollapsibleSection.on_expand) and from then
        on follows the current file.

        Returns:
            None.
        """
        page = self.tabs.currentWidget()
        path = getattr(page, "file_path", None) or ""
        source = page.code.toPlainText() if isinstance(page, EditorPage) else ""

        self.sidebar.timeline_section.set_suffix(os.path.basename(path))
        self.status.set_file(path, source)                 # cheap: no process spawned

        if self.sidebar.outline_section.is_expanded():
            self.sidebar.outline.set_source(path, source)
        self._refresh_variables()                          # cheap, and the namespace is shared across tabs
        if path != getattr(self, "_side_path", None):
            # the folder changed, so the branch may have: this is the only place git is asked
            self._side_path = path
            self.status.set_branch(os.path.dirname(path) if path else "")
            if self.sidebar.timeline_section.is_expanded():
                self.sidebar.timeline.set_file(path)
            self._refresh_changes(page)
        self._follow_caret()

    def _refresh_changes(self, page=None) -> None:
        """Ask git what changed in this file and hand the hunks to its gutter.

        Only on a tab change or a save, never on the typing debounce: `git show` spawns a process,
        and doing that per keystroke is what made selecting a script feel slow before.

        Returns:
            None.
        """
        page = page if page is not None else self.tabs.currentWidget()
        if not isinstance(page, EditorPage):
            return
        path = getattr(page, "file_path", None) or ""
        if not path or not os.path.isfile(path):
            page.code.set_changes([])
            return
        root = vcs.repo_root_cached(os.path.dirname(path))
        if not root:
            page.code.set_changes([])
            return
        old = vcs.show(root, path, "HEAD")
        page.code.set_changes(diff_hunks(old, page.code.toPlainText()) if old else [])

    def _load_outline(self) -> None:
        """Fill the outline when its section is opened.

        Returns:
            None.
        """
        page = self.tabs.currentWidget()
        self.sidebar.outline.set_source(getattr(page, "file_path", None) or "",
                                        page.code.toPlainText() if isinstance(page, EditorPage) else "")
        self._follow_caret()

    def _load_timeline(self) -> None:
        """Fill the timeline when its section is opened.

        Returns:
            None.
        """
        self.sidebar.timeline.set_file(getattr(self.tabs.currentWidget(), "file_path", None) or "")

    def _follow_caret(self, *_) -> None:
        """Keep the outline's highlight, and the status bar's position, on the caret.

        Returns:
            None.
        """
        page = self.current_page()
        if page is None:
            return
        cursor = page.code.textCursor()
        self.status.set_position(cursor.blockNumber() + 1, cursor.columnNumber() + 1)
        if self.sidebar.outline_section.is_expanded():
            self.sidebar.outline.follow(cursor.blockNumber() + 1)
        self._follow_quick_help(page)

    def _follow_quick_help(self, page) -> None:
        """Point QUICK HELP at whatever call the caret is inside.

        Nothing is computed while the panel is hidden - it is hidden by default, and resolving a name
        and reading maya's help on every caret move to fill something nobody can see is exactly the
        kind of cost that made this editor feel slow before.

        Returns:
            None.
        """
        if not self.secondary.isVisibleTo(self):
            return
        context = page.code.call_context()
        if context is None:
            self.secondary.clear()
            return
        name, argument = context
        info = signature.describe(name, page.code.resolve(name))
        self.secondary.set_call(name, info, argument, self.theme)

    def _goto_line(self, line:int) -> None:
        """Jump the current tab to `line` (the outline clicked).

        Returns:
            None.
        """
        page = self.current_page()
        if page is not None:
            self._goto_error(page.file_path or "", int(line))

    def open_revision(self, path:str, root:str, sha:str) -> None:
        """Open a diff of `path` at commit `sha` against what is on disk now.

        Returns:
            None.
        """
        if not path or not sha:
            return
        try:
            old = vcs.show(root, path, sha)
        except Exception as exception:
            compat.message.critical(title="Timeline", buttons=["Close"],
                                    message_text="Could not read that revision.",
                                    informative_text=str(exception), parent=self)
            return
        new = compat.folder.read(path) if os.path.isfile(path) else ""
        label = "%s (%s)" % (os.path.basename(path), sha[:7])
        page = DiffPage(path, old, new, title=label)
        page.vcs_root, page.vcs_staged = root, False
        self._place_tab(page, file_icon(path), label, preview=True)

    def open_commit(self, path:str, root:str, sha:str) -> None:
        """Open the whole commit `sha` as a unified patch.

        Returns:
            None.
        """
        if not sha:
            return
        text = vcs.patch(root or os.path.dirname(path or ""), sha)
        if not text.strip():
            compat.message.warning(title="Open Commit", buttons=["Close"],
                                   message_text="Nothing to show for %s." % sha[:7], parent=self)
            return
        label = "Commit %s" % sha[:7]
        self._place_tab(PatchPage(label, text, path), _icon("file.png"), label, preview=True)

    def _update_menu_targets(self, *_) -> None:
        """Update menu targets.

        Returns:
            None.
        """
        self._show_welcome_if_empty()

    # ------------------------------------------------------------------ slots

    def _refresh_variables(self) -> None:
        """Repopulate the Variables panel from the shared run namespace.

        Returns:
            None.
        """
        panel = getattr(self.sidebar, "variables", None)
        if panel is not None and qt.is_valid(panel):
            panel.refresh(self.namespace)

    def _print_variable(self, name:str=None) -> None:
        """Echo a namespace global's full repr to the output (Variables panel double click).

        Args:
            name: (str): - the global's name.

        Returns:
            None.
        """
        if not name or name not in self.namespace:
            return
        try:
            text = repr(self.namespace[name])
        except Exception:
            text = "<unrepresentable>"
        sys.stdout.write("%s = %s\n" % (name, text))

    def _run_selection(self) -> None:
        """Run selection.

        Returns:
            None.
        """
        page = self.current_page()
        if page is None:
            return
        if page.code.textCursor().hasSelection():
            page.code.run()
        else:
            page.code.execute(page.code.toPlainText())
        self._refresh_variables()

    def _run_all(self) -> None:
        """Run all.

        Returns:
            None.
        """
        page = self.current_page()
        if page is not None:
            page.code.execute(page.code.toPlainText())
            self._refresh_variables()

    # ------------------------------------------------------------------ layout toggles

    def _build_layout_toggles(self) -> None:
        """Put the three VS Code layout toggles as icons on the right end of the menu bar.

        Returns:
            None.
        """
        # keep a python reference: setCornerWidget does not reliably transfer ownership in PySide, and a
        # garbage-collected corner takes its buttons with it (invisible toggles, then RuntimeError)
        corner = qt.QWidget(self.menu_bar)
        self._corner = corner
        # without this the global "QWidget { background: #121314 }" rule paints a darker patch here;
        # target by objectName so the rule does not cascade onto the buttons
        corner.setObjectName("codeMenuCorner")
        corner.setAttribute(qt.Qt.WA_StyledBackground, True)
        row = qt.QHBoxLayout(corner)
        row.setContentsMargins(0, 0, qt.px(6), 0)
        row.setSpacing(qt.px(2))

        self._layout_buttons = []
        for base, tip, slot in (
            ("layout_left",  "Toggle Primary Side Bar",   self._toggle_sidebar),
            ("layout_panel", "Toggle Panel",              self._toggle_output),
            ("layout_right", "Toggle Secondary Side Bar", self._toggle_secondary),
        ):
            button = qt.QToolButton()
            button.setAutoRaise(True)
            button.setToolTip(tip)
            button.setIconSize(qt.QSize(qt.px(17), qt.px(17)))
            # no background in any state: the icon itself brightens on hover instead
            button.setObjectName("codeLayoutToggle")
            button.clicked.connect(slot)
            row.addWidget(button)
            self._layout_buttons.append((button, base))

        self.menu_bar.setCornerWidget(corner)
        # Ctrl+B / Ctrl+J / Ctrl+Alt+B are NOT bound here: _add already binds every sequence printed in
        # the menus. Two QShortcuts on the same window with the same sequence make Qt call it ambiguous
        # and fire neither, so the toggles would silently stop responding to the keyboard.
        self._refresh_layout_toggles()

    def _refresh_layout_toggles(self) -> None:
        """Swap each toggle's icon to reflect whether its panel is currently shown.

        Returns:
            None.
        """
        buttons = [(b, base) for b, base in getattr(self, "_layout_buttons", []) if qt.is_valid(b)]
        if not buttons:
            return        # corner bar gone (window closed, or a stale instance after a reload)
        try:
            # isVisibleTo(): reports the intended state even before the window itself is shown
            state = {
                "layout_left":  self.sidebar.isVisibleTo(self),
                "layout_panel": self.panel.isVisibleTo(self),
                "layout_right": self.secondary.isVisibleTo(self),
            }
            for button, base in buttons:
                button.setIcon(_icon("%s_%s.png" % (base, "on" if state[base] else "off")))
        except RuntimeError:
            pass          # a widget died between the validity check and the call

    def open_diff(self, path:str, root:str, staged:bool, preview:bool=False) -> None:
        """Open a side-by-side diff tab for a source-control entry.

        Staged entries compare HEAD against the index; unstaged ones compare the index against the file
        on disk, which is exactly what each group in the panel represents.

        Returns:
            None.
        """
        root = root or os.path.dirname(path)
        try:
            if staged:
                old_text, new_text = vcs.show(root, path, "HEAD"), vcs.show(root, path, "")
                label = "%s (Staged)" % os.path.basename(path)
            else:
                old_text = vcs.show(root, path, "")
                new_text = compat.folder.read(path) if os.path.isfile(path) else ""
                label = "%s (Working Tree)" % os.path.basename(path)
        except Exception as exception:
            compat.message.critical(title="Diff", buttons=["Close"],
                                    message_text="Could not read the revisions.",
                                    informative_text=str(exception), parent=self)
            return

        # focus an already-open diff for the same file instead of stacking duplicates
        for index in range(self.tabs.count()):
            page = self.tabs.widget(index)
            if isinstance(page, DiffPage) and page.file_path == path and page.name() == label:
                self.tabs.setCurrentIndex(index)
                if not preview and self._preview_page is page:
                    self._preview_page = None        # a double click pins it
                    self.tabs.tabBar().update()
                return

        page = DiffPage(path, old_text, new_text, title=label)
        page.vcs_root, page.vcs_staged = root, staged      # what a session restore rebuilds it from
        self._place_tab(page, file_icon(path), label, preview)

    def _open_sources(self) -> list:
        """(path, source) for every open editor tab, for the Problems panel to compile.

        Returns:
            list: the (path, source) pairs for every open editor tab.
        """
        sources = []
        for page in self._all_pages():
            if isinstance(page, EditorPage):
                sources.append((page.file_path or "untitled.py", page.code.toPlainText()))
        return sources

    def _panel_changed(self, index:int) -> None:
        """Refresh the tab that just became visible, and show its own controls only when relevant.

        Returns:
            None.
        """
        current = self.panel.widget(index)
        if current is self.problems:
            self.problems.refresh()
        corner = self.panel.cornerWidget(qt.Qt.TopRightCorner)
        if corner is not None and qt.is_valid(corner):
            corner.setVisible(current is self.output)     # .py/.mel and echo only apply to the log

    def _problems_count_changed(self, count:int) -> None:
        """Show the problem count as a blue bubble on the PROBLEMS tab, like VS Code does.

        Returns:
            None.
        """
        index = self.panel.indexOf(self.problems)
        bar = self.panel.tabBar()
        if index >= 0 and hasattr(bar, "set_badge"):
            bar.set_badge(index, count)

    def _activity_view_changed(self, name:str) -> None:
        """Switch the side bar to the picked view, revealing it if it was hidden.

        Returns:
            None.
        """
        self.sidebar.show_view(name)
        if not self.sidebar.isVisibleTo(self):
            self.sidebar.setVisible(True)
            self._refresh_layout_toggles()

    PANEL_KEYS = ("sidebar", "panel", "secondary")
    PANEL_MIN = 120                    # what a reopened panel falls back to, having never been sized

    def _panel_named(self, key:str) -> object:
        """The panel widget named `key`.

        Returns:
            object: the panel widget for `key`, or None when unknown.
        """
        return {"sidebar": self.sidebar, "panel": self.panel, "secondary": self.secondary}.get(key)

    def _panel_extent(self, panel) -> int:
        """How wide (or tall) `panel` is along its splitter's axis.

        Returns:
            int: the panel's width or height along its splitter's axis.
        """
        splitter = panel.parent()
        if not isinstance(splitter, qt.QSplitter):
            return 0
        return panel.width() if splitter.orientation() == qt.Qt.Horizontal else panel.height()

    def _toggle_panel(self, panel) -> None:
        """Flip a panel's visibility, keeping the size it was dragged to.

        A QSplitter does not hand a hidden child its old size back: reopening gave it whatever was
        left over, so every toggle quietly undid the width you had set. The extent is remembered on
        the way out and put back on the way in.

        Returns:
            None.
        """
        if not qt.is_valid(panel):
            return
        showing = not panel.isVisibleTo(self)
        if not showing:
            extent = self._panel_extent(panel)
            if extent > 0:
                self._panel_sizes[panel] = extent
        panel.setVisible(showing)
        if showing:
            # after the layout has taken the newly shown widget into account, not before
            qt.QTimer.singleShot(0, lambda p=panel: self._restore_panel_extent(p))
        self._refresh_layout_toggles()

    def _restore_panel_extent(self, panel) -> None:
        """Give `panel` back the extent it had, taking it from its largest sibling.

        Returns:
            None.
        """
        if not qt.is_valid(panel) or not panel.isVisibleTo(self):
            return
        splitter = panel.parent()
        if not isinstance(splitter, qt.QSplitter):
            return
        wanted = self._panel_sizes.get(panel) or qt.px(self.PANEL_MIN)
        sizes = splitter.sizes()
        index = splitter.indexOf(panel)
        if index < 0 or sizes[index] == wanted:
            return
        donor = max((i for i in range(len(sizes)) if i != index),
                    key=lambda i: sizes[i], default=None)
        if donor is None:
            return
        delta = wanted - sizes[index]
        if sizes[donor] - delta < qt.px(self.PANEL_MIN):
            return                               # the editor would be squeezed: leave it alone
        sizes[index], sizes[donor] = wanted, sizes[donor] - delta
        splitter.setSizes(sizes)

    def _toggle_sidebar(self) -> None:
        """Show/hide the primary side bar (Workspace / Outline / Timeline).

        Returns:
            None.
        """
        self._toggle_panel(self.sidebar)

    def _toggle_output(self) -> None:
        """Show/hide the bottom panel (Output / Problems / Debug Console).

        Returns:
            None.
        """
        self._toggle_panel(self.panel)

    def _toggle_secondary(self) -> None:
        """Show/hide the secondary side bar on the right.

        Returns:
            None.
        """
        self._toggle_panel(self.secondary)
        self._sync_argument_completion()

    def _sync_argument_completion(self) -> None:
        """Let the completion popup offer a call's parameters while QUICK HELP is open.

        Tied to that panel on purpose: it is where those names come from, and it is the switch the
        user already reaches for when they want to see what a call takes.

        Returns:
            None.
        """
        on = self.secondary.isVisibleTo(self)
        for page in self._all_pages():
            code = getattr(page, "code", None)
            if code is not None:
                code.argument_completion = on

    # ---- Go menu commands

    def _mark_location(self) -> None:
        """Remember where we are, so Back can return to it.

        Returns:
            None.
        """
        page = self.current_page()
        if page is None or not page.file_path:
            return
        spot = (page.file_path, page.code.textCursor().blockNumber() + 1)
        if self._history and self._history[self._history_at] == spot:
            return
        del self._history[self._history_at + 1:]     # a new jump drops the forward branch
        self._history.append(spot)
        del self._history[:-50]
        self._history_at = len(self._history) - 1

    def navigate(self, step:int) -> None:
        """Walk the jump history backwards or forwards.

        Returns:
            None.
        """
        target = self._history_at + step
        if not (0 <= target < len(self._history)):
            return
        self._history_at = target
        path, line = self._history[target]
        self._goto_error(path, line)

    def goto_last_edit(self) -> None:
        """Jump to where the last edit happened.

        Returns:
            None.
        """
        if self._last_edit:
            self._goto_error(*self._last_edit)

    def _touch_mru(self, *_) -> None:
        """Move the current tab to the front of the most-recently-used order.

        Returns:
            None.
        """
        page = self.current_page()
        self._mru = [p for p in self._mru if p is not page and qt.is_valid(p)]
        if page is not None:
            self._mru.insert(0, page)

    def _step_editor(self, step:int) -> None:
        """Next / Previous editor in TAB order, wrapping round the ends.

        Returns:
            None.
        """
        count = self.tabs.count()
        if count > 1:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + step) % count)

    def _step_used_editor(self, step:int) -> None:
        """Next / Previous editor in the order they were last looked at.

        Kept as widgets rather than indices: closing a tab renumbers every index after it, and the
        history would then walk to the wrong files.

        Returns:
            None.
        """
        self._mru = [p for p in self._mru if qt.is_valid(p) and self.tabs.indexOf(p) >= 0]
        if len(self._mru) < 2:
            return
        page = self.current_page()
        at = self._mru.index(page) if page in self._mru else 0
        target = self._mru[(at + step) % len(self._mru)]
        self.tabs.setCurrentIndex(self.tabs.indexOf(target))

    def _fill_editors(self) -> None:
        """Re-list the open tabs under Switch Editor's fixed commands.

        Only the FILE rows are rebuilt. The commands above them are built once, in _build_menus:
        rebuilt here they would install their shortcut again on every showing, and two live
        QShortcuts on one sequence fire neither.

        Returns:
            None.
        """
        if not self._alive() or not qt.is_valid(self.editors_menu):
            return
        for action in getattr(self, "_editor_entries", []):
            if qt.is_valid(action):
                self.editors_menu.removeAction(action)
        self._editor_entries = []
        pages = self._all_pages()
        if pages:
            self._editor_entries.append(self.editors_menu.addSeparator())
        for group in self._groups:
            for index in range(group.count()):
                page = group.widget(index)
                action = self.editors_menu.addAction(group.tabText(index))
                action.setIcon(group.tabIcon(index))
                action.triggered.connect(lambda *_, p=page: self._focus_page(p))
                self._editor_entries.append(action)

    def goto_file(self) -> None:
        """Quick-open: every file under the workspace roots, filtered as you type.

        Returns:
            None.
        """
        entries, seen = [], set()
        for root in self.sidebar.workspace.roots:
            for base, folders, names in os.walk(root):
                folders[:] = [f for f in folders if f not in SearchPanel.SKIP_DIRS]
                for name in names:
                    path = os.path.join(base, name)
                    if path in seen:
                        continue
                    seen.add(path)
                    entries.append((name, os.path.relpath(base, root),
                                    lambda p=path: self.open_file(p, preview=False)))
                if len(entries) > 6000:              # a deep tree must not freeze the window
                    break
        self.palette_widget.open_entries(sorted(entries, key=lambda e: e[0].lower()),
                                         "Go to file")

    def goto_symbol(self) -> None:
        """Quick-open the definitions of the current file, read straight from its syntax tree.

        Returns:
            None.
        """
        page = self.current_page()
        if page is None:
            return
        try:
            tree = ast.parse(page.code.toPlainText())
        except SyntaxError as error:
            self._goto_error(page.file_path or "", error.lineno or 1)
            return

        entries = []

        def walk(node, prefix="") -> None:
            """Recurse the AST, collecting a Go-to-symbol entry per class and function.

            Returns:
                None.
            """
            for child in getattr(node, "body", []):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    kind = "class" if isinstance(child, ast.ClassDef) else "def"
                    name = prefix + child.name
                    entries.append(("%s %s" % (kind, name), "line %d" % child.lineno,
                                    lambda l=child.lineno: self._goto_error(page.file_path or "", l)))
                    walk(child, name + ".")

        walk(tree)
        self.palette_widget.open_entries(entries, "Go to symbol")

    def goto_bracket(self) -> None:
        """Jump to the bracket matching the one at the caret.

        Returns:
            None.
        """
        page = self.current_page()
        if page is None:
            return
        cursor = page.code.textCursor()
        span = editor_module._bracket_span(page.code.toPlainText(),
                                           cursor.selectionStart(), cursor.selectionEnd())
        if not span:
            return
        position = cursor.position()
        cursor.setPosition(span[1] if abs(position - span[0]) < abs(position - span[1]) else span[0])
        page.code.setTextCursor(cursor)
        page.code.ensureCursorVisible()

    def step_problem(self, step:int) -> None:
        """Walk through the problems the checker found, in order.

        Returns:
            None.
        """
        rows = []
        for index in range(self.problems.tree.topLevelItemCount()):
            parent = self.problems.tree.topLevelItem(index)
            for child_index in range(parent.childCount()):
                child = parent.child(child_index)
                if child.data(0, _PATH_ROLE):
                    rows.append(child)
        if not rows:
            return
        current = self.problems.tree.currentItem()
        at = rows.index(current) if current in rows else -1 if step > 0 else 0
        item = rows[(at + step) % len(rows)]
        self.problems.tree.setCurrentItem(item)
        self._show_panel(self.problems)
        self._goto_error(item.data(0, _PATH_ROLE), int(item.data(0, _LINE_ROLE) or 1))

    # ---- Help menu commands

    def show_shortcuts(self) -> None:
        """List every shortcut the menus declare, and whether it is bound here.

        Returns:
            None.
        """
        lines = []
        for top in self.menu_bar.actions():
            menu = top.menu()
            if menu is None:
                continue
            rows = []
            for action in menu.actions():
                label, _, sequence = action.text().partition("\t")
                if sequence:
                    rows.append("    %-30s %s" % (label.replace("&", ""), sequence))
            if rows:
                lines.append(top.text().replace("&", ""))
                lines.extend(rows)
                lines.append("")
        note = ("Undo, Redo, Cut, Copy, Paste and Select All are handled by the focused widget\n"
                "rather than by the window, so they work in every field, not only in the code.")
        compat.message.warning(title="Keyboard Shortcuts", buttons=["Close"],
                               message_text="\n".join(lines).strip(), informative_text=note, parent=self)

    def show_about(self) -> None:
        """Version and where this editor keeps its state.

        Returns:
            None.
        """
        compat.message.warning(
            title="About", buttons=["Close"],
            message_text="%s  %s" % (Editor.title, Editor.version),
            informative_text=("A self-contained code editor for Maya.\n\n"
                              "Session and backups: %s\nQt binding: %s"
                              % (self._session.folder, qt.__qt__)), parent=self)

    # ---- View menu commands

    def _show_view(self, name:str) -> None:
        """Bring a side bar view forward, revealing the side bar when it is hidden.

        Returns:
            None.
        """
        self.sidebar.show_view(name)
        if not self.sidebar.isVisibleTo(self):
            self.sidebar.setVisible(True)
            self._refresh_layout_toggles()

    def _show_panel(self, widget) -> None:
        """Bring a bottom panel tab forward, revealing the panel when it is hidden.

        Returns:
            None.
        """
        index = self.panel.indexOf(widget)
        if index < 0:
            return
        if not self.panel.isVisibleTo(self):
            self.panel.setVisible(True)
            self._refresh_layout_toggles()
        self.panel.setCurrentIndex(index)

    def toggle_word_wrap(self, on:bool=None) -> None:
        """Wrap long lines, on every open tab and on the ones opened afterwards.

        Returns:
            None.
        """
        self.word_wrap = (not self.word_wrap) if on is None else bool(on)
        mode = qt.QPlainTextEdit.WidgetWidth if self.word_wrap else qt.QPlainTextEdit.NoWrap
        for page in self._all_pages():
            code = getattr(page, "code", None)
            if code is not None:
                code.setLineWrapMode(mode)
        self._touch_session()

    def show_palette(self) -> None:
        """Open the command palette, or close it when it is already up.

        Returns:
            None.
        """
        self.palette_widget.toggle()

    def _palette_actions(self) -> list:
        """Every leaf action of the menu bar, submenus included.

        Returns:
            list: every leaf QAction of the menu bar, submenus included.
        """
        found, seen = [], set()

        def walk(menu) -> None:
            """Recurse into `menu`, collecting its leaf actions.

            Returns:
                None.
            """
            for action in menu.actions():
                if action.isSeparator():
                    continue
                if action.menu() is not None:
                    walk(action.menu())
                elif id(action) not in seen:
                    seen.add(id(action))
                    found.append(action)

        self._sync_file_menu()               # so a disabled command is not offered as runnable
        for action in self.menu_bar.actions():
            if action.menu() is not None:
                walk(action.menu())
        return found

    def _toggle_sticky(self, on:bool=None) -> None:
        """Pin or unpin the enclosing class/def band at the top of every open editor tab.

        Returns:
            None.
        """
        self.sticky_on = (not self.sticky_on) if on is None else bool(on)
        action = getattr(self, "action_sticky", None)
        if action is not None and action.isChecked() != self.sticky_on:
            action.setChecked(self.sticky_on)    # keep the View tick and the gear menu agreeing
        for page in self._all_pages():
            code = getattr(page, "code", None)
            if code is None:
                continue
            code.sticky_scroll = self.sticky_on
            code._rebuild_sticky()               # switched on, the map has to be read for the first time
            code.viewport().update()
        self._touch_session()

    def _toggle_minimap(self, on:bool=None) -> None:
        """Show or hide the minimap strip on every open editor tab.

        Returns:
            None.
        """
        self.minimap_on = (not self.minimap_on) if on is None else bool(on)
        action = getattr(self, "action_minimap", None)
        if action is not None and action.isChecked() != self.minimap_on:
            action.setChecked(self.minimap_on)   # keep the View tick and the gear menu agreeing
        for page in self._all_pages():
            strip = getattr(getattr(page, "code", None), "minimap", None)
            if strip is None:
                continue
            strip.setVisible(self.minimap_on)
            page.code.update_number_width(0)     # reclaim / give back the right viewport margin
        self._touch_session()

    # ---- Manage (the gear at the foot of the activity bar)

    def reset_placement(self) -> None:
        """Throw away maya's record of where this panel was docked, and reopen it floating.

        Imported here rather than at the top: main imports window, so the other direction can only
        happen at call time.

        Returns:
            None.
        """
        from . import main
        qt.QTimer.singleShot(0, main.reset)      # not while this menu's own click is being handled

    def show_manage(self) -> None:
        """The gear menu: the handful of settings this editor actually has.

        Returns:
            None.
        """
        menu = qt.QMenu(self)
        menu.addAction("Command Palette...\tCtrl+Shift+P", self.show_palette)
        menu.addSeparator()

        themes = menu.addMenu("Themes")
        for name, label in (("vscode", "Dark (VS Code)"), ("the host", "Dark (the host)")):
            action = themes.addAction(label)
            action.setCheckable(True)
            action.setChecked(self.theme_name == name)
            action.triggered.connect(lambda *_, n=name: self.set_theme(n))

        for label, state, slot in (("Word Wrap", self.word_wrap, self.toggle_word_wrap),
                                   ("Minimap", self.minimap_on, self._toggle_minimap),
                                   ("Sticky Scroll", self.sticky_on, self._toggle_sticky),
                                   ("Auto Save", self.autosave, self.toggle_autosave)):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(state)
            action.toggled.connect(slot)

        menu.addSeparator()
        if self.chrome:
            menu.addAction("Reset Window Placement", self.reset_placement)
        menu.addAction("Keyboard Shortcuts", self.show_shortcuts)
        menu.addAction("About", self.show_about)

        button = self.activity_bar.manage
        point = button.mapToGlobal(qt.QPoint(button.width(), 0))
        menu.exec_(point) if hasattr(menu, "exec_") else menu.exec(point)

    def set_theme(self, name:str) -> None:
        """Re-skin the whole editor at runtime.

        Three things have to move together: the stylesheet, the colours the delegates paint by hand,
        and the surface each open CodeTextEdit was given at construction. Missing the last one leaves
        every open tab on the previous background until it is closed and reopened.

        Returns:
            None.
        """
        if name == self.theme_name:
            return
        self.theme_name = name
        self.theme = qt.theme(name)
        self.setStyleSheet(qt.stylesheet(name))
        self._apply_theme()
        for page in self._all_pages():
            code = getattr(page, "code", None)
            if code is not None:
                code.surface = self.theme["editor"]
                code.palette_theme = self.theme     # the completion popup repaints from this
                code._apply_editor_style()
        self._touch_session()

    def _goto_error(self, file_path:str, line:int) -> None:
        """Open the file from a traceback and jump to the line.

        Returns:
            None.
        """
        if file_path and os.path.isfile(file_path):
            self.open_file(file_path)
        page = self.current_page()
        if page is None:
            return
        try:
            block  = page.code.document().findBlockByNumber(max(0, int(line) - 1))
            cursor = page.code.textCursor()
            cursor.setPosition(block.position())
            page.code.setTextCursor(cursor)
            page.code.centerCursor()
            page.code.setFocus()
        except Exception:
            pass
