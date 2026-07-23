"""
KATA. (c)

Author: Gregoire Dehame
Created: Wed 11, 2024
Modified: Jul 08, 2026
Module: ui.code_editor.core.console
Execute: from kata.ui.code_editor.core import console
"""

import re, os
import logging
log = logging.getLogger(__name__)

from functools import partial

import maya.OpenMaya as om
import maya.cmds as cmds

# self-contained: no dependency on the rest of kata (only its sibling core modules)
from . import qt
__icons__ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")

# the MEL -> python echo used to need pymel, which Maya dropped in 2026; core/mel2py.py replaces it
# and has no version constraint, so the switch is always available now
__mel2py__ = True

__light__ = True


class Widget(qt.QWidget):
    """Maya output console widget displaying command results in a styled text editor.

    Args:
        parent:   (object): - Parent widget.
        **kwargs: (dict):   - Optional keyword arguments.
    """

    # emitted when the user double-clicks a traceback line: (file_path, line_number)
    errorClicked = qt.signal(str, int)

    # File "C:/path/to/file.py", line 42, in <module>
    _TRACE_RE = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)')


    def __init__(self, parent=None, **kwargs) -> None:
        """Initialize the output console widget and register the Maya command callback.

        Args:
            parent:   (object): - Parent widget.
            **kwargs: (dict):   - Optional keyword arguments.

        Returns:
            None.
        """
        super().__init__(parent=parent)
        self.layout = qt.QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)

        self.bar = qt.QWidget()
        self.grid = qt.QHBoxLayout(self.bar)
        self.grid.setContentsMargins(5, 2, 5, 5)
        self.grid.setSpacing(0)

        self.label = qt.QLabel('OUTPUT')
        self.label.setStyleSheet("QLabel { color : white; }");
        self.label.setSizePolicy(qt.QSizePolicy.Preferred, qt.QSizePolicy.Expanding)
        self.label.setFixedHeight(20)
        self.grid.addWidget(self.label)

        self.python = qt.QRadioButton(".py", fixedWidth=45, enabled=__mel2py__)
        self.grid.addWidget(self.python)

        self.mel = qt.QRadioButton(".mel", checked=True, fixedWidth=50)
        self.grid.addWidget(self.mel)

        self.echo = qt.QComboBox(fixedWidth=100, fixedHeight=20)
        self.echo.addItems(["Echo All", "Normal"])
        self.echo.setCurrentText("Echo All" if cmds.commandEcho(query=True, state=True) else "Normal")
        self.echo.setStyleSheet("background-color: #5d5d5d;")

        self.echo.currentTextChanged.connect(partial(self.clicked_echo))
        self.grid.addWidget(self.echo)

        self.clean = qt.QPushButton(fixedWidth=25, fixedHeight=20)
        clean_icons = [str(os.path.join(__icons__, "erase.png")).replace('\\', '/'), str(os.path.join(__icons__, "erase_hover.png")).replace('\\', '/')]
        self.clean.setStyleSheet('''QPushButton{background: transparent;border: 0px solid #444444;icon: url('''+ clean_icons[0] + ''');}QPushButton:hover {icon: url(''' + clean_icons[1] + ''');}''')

        self.clean.clicked.connect(partial(self.clicked_clean))
        self.grid.addWidget(self.clean)

        self.grid.setStretch(0, 0)
        if __light__:
            self.bar.setStyleSheet("background-color: #2b2b2b;")
        else:
            self.bar.setStyleSheet("background-color: #1e1e1e;")

        self.editor = qt.QTextEdit(readOnly=True)
        self.editor.document().setMaximumBlockCount(3000)

        if __light__:
            self.editor.setStyleSheet("""QTextEdit {border: 0px; background-color: #2b2b2b;} QTextEdit QScrollBar {background: none;}""")
        else:
            self.editor.setStyleSheet("""QTextEdit {border: 0px; background-color: #1e1e1e;} QTextEdit QScrollBar {background: none;}""")

        self.editor.setWordWrapMode(qt.QTextOption.NoWrap)
        self.editor.setSizePolicy(qt.QSizePolicy.Preferred, qt.QSizePolicy.Expanding)
        font = qt.QFont("Consolas", 9)
        self.editor.setFont(font)
        self.editor.setContextMenuPolicy(qt.Qt.CustomContextMenu)
        self.editor.customContextMenuRequested.connect(self.context_menu_editor)
        # double-click a "File "...", line N" traceback line to jump to it in the editor
        self.editor.mouseDoubleClickEvent = self._output_double_click
        Highlighter(self.editor.document())

        self.layout.addWidget(self.bar)
        self.layout.addWidget(self.editor)
        self.layout.setStretch(0, 0)
        self.layout.setStretch(1, 1)

        # how maya's command echo is shown: as MEL, or translated with short or long flag names.
        # Long is the default because "radius=1" says what it is where "r=1" needs the docs open.
        self.output_language = "python_long"

        # an exception reaches us in pieces; they are collected here and written as one block once
        # maya goes quiet. 40ms is long enough to catch the whole burst, short enough to feel live.
        self._error_parts = []
        self._error_timer = qt.QTimer(self)
        self._error_timer.setSingleShot(True)
        self._error_timer.setInterval(40)
        self._error_timer.timeout.connect(self._flush_errors)

        self.callback_id = om.MCommandMessage.addCommandOutputFilterCallback(self.callback)
        # make sure the Maya callback is torn down even if the widget is destroyed without a closeEvent
        # (deleteLater / parent destruction) — a callback firing into a dead widget can crash Maya.
        self.destroyed.connect(lambda *a, cid=self.callback_id: Widget._safe_remove_callback(cid))

        self.setLayout(self.layout)


    @staticmethod
    def _safe_remove_callback(callback_id) -> None:
        """Remove a Maya callback id, ignoring the case where it is already gone."""
        if callback_id is None:
            return
        try:
            om.MMessage.removeCallback(callback_id)
        except Exception:
            pass


    def closeEvent(self, event:qt.QCloseEvent) -> None:
        """Remove the Maya command output callback when the widget is closed.

        Args:
            event: (qt.QCloseEvent): - The close event.

        Returns:
            None.
        """
        Widget._safe_remove_callback(getattr(self, "callback_id", None))
        self.callback_id = None
        super().closeEvent(event)


    def append_plain_text(self, text:str="") -> None:
        """Append text to the editor as plain text, bypassing QTextEdit.append()'s rich-text
        detection (Qt.mightBeRichText), which misreads traceback patterns like '<module>' or
        '<maya console>' as html tags and garbles the output.

        Args:
            text: (str): - Text to append.

        Returns:
            None.
        """
        cursor = self.editor.textCursor()
        cursor.movePosition(qt.QTextCursor.End)
        if not self.editor.document().isEmpty():
            cursor.insertBlock()
        cursor.insertText(text)
        self.editor.setTextCursor(cursor)


    def _output_double_click(self, event) -> None:
        """On double-click, if the clicked line is a 'File "...", line N' traceback line, emit errorClicked
        so the editor can jump there. Otherwise fall back to the default double-click (word select).
        """
        cursor = self.editor.cursorForPosition(event.pos())
        line = cursor.block().text()
        match = self._TRACE_RE.search(line or "")
        if match:
            try:
                self.errorClicked.emit(match.group("path"), int(match.group("line")))
                return
            except Exception:
                log.exception("code output: failed to route a traceback double-click")
        qt.QTextEdit.mouseDoubleClickEvent(self.editor, event)


    # A line Python 3.11+ draws under a traceback frame to point at the failing expression: runs of
    # ^ ~ | and nothing else. Maya prefixes traceback lines with "# ", and re-wraps long ones, so the
    # prefix and a lone marker on its own line both have to be tolerated.
    _MARKER_RE = re.compile(r'^[\s#]*[\^~|][\s#\^~|]*$')

    @classmethod
    def strip_markers(cls, text:str) -> str:
        """Drop the caret ruler lines from a traceback, keeping everything that carries information.

        Without this the log shows a column of ^ under every frame, which is noise here: the file and
        line are already there, and double-clicking them jumps to the exact spot.
        """
        lines = [line for line in text.splitlines() if not cls._MARKER_RE.match(line)]
        while lines and lines[0].strip() in ("", "#"):      # a prefix left stranded by a dropped line
            lines.pop(0)
        while lines and lines[-1].strip() in ("", "#"):
            lines.pop()
        return "\n".join(lines).strip()

    @staticmethod
    def is_mel_echo(text:str) -> bool:
        """True only for a line that is really Maya echoing a MEL command.

        Everything else on this channel must be printed VERBATIM. A python traceback arrives here
        too, and running it through the MEL translator mangled it beyond reading: frames came out as
        `cmds.self(_place_tab, ...)`, source lines got a `# MEL:` prefix, and the spacing collapsed.

        A MEL echo always ends in a semicolon and never begins with a comment marker, which is enough
        to tell the two apart without guessing at the content.
        """
        line = (text or "").strip()
        if not line or line.startswith(("#", "//")):
            return False
        if "Traceback (most recent call last)" in line or line.startswith("File \""):
            return False
        return line.endswith(";")

    def _buffer_error(self, fragment:str) -> None:
        """Collect an error fragment; the whole block is written once Maya stops sending.

        Maya does not deliver an exception as one message. It arrives in pieces - the frames, then
        "AttributeError", then ":", then the text, then ". Did you mean: '" - and writing each as
        its own line is what shredded the traceback across the log. Buffering them and flushing on a
        short idle reassembles the block the script editor shows.
        """
        self._error_parts.append(fragment)
        self._error_timer.start()

    def _flush_errors(self) -> None:
        """Write the buffered fragments as one block."""
        parts, self._error_parts = self._error_parts, []
        if not parts:
            return
        # a fragment that is punctuation or a continuation joins the previous line; one that starts a
        # new frame keeps its own. Maya never marks the difference, so the shape of the text decides.
        text = ""
        for piece in parts:
            if not text:
                text = piece
            elif piece.startswith("\n") or text.endswith("\n"):
                text += piece                    # one side already carries the break
            elif piece.lstrip().startswith(("File \"", "Traceback")):
                text += "\n" + piece             # a new frame starts its own line
            else:
                text += piece                    # punctuation and continuations stay on the line
        block = self.strip_markers(text)
        if block.strip():
            self.append_plain_text("// Error: %s" % block)

    def callback(self, message, message_type, *args) -> None:
        """Update the output editor with Maya command messages in real-time.

        Args:
            message     : (str):   - The message string from Maya.
            message_type: (int):   - The message type constant from MCommandMessage.
            *args       : (tuple): - Additional arguments from the callback.

        Returns:
            None.
        """
        if not message or not message.strip():
            return
        try:
            # done BEFORE the type switch: a traceback does not always arrive as kError - Maya routes
            # parts of it through kWarning and kHistory too, and those branches used to print the
            # caret rulers verbatim.
            stripped = self.strip_markers(message.strip())
            if not stripped:
                return

            if message_type == om.MCommandMessage.kInfo:
                self.append_plain_text("// Info: %s" % stripped)

            elif message_type == om.MCommandMessage.kWarning:
                self.append_plain_text("// Warning: %s" % stripped)

            elif message_type == om.MCommandMessage.kError:
                self._buffer_error(message)

            elif message_type == om.MCommandMessage.kResult:
                self.append_plain_text("// Result: %s" % stripped)

            else:
                if self.output_language.startswith("python") and self.is_mel_echo(stripped):
                    try:
                        from . import mel2py
                        text = mel2py.translate(
                            str(stripped), long_names=self.output_language == "python_long")
                        self.append_plain_text(text or stripped)
                    except Exception:
                        self.append_plain_text(stripped)   # untranslatable: show the MEL as it came
                else:
                    self.append_plain_text(stripped)

            try:
                vertical_scoll_bar = self.editor.verticalScrollBar()
                vertical_scoll_bar.setValue(vertical_scoll_bar.maximum())

                cursor = self.editor.textCursor()
                cursor.movePosition(qt.QTextCursor.End)
                cursor.movePosition(qt.QTextCursor.StartOfBlock)
                self.editor.setTextCursor(cursor)

            except Exception:
                pass                            # auto-scroll is cosmetic; never break the callback for it

        except Exception:
            # a formatting bug must not silently swallow output — fall back to the raw message and log it
            try:
                self.append_plain_text(message.strip())
            except Exception:
                pass
            log.exception("code output: failed to format a Maya message")


    def clicked_echo(self, echo:str=None) -> None:
        """Update the Maya command echo state based on the selected option.

        Args:
            echo: (str): - Echo mode string, either "Echo All" or "Normal".

        Returns:
            None.
        """
        cmds.commandEcho(state=True if echo == "Echo All" else False)


    def clicked_clean(self) -> None:
        """Clear all text from the output editor.

        Args:
            //

        Returns:
            None.
        """
        self.editor.clear()


    def context_menu_editor(self, position) -> None:
        """Build and show a custom context menu for the output editor.

        Args:
            position: (qt.QPoint): - Position at which to show the menu.

        Returns:
            None.
        """
        self.menu = qt.QMenu(self.editor)

        self.copy = self.menu.addAction("Copy")
        self.copy.setShortcut("Ctrl+C")
        self.copy.triggered.connect(self.triggered_copy)
        self.menu.addSeparator()

        self.select = self.menu.addAction("Select All")
        self.select.setShortcut("Ctrl+A")
        self.select.triggered.connect(self.triggered_select)
        self.menu.addSeparator()

        self.clear = self.menu.addAction("Clean Ouput")
        self.clear.setShortcut("Ctrl+Shift+1")
        self.clear.triggered.connect(self.clicked_clean)
        self.menu.addSeparator()

        self.menu.exec_(self.editor.mapToGlobal(position))


    def triggered_copy(self) -> None:
        """Copy the currently selected text from the editor to the clipboard.

        Args:
            //

        Returns:
            None.
        """
        selected_text = self.editor.textCursor().selectedText()
        if selected_text:
            clipboard = qt.QApplication.clipboard()
            clipboard.setText(selected_text, qt.QClipboard.Clipboard)
            clipboard.setText(selected_text, qt.QClipboard.Selection)


    def triggered_select(self) -> None:
        """Select all text in the output editor.

        Args:
            //

        Returns:
            None.
        """
        self.editor.selectAll()


