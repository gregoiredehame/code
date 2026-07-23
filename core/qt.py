"""
KATA. (c)

Author: Gregoire Dehame
Created: Jul 21, 2026
Module: ui.code_editor.core.qt
Execute: from kata.ui.code_editor.core import qt

Self-contained Qt binding wrapper for the code editor package, so `code/` has NO dependency on kata's
ui/qt.py. Detects PySide6 (Maya 2025+) or PySide2, star-imports its widgets/gui, exposes QtCore and a
binding string (__qt__), and a binding-agnostic signal() factory.
"""

scale_multiplier = 1
try:
    import maya.cmds as cmds
    scale_multiplier = cmds.mayaDpiSetting(query=True, realScaleValue=True) or 1
except Exception:
    pass

__qt__ = None
try:
    from PySide6 import QtCore
    from PySide6.QtGui import *
    from PySide6.QtWidgets import *
    from PySide6.QtCore import Qt
    __qt__ = "pyside6"
except Exception:
    try:
        from PySide2 import QtCore
        from PySide2.QtGui import *
        from PySide2.QtWidgets import *
        from PySide2.QtCore import Qt
        __qt__ = "pyside2"
    except Exception:
        __qt__ = None

# QtCore classes the editor uses that the QtGui/QtWidgets star-imports do NOT provide
if __qt__:
    QStringListModel   = QtCore.QStringListModel
    QItemSelection     = QtCore.QItemSelection
    QItemSelectionModel = QtCore.QItemSelectionModel
    QTimer             = QtCore.QTimer
    QEvent             = QtCore.QEvent
    QSize              = QtCore.QSize
    QRect              = QtCore.QRect
    QPoint             = QtCore.QPoint          # QPoint lives in QtCore, not QtGui/QtWidgets
    QPointF            = QtCore.QPointF         # and so does its float twin, used for antialiased shapes
    QRectF             = QtCore.QRectF


def signal(*arg_list) -> "QtCore.Signal":
    """Binding-agnostic Signal factory (PySide uses QtCore.Signal)."""
    return QtCore.Signal(*arg_list)


def wrap_instance(pointer:object, base:object=None) -> object:
    """Wrap a C++ pointer (e.g. a Maya MQtUtil control) into a Qt object (shiboken6/2)."""
    if pointer is None:
        return None
    if __qt__ == "pyside6":
        import shiboken6 as shiboken
    else:
        import shiboken2 as shiboken
    return shiboken.wrapInstance(int(pointer), base if base is not None else QWidget)


def is_valid(obj:object) -> bool:
    """True when the C++ side of a Qt object is still alive.

    Widgets outlive their C++ counterpart on the python side (a reloaded module, a closed window kept
    referenced...). Touching one then raises RuntimeError, so guard with this before using a stored widget.
    """
    if obj is None:
        return False
    try:
        if __qt__ == "pyside6":
            import shiboken6 as shiboken
        else:
            import shiboken2 as shiboken
        return shiboken.isValid(obj)
    except Exception:
        return True          # no shiboken: assume alive and let the caller's try/except handle it


# Every surface the editor paints, by role rather than by colour, so the same widgets can render in
# two skins. The standalone window keeps the VS Code look it was designed around; the editor embedded
# in kata_manager takes kata's own palette so it does not read as a foreign application inside it.
#
# Themes are resolved PER EDITOR, never globally: both can be open at once, and a module-level theme
# would leave whichever was built last dictating the look of the other.
THEMES = {
    "vscode": {
        "editor":    "#121314",   # code area, active tab, welcome page, image and diff backgrounds
        "panel":     "#191a1b",   # side bars, bottom panel, menus, output console
        "rule":      "#2b2b2b",   # every 1px separator, and the tab borders
        "hover":     "#202122",
        "text":      "#cccccc",
        "bright":    "#ffffff",
        "dim":       "#9d9d9d",   # inactive tab labels
        "muted":     "#6a6f75",   # hints and placeholders
        "accent":    "#0078d4",   # active tab underline, focus ring, buttons
        "accent_on": "#1a86d9",
        "selection": "#5db4e5",   # menu / list selection, count bubbles
        "field":     "#252526",   # inputs and popup lists
        "border":    "#454545",
        "guide":     "#333333",   # tree indent guides
        "scroll":    "#424242",
        "textsel":   "#264f78",   # selected text inside an editor
    },
    "kata": {
        "editor":    "#2b2b2b",
        "panel":     "#303030",
        "rule":      "#3a3a3a",
        "hover":     "#373737",
        "text":      "#c4c4c4",
        "bright":    "#ffffff",
        "dim":       "#a8a8a8",
        "muted":     "#6a6a6a",
        "accent":    "#5285a6",
        "accent_on": "#639ec2",
        "selection": "#5285a6",
        "field":     "#373737",
        "border":    "#565656",
        "guide":     "#3f3f3f",
        "scroll":    "#565656",
        "textsel":   "#5285a6",
    },
}


def theme(name:str=None) -> dict:
    """The palette called `name` (a copy, so a caller cannot mutate the shared one)."""
    return dict(THEMES.get(name or "vscode", THEMES["vscode"]))


# kept for the modules that still refer to the accent by name
selection_blue = THEMES["vscode"]["selection"]


def px(n:int) -> int:
    """Scale a pixel value by Maya's DPI setting."""
    return max(1, round(n * scale_multiplier))


# symbol badges, shared by the Outline and the completion popup so a class reads the same in both.
# Colours follow VS Code's symbol icons; the letter is what its codicon draws as a glyph.
_SYMBOL_COLOR = {"class": "#ee9d28", "def": "#b180d7", "var": "#75beff",
                 "module": "#4ec9b0", "keyword": "#c586c0",
                 "arg": "#9cdcfe"}          # a parameter of the call the caret is inside
_SYMBOL_LETTER = {"class": "C", "def": "M", "module": "P", "keyword": "K", "arg": "A"}
_SYMBOL_CACHE = {}


def symbol_icon(kind:str) -> "QIcon":
    """The little coloured badge for a symbol kind, built on demand and cached."""
    if kind in _SYMBOL_CACHE:
        return _SYMBOL_CACHE[kind]
    size = px(16)
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    plate = QRect(px(2), px(2), size - px(4), size - px(4))
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(_SYMBOL_COLOR.get(kind, "#cccccc")))
    painter.drawRoundedRect(plate, px(3), px(3))
    font = painter.font()
    font.setPixelSize(max(7, size - px(7)))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor("#1a1a1a"))
    painter.drawText(plate, Qt.AlignCenter, _SYMBOL_LETTER.get(kind, "V"))
    painter.end()
    _SYMBOL_CACHE[kind] = QIcon(pixmap)
    return _SYMBOL_CACHE[kind]