def char_format(color:str=None, style:str="") -> qt.QTextCharFormat:
    """Create and return a QTextCharFormat configured with the given color and style.

    Args:
        color: (str): - Named color key or color string.
        style: (str): - Additional style string (currently unused).

    Returns:
        (qt.QTextCharFormat): - Configured text character format.
    """
    if type == 'display':
        fg = qt.QColor(156, 220, 254)
    elif type == 'info':
        fg = qt.QColor(212, 212, 212)
    elif color == 'warning':
        fg = qt.QColor(223, 229, 36)
    elif color == 'error':
        fg = qt.QColor(240, 40, 40)
    elif color == 'result':
        fg = qt.QColor(42, 180, 34)
    else:
        fg = qt.QColor()
        fg.setNamedColor(color)

    frm = qt.QTextCharFormat()
    frm.setForeground(fg)
    return frm


STYLES = {
    'display': char_format('display'),
    'info': char_format('info'),
    'warning': char_format('warning'),
    'error': char_format('error'),
    'result': char_format('result'),
}


class Highlighter(qt.QSyntaxHighlighter):
    """Syntax highlighter for the output editor, colorizing info, warning, error, and result lines.

    Args:
        parent: (object): - Parent document to highlight.
    """


    def __init__(self, parent=None) -> None:
        """Initialize the highlighter and compile regex rules.

        Args:
            parent: (object): - Parent document to highlight.

        Returns:
            None.
        """
        super().__init__(parent)
        rules = []
        rules += [
            (r'// Display:[^\n]*', 0, STYLES['display']),
            (r'// Warning:[^\n]*', 0, STYLES['warning']),
            (r'// Error:[^\n]*', 0, STYLES['error']),
            (r'// Result:[^\n]*', 0, STYLES['result']),
        ]

        self.rules = [(qt.QtCore.QRegularExpression(pat), index, fmt) for (pat, index, fmt) in rules] if qt.__qt__ == "pyside6" else [(qt.QtCore.QRegExp(pat), index, fmt) for (pat, index, fmt) in rules]


    def highlightBlock(self, text) -> None:
        """Apply syntax highlighting rules to the given block of text.

        Args:
            text: (str): - Block of text to highlight.

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
                    self.setFormat(index, length, format)
            else:
                index = expression.indexIn(text, 0)
                while index >= 0:
                    index = expression.pos(nth)
                    length = len(expression.cap(nth))
                    self.setFormat(index, length, format)
                    index = expression.indexIn(text, index + length)

        self.setCurrentBlockState(0)



    '''
    def highlightBlock(self, text):
        """apply syntax highlighting to the given block of text."""

        self.tripleQuoutesWithinStrings = []
        for expression, nth, format in self.rules:
            index = expression.indexIn(text, 0)

            while index >= 0:
                if index in self.tripleQuoutesWithinStrings:
                    index += 1
                    expression.indexIn(text, index)
                    continue

                index = expression.pos(nth)
                length = len(expression.cap(nth))
                self.setFormat(index, length, format)
                index = expression.indexIn(text, index + length)

        self.setCurrentBlockState(0)'''