def kata_stylesheet(name:str=None) -> str:
    """The editor's stylesheet, rendered in the palette called `name` (see THEMES).

    Every surface comes from the palette, and every panel background lives HERE rather than in an
    inline setStyleSheet on the widget itself. That matters for more than tidiness: a rule a widget
    sets on itself beats one inherited from an ancestor, so an inline background could not be
    re-skinned by the editor that owns it, and the two themes could not coexist.
    """
    import os
    icons = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons").replace("\\", "/")
    palette = theme(name)
    palette["right"] = "%s/chevron_right.png" % icons
    palette["down"]  = "%s/chevron_down.png"  % icons

    return """
    QWidget { background: %(editor)s; color: %(text)s; font-size: 13px; }

    /* splitter handles: the thin separator line drawn between layouts */
    QSplitter::handle { background: %(rule)s; }
    QSplitter::handle:horizontal { width: 1px; }
    QSplitter::handle:vertical { height: 1px; }

    QPlainTextEdit, QTextEdit { background: %(editor)s; border: none;
                                selection-background-color: %(textsel)s; }

    /* ---- left sidebar: file tree ----
       Row background / hover / selection and the name text are painted by GitStatusDelegate, so the
       item/branch background rules here stay transparent to avoid double-painting; only the chevron
       images come from the stylesheet. */
    QTreeView {
        background: %(panel)s; border: none; outline: 0;
        show-decoration-selected: 1;
        font-size: 13px;
    }
    /* selection matches what GitStatusDelegate paints in the explorer: a light translucent band with
       an accent outline, so every list in the side bar reads the same way */
    QTreeView::item, QListWidget::item { min-height: 22px; border: none; color: %(text)s; }
    QTreeView::item:hover, QListWidget::item:hover { background: rgba(255, 255, 255, 14); }
    QTreeView::item:selected, QListWidget::item:selected {
        background: rgba(255, 255, 255, 28); border: 1px solid %(selection)s; color: %(bright)s;
    }
    QListWidget { background: %(panel)s; border: none; outline: 0; }
    QTreeView:focus, QListWidget:focus { border: none; }

    /* the workspace tree is fully custom-painted by GitStatusDelegate: keep every state transparent
       there so the stylesheet does not paint a second background under the delegate's own band */
    QTreeView#codeWorkspaceTree::item,
    QTreeView#codeWorkspaceTree::item:hover,
    QTreeView#codeWorkspaceTree::item:selected,
    QTreeWidget#codeScmTree::item,
    QTreeWidget#codeScmTree::item:hover,
    QTreeWidget#codeScmTree::item:selected { background: transparent; border: none; }

    QTreeView::branch { background: transparent; }
    QTreeView::branch:has-children:closed { image: url(%(right)s); }
    QTreeView::branch:has-children:open   { image: url(%(down)s); }

    /* ---- tab bar: strip and inactive tabs on the panel surface, active tab = editor surface,
           tabs separated by the same 1px rule used elsewhere in the UI ---- */
    QTabWidget { background: %(panel)s; }                /* strip filler right of the last tab */
    QTabWidget::pane { border: none; background: %(editor)s; }
    /* the page area is a real QStackedWidget child, not just the ::pane sub-control. Without this it
       keeps the generic QWidget colour and shows through wherever a page does not reach. */
    QTabWidget > QStackedWidget { background: %(editor)s; }
    /* document mode: the bar spans the full width, so this background fills the whole strip.
       the bottom rule runs under the whole row and is broken only under the ACTIVE tab. */
    QTabBar { background: %(panel)s; border-bottom: 1px solid %(rule)s; qproperty-drawBase: 0; }
    QTabBar::tab { background: %(panel)s; color: %(dim)s; font-size: 13px; min-width: 40px;
                   padding: 6px 10px 6px 14px;           /* extra room on the right for the close button */
                   border-right: 1px solid %(rule)s;
                   border-bottom: 1px solid %(rule)s; }
    QTabBar::tab:selected { background: %(editor)s; color: %(bright)s;
                            border-top: 1px solid %(accent)s;
                            border-bottom: 1px solid %(editor)s; }  /* breaks the rule under the active tab */
    QTabBar::tab:hover:!selected { background: %(hover)s; color: %(bright)s; }
    /* pull the close cross away from the tab's right edge (and the window controls above it) */
    QTabBar::close-button { margin-left: 8px; margin-right: 12px; }

    /* bottom panel (Problems / Output / Debug Console): flat text tabs, no closing cross.
       drawBase: 0 removes the rule document mode paints under the whole row, so the only line left
       is the accent underline marking the active tab. */
    QTabWidget#codePanel { background: %(panel)s; }      /* strip behind the corner controls */
    QTabWidget#codePanel::pane { border: none; background: %(panel)s; }
    QTabWidget#codePanel > QStackedWidget { background: %(panel)s; }
    QTabWidget#codePanel > QTabBar { background: %(panel)s; border: none; qproperty-drawBase: 0; }
    QTabWidget#codePanel > QTabBar::tab { background: transparent; color: %(dim)s; border: none;
                                          padding: 6px 12px; font-size: 11px; }
    QTabWidget#codePanel > QTabBar::tab:selected { color: %(bright)s;
                                                   border-bottom: 1px solid %(accent)s; }
    QTabWidget#codePanel > QTabBar::tab:hover:!selected { color: %(bright)s; }

    /* ---- menu bar ---- */
    QMenuBar { background: %(panel)s; color: %(text)s; padding: 2px; font-size: 13px; }
    QMenuBar::item { padding: 4px 8px; background: transparent; border-radius: 3px; }
    QMenuBar::item:selected { background: %(selection)s; color: %(bright)s; }
    QMenu { background: %(panel)s; color: %(text)s; border: 1px solid %(border)s; }
    QMenu::item { padding: 4px 24px; }
    QMenu::item:selected { background: %(selection)s; color: %(bright)s; }
    QMenu::separator { height: 1px; background: %(border)s; margin: 4px 0; }

    /* ---- buttons / labels ---- */
    QPushButton { background: %(accent)s; color: %(bright)s; border: none;
                  padding: 3px 10px; border-radius: 2px; }
    QPushButton:hover { background: %(accent_on)s; }
    QLabel { background: transparent; }

    /* section headers of the sidebar accordion */
    QToolButton { border: none; background: %(panel)s; color: %(text)s; }

    /* ---- scrollbars (thin) ---- */
    QScrollBar:vertical { background: transparent; width: 12px; margin: 0; }
    QScrollBar::handle:vertical { background: %(scroll)s; min-height: 24px; border-radius: 6px; }
    QScrollBar:horizontal { background: transparent; height: 12px; margin: 0; }
    QScrollBar::handle:horizontal { background: %(scroll)s; min-width: 24px; border-radius: 6px; }
    QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
    QScrollBar::add-page, QScrollBar::sub-page { background: none; }

    /* ---- panels, by object name ----
       These used to be set inline on each widget. Kept here so one sheet skins the whole editor and
       an embedded instance can differ from the standalone one. Each of these widgets carries
       WA_StyledBackground, without which a plain QWidget subclass paints no stylesheet background. */
    QWidget#codeSidebar, QWidget#codeSidebarViews, QWidget#codeExplorerView,
    QWidget#codeWorkspace, QWidget#codeSearch, QWidget#codeGraph, QWidget#codeScm,
    QWidget#codeActivityBar, QWidget#codeSecondary, QWidget#codeProblems,
    QWidget#codeOutline, QWidget#codeTimeline, QWidget#codeOpenEditors,
    QWidget#codeDebugConsole, QWidget#codeOutput, QWidget#codeMenuCorner,
    QWidget#codePanelCorner { background: %(panel)s; }

    QWidget#codeImagePage, QLabel#codeImageView { background: %(editor)s; }
    QStackedWidget#codeStack { background: %(editor)s; }
    QLabel#codeWelcome { background: %(editor)s; color: %(muted)s; }
    QScrollArea#codeImageArea { border: none; background: %(editor)s; }
    QListWidget#codeGraphList { background: %(panel)s; border: none; outline: 0; }
    QTextEdit#codeDebugView { background: %(panel)s; border: none; color: %(text)s; }
    QFrame#codeRule { background: %(rule)s; border: none; }

    /* headers above an image or a diff, and the muted one-line hints */
    QLabel#codePageHeader { background: %(panel)s; color: %(text)s; font-size: 11px;
                            padding: 4px 6px; border-bottom: 1px solid %(rule)s; }
    QLabel#codeHint { color: %(muted)s; font-size: 10px; background: transparent; padding: 4px 6px; }

    /* single-line inputs: the search field, and the debug console's prompt */
    QLineEdit#codeInput { background: %(editor)s; border: 1px solid %(rule)s; color: %(text)s;
                          padding: 4px 6px; }
    QLineEdit#codeInput:focus { border: 1px solid %(accent)s; }
    QLineEdit#codePrompt { background: %(editor)s; border: none; border-top: 1px solid %(rule)s;
                           color: %(text)s; padding: 5px 6px; }

    /* a plain (non-collapsible) section title, e.g. in the secondary side bar */
    QLabel#codeSectionLabel { border-top: 1px solid %(rule)s; background: %(panel)s; color: %(text)s;
                              font-size: 11px; font-weight: bold; padding: 5px 6px; }

    /* hover cards: Qt's default is a pale yellow plate that belongs to no theme here */
    QToolTip { background: %(panel)s; color: %(text)s; border: 1px solid %(border)s;
               padding: 6px 8px; }

    /* ---- dialogs (New File, Rename, Delete, Save Error...) ----
       They reach these rules by being PARENTED to a widget inside the editor: a stylesheet travels
       down the parent chain, and a dialog created with parent=None is styled by nothing. */
    QDialog, QMessageBox, QInputDialog { background: %(panel)s; color: %(text)s; }
    QDialog QLabel, QMessageBox QLabel, QInputDialog QLabel { color: %(text)s; background: transparent; }
    QDialog QLineEdit, QInputDialog QLineEdit {
        background: %(editor)s; border: 1px solid %(rule)s; color: %(text)s;
        padding: 4px 6px; min-width: 260px;
    }
    QDialog QLineEdit:focus, QInputDialog QLineEdit:focus { border: 1px solid %(accent)s; }
    QDialog QPlainTextEdit, QInputDialog QPlainTextEdit {
        background: %(editor)s; border: 1px solid %(rule)s; color: %(text)s;
    }
    /* the buttons a QMessageBox builds for itself, which the generic QPushButton rule sizes too
       tightly to read */
    QDialog QPushButton, QMessageBox QPushButton, QInputDialog QPushButton {
        background: %(field)s; color: %(text)s; border: 1px solid %(rule)s;
        padding: 5px 16px; border-radius: 2px; min-width: 76px;
    }
    QDialog QPushButton:hover, QMessageBox QPushButton:hover, QInputDialog QPushButton:hover {
        background: %(hover)s; color: %(bright)s; border: 1px solid %(border)s;
    }
    QDialog QPushButton:default, QMessageBox QPushButton:default, QInputDialog QPushButton:default {
        background: %(accent)s; color: %(bright)s; border: 1px solid %(accent)s;
    }
    QDialog QPushButton:default:hover, QMessageBox QPushButton:default:hover {
        background: %(accent_on)s;
    }

    /* ---- the command palette, dropped over the top of the window ---- */
    QWidget#codePalette { background: %(panel)s; border: 1px solid %(border)s; }
    QLineEdit#codePaletteField { background: %(editor)s; border: 1px solid %(rule)s;
                                 color: %(text)s; padding: 5px 7px; font-size: 13px; }
    QLineEdit#codePaletteField:focus { border: 1px solid %(accent)s; }
    QListWidget#codePaletteList { background: %(panel)s; border: none; outline: 0;
                                  font-size: 12px; }
    QListWidget#codePaletteList::item { color: %(text)s; padding: 3px 7px; border: none; }
    QListWidget#codePaletteList::item:selected { background: %(selection)s; color: %(bright)s; }

    /* ---- the floating find panel, over the editor's top-right corner ---- */
    QWidget#codeFind { background: %(panel)s; border: 1px solid %(rule)s; }
    /* bare containers inside a themed panel: without a name they fall to the generic
       QWidget rule and paint a plate of the EDITOR surface over the panel one */
    QWidget#codeTransparent { background: transparent; }
    QWidget#codeFindField { background: %(editor)s; border: 1px solid %(rule)s; }
    QWidget#codeFindField[invalid="true"] { border: 1px solid #c74e39; }
    QLineEdit#codeFindEdit { background: transparent; border: none; color: %(text)s;
                             font-size: 12px; min-width: 200px; min-height: 26px; }
    /* every button in the panel is flat: no plate in ANY state, the tint only appears on hover.
       :pressed and :focus have to be spelled out too, or the style paints its own sunken frame. */
    QToolButton#codeFindOption, QToolButton#codeFindNav, QToolButton#codeFindToggle {
        border: none; background: transparent; border-radius: 3px;
    }
    QToolButton#codeFindOption:pressed, QToolButton#codeFindNav:pressed,
    QToolButton#codeFindToggle:pressed { border: none; }

    /* the glyphs were rendering tiny next to VS Code's: these three sizes are most of the gap */
    QToolButton#codeFindOption { color: %(dim)s; font-size: 12px; }
    QToolButton#codeFindOption:hover { background: rgba(255, 255, 255, 22); color: %(bright)s; }
    /* a toggle still has to SAY it is on, so checked keeps a tint - just a translucent one rather
       than a solid accent plate */
    QToolButton#codeFindOption:checked { background: rgba(255, 255, 255, 40); color: %(bright)s; }
    QToolButton#codeFindOption:checked:hover { background: rgba(255, 255, 255, 58); }

    QToolButton#codeFindNav { color: %(text)s; font-size: 15px; padding: 0px; }
    QToolButton#codeFindNav:hover { background: rgba(255, 255, 255, 22); color: %(bright)s; }
    QToolButton#codeFindNav:disabled { color: %(muted)s; background: transparent; }

    QToolButton#codeFindToggle { color: %(dim)s; }
    QToolButton#codeFindToggle:hover { background: rgba(255, 255, 255, 22); }
    QLabel#codeFindCount { color: %(muted)s; font-size: 11px; background: transparent; }

    /* the code area and the diff view: the editor surface, no frame */
    QPlainTextEdit#codeEdit { background: %(editor)s; border: 0px; }
    QPlainTextEdit#codeDiffView { background: %(editor)s; border: none; color: %(text)s; }

    /* the status strip along the bottom of the window */
    QWidget#codeStatus { background: %(panel)s; }
    QToolButton#codeStatusField { border: none; background: transparent; color: %(dim)s;
                                  font-size: 11px; padding: 2px 7px; border-radius: 3px; }
    QToolButton#codeStatusField:hover { background: rgba(255, 255, 255, 22); color: %(bright)s; }
    QToolButton#codeStatusField:disabled { color: %(dim)s; background: transparent; }

    /* QUICK HELP: a documentation table, the way the cmds docs list flags */
    QTextBrowser#codeQuickHelp { background: %(panel)s; color: %(text)s; border: 0px; }
    /* no colour here: the title is rich text, painted with the python highlighter's own roles */
    QLabel#codeHelpTitle { font-size: 11px; background: %(panel)s;
                           padding: 4px 8px 4px 8px; border-bottom: 1px solid %(rule)s; }
    QTreeWidget#codeHelpTable { background: %(panel)s; color: %(text)s; border: 0px;
                                font-size: 11px; }
    /* the row band is painted by HelpRowDelegate, the way the explorer paints its rows: a
       stylesheet rule here would put a solid bar straight over it */
    QTreeWidget#codeHelpTable::item { padding: 2px 4px; border: 0px; }
    QTreeWidget#codeHelpTable QHeaderView::section {
        background: %(panel)s; color: %(muted)s; font-size: 10px; padding: 3px 4px;
        border: none; border-bottom: 1px solid %(rule)s;
    }

    /* breadcrumbs over the editor. On the CODE surface, not the panel one: the strip belongs to the
       editor, and a paler band across the top read as a separate widget sitting on it. */
    QWidget#codeBreadcrumbs { background: %(editor)s; }
    QToolButton#codeCrumb { border: none; background: transparent; color: %(dim)s;
                            font-size: 11px; padding: 2px 4px; }
    QToolButton#codeCrumb:hover { color: %(bright)s; }
    QToolButton#codeCrumb:disabled { color: %(muted)s; }
    /* padded down a little: the chevron is drawn in the middle of its own square, which sits higher
       than the x-height of the crumb text beside it */
    QLabel#codeCrumbSep { background: transparent; padding: 3px 2px 0px 2px; }

    /* the title row at the top of each side bar view, closed by the same 1px rule as everything
       else. The FIRST section below it drops its own top border, or the two would stack into a
       2px line under Explorer while Search - which has no sections - showed only one. */
    QWidget#codeViewHeader { background: %(panel)s; border-bottom: 1px solid %(rule)s; }
    QLabel#codeViewTitle { color: %(text)s; font-size: 11px; background: transparent; }
    QToolButton#codeViewMore { border: none; background: transparent; color: %(dim)s;
                               font-size: 14px; padding: 0px 6px; border-radius: 3px; }
    QToolButton#codeViewMore:hover { background: rgba(255, 255, 255, 22); color: %(bright)s; }

    /* collapsible section headers of the side bar accordion */
    /* Each header carries its OWN separator instead of the section stack's splitter handles drawing
       them: a section living outside the stack (Open Editors) had no line above it that way, and the
       last one none below. Owned by the header, the rule follows the section wherever it is put -
       so the stack's handles are painted transparent and only serve as the drag zone. */
    QSplitter#codeSectionStack::handle { background: transparent; }
    QSplitter#codeSectionStack::handle:vertical { height: 1px; }
    QToolButton#codeSectionHeader {
        border: none; border-top: 1px solid %(rule)s; background: %(panel)s;
        color: %(text)s; font-size: 11px; font-weight: bold; padding: 6px 6px; text-align: left;
    }
    /* the section leading a column sits under the view header, which already drew that rule */
    QToolButton#codeSectionHeader[first="true"] { border-top: none; }
    /* the last header of a fully collapsed column, so the stack reads as a block instead of
       dissolving into the empty space below it */
    QToolButton#codeSectionHeader[closing="true"] { border-bottom: 1px solid %(rule)s; }
    QToolButton#codeSectionHeader:hover { color: %(bright)s; }
    /* the file name beside a section title: smaller, dimmer, and NOT bold like the title */
    QLabel#codeSectionSuffix { color: %(muted)s; font-size: 10px; font-weight: normal;
                               background: transparent; }

    /* activity bar buttons: a transparent left border is always reserved, so the icon never shifts
       sideways when the active view lights it up */
    QToolButton#codeActivityButton { border: none; border-left: 2px solid transparent;
                                     background: transparent; }
    QToolButton#codeActivityButton:hover { background: transparent; }
    QToolButton#codeActivityButton:checked { border-left: 2px solid %(bright)s; background: transparent; }
    QLabel#codeActivityBadge { background: %(selection)s; color: %(bright)s; font-size: 9px;
                               border-radius: 7px; padding: 1px 4px; }

    /* the layout toggles at the right end of the menu bar: icon only, no plate in any state */
    QToolButton#codeLayoutToggle, QToolButton#codeLayoutToggle:hover,
    QToolButton#codeLayoutToggle:pressed { border: none; background: transparent; padding: 2px; }

    /* the output console's own controls, lifted into the panel tab row's corner */
    QWidget#codePanelCorner QRadioButton { background: transparent; color: %(dim)s; font-size: 11px;
                                           spacing: 4px; }
    QWidget#codePanelCorner QRadioButton:hover { color: %(bright)s; }
    QWidget#codePanelCorner QComboBox { background: transparent; color: %(dim)s; font-size: 11px;
                                        border: 1px solid %(rule)s; border-radius: 2px;
                                        padding: 1px 6px; }
    QWidget#codePanelCorner QComboBox:hover { color: %(bright)s; border: 1px solid %(border)s; }
    /* the arrow has to be given explicitly: a styled QComboBox stops drawing the native one, which
       is why the boxes read as flat rectangles with nothing to say they open */
    QWidget#codePanelCorner QComboBox::drop-down { border: none; width: 14px;
                                                   subcontrol-origin: padding;
                                                   subcontrol-position: center right; }
    QWidget#codePanelCorner QComboBox::down-arrow { image: url(%(down)s); width: 9px; height: 9px; }
    QWidget#codePanelCorner QComboBox QAbstractItemView { background: %(field)s; color: %(text)s;
                                                          border: 1px solid %(border)s;
                                                          selection-background-color: %(selection)s; }

    /* Maya's log stream, sitting on the panel surface like the rest of the bottom row */
    QWidget#codeOutput QTextEdit { border: 0px; background-color: %(panel)s; }
    QWidget#codeOutput QTextEdit QScrollBar { background: none; }
    """ % palette
