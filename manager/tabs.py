"""
CODE EDITOR.

Author: Gregoire Dehame
Created: Wed 11, 2024
Modified: Aug 26, 2026
Module: code_editor.manager.tabs
Execute: from code_editor.manager import tabs
"""

from functools import partial

from ... import __icons__, qt
from .... import core as kcore

from ..core import editor

# from importlib import reload
# reload(editor)

import maya.cmds as cmds
import os, datetime


def _rel(path:str=None, root:str=None) -> str:
    """Convert absolute path to relative from root; keep absolute if outside root or different drive.

    Args:
        path: (str): - absolute path to convert.
        root: (str): - root directory to make the path relative to.

    Returns:
        str: path relative to root, or the original path when outside root.
    """
    if not path or not root or not os.path.isabs(path):
        return path
    try:
        rel = os.path.relpath(path, root)
        return rel if not rel.startswith('..') else path
    except ValueError:
        return path


def _abs(path:str=None, root:str=None) -> str:
    """Resolve relative path against root; absolute paths pass through.

    Args:
        path: (str): - relative or absolute path to resolve.
        root: (str): - root directory to resolve the path against.

    Returns:
        str: absolute path resolved against root.
    """
    if not path or os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(root, path))


__light__ = True


class TabWidget(qt.QTabWidget):
    """Tabbed code editor container managing temp-file-backed script tabs."""
    dataRemoved = qt.signal(object)

    def __init__(self, parent=None) -> None:
        """Initialize the tab widget, its stylesheet and corner focus menu.

        Args:
            parent: (object): - parent widget for the tab container.

        Returns:
            None.
        """
        super().__init__(parent)
        self.workspace = None
        self.setMovable(True)
        self.setTabsClosable(True)
        self.setVisible(False)

        tab_h = qt.px(25)
        if __light__:
            self.setStyleSheet(f"""
                QTabWidget {{ background-color: #2b2b2b; }}
                QTabWidget::pane {{
                    border: none;
                    background-color: #2b2b2b;
                    margin: 0px;
                    top: -1px;
                }}
                QTabBar {{ background-color: #2b2b2b; }}
                QTabBar::tab:selected {{
                    background-color: #2b2b2b;
                    color: white;
                }}
                QTabBar::tab {{
                    height: {tab_h}px;
                    background-color: #444444;
                    color: white;
                }}
                """)
        else:
            self.setStyleSheet(f"""
                QTabWidget {{ background-color: #1e1e1e; }}
                QTabWidget::pane {{
                    border: none;
                    background-color: #1e1e1e;
                    margin: 0px;
                    top: -1px;
                }}
                QTabBar {{ background-color: #1e1e1e; }}
                QTabBar::tab:selected {{
                    background-color: #1e1e1e;
                    color: white;
                }}
                QTabBar::tab {{
                    height: {tab_h}px;
                    background-color: #444444;
                    color: white;
                }}
                """)

        self.focus = qt.QToolButton(icon=qt.QIcon(os.path.join(__icons__, "dots.png")))
        self.setCornerWidget(self.focus, qt.Qt.TopRightCorner)
        self.focus.setContextMenuPolicy(qt.Qt.CustomContextMenu)
        self.focus.customContextMenuRequested.connect(self.focus_menu)

        self.tabCloseRequested.connect(partial(self.tab_close_requested))
        self.currentChanged.connect(partial(self.current_changed))
        self.setTabPosition(qt.QTabWidget.North)

    def removeTab(self, index:int=None, history:bool=True) -> None:
        """Close a tab, running its close event and optionally purging its temp file.

        Args:
            index:    (int): - index of the tab to remove.
            history: (bool): - True to keep the workspace entry and temp file, False to delete them.

        Returns:
            None.
        """
        widget = self.widget(index)
        if widget:
            close_event = qt.QCloseEvent()
            widget.closeEvent(close_event)
            if close_event.isAccepted():
                self.dataRemoved.emit(True)

                if self.workspace and not history:
                    data = kcore.json.read(self.workspace)
                    file_temp_abs = self.widget(index).accessibleName()
                    krig_root = os.path.dirname(os.path.dirname(self.workspace))
                    file_temp_rel = _rel(file_temp_abs, krig_root)
                    key = file_temp_rel if file_temp_rel in data else (file_temp_abs if file_temp_abs in data else None)
                    if key:
                        del data[key]
                        kcore.json.write(self.workspace, data)

                    if os.path.exists(file_temp_abs):
                        os.remove(file_temp_abs)

                super().removeTab(index)

    def tab_close_requested(self, index:int=None) -> None:
        """Handle a tab close request by removing the tab without keeping history.

        Args:
            index: (int): - index of the tab requested to close.

        Returns:
            None.
        """
        self.removeTab(index, False)

    def close_file(self, file_path:str=None) -> None:
        """closes the tab for the given file path, if it's currently open.
        used when the underlying script gets removed from disk (e.g. action deletion), so the editor
        doesn't keep a stale tab pointing to a now-missing file.

        Args:
            file_path: (str): - file path to close from the editor.

        Returns:
            None.
        """
        paths = self.current_paths()
        if file_path in paths:
            self.removeTab(paths.index(file_path), False)

    def close_missing_file(self, file_path:str=None) -> None:
        """Ask the user to close and discard, or save the current content to a new path.

        Args:
            file_path: (str): - path of the missing file whose tab should be handled.

        Returns:
            None.
        """
        paths = self.current_paths()
        if file_path not in paths:
            return
        index     = paths.index(file_path)
        file_temp = self.widget(index).accessibleName()
        file_name = os.path.basename(file_path)

        choice = kcore.message.warning(
            title="File Not Found",
            buttons=["Close", "Save to Disk"],
            message_text='"%s" no longer exists on disk.' % file_name,
            informative_text="Close the tab (lose unsaved changes) or save the current content to a new location?",
        )

        if choice == 1:  # Save to Disk
            ext     = file_path.rsplit('.', 1)[-1] if '.' in file_path else 'py'
            filters = "MEL (*.mel);;Python (*.py)" if ext == 'mel' else "Python (*.py);;MEL (*.mel)"
            result  = cmds.fileDialog2(
                fileFilter=filters,
                dialogStyle=2,
                fileMode=0,
                caption="Save Script As",
                dir=os.path.dirname(file_path),
            )
            new_path = result[0] if result else None
            if new_path:
                content = kcore.folder.read(file_temp)   # read before close deletes temp
                self.close_file(file_path)
                kcore.folder.create_file(new_path)
                kcore.folder.write(new_path, content)
                self.open_file(new_path)
                return

        self.close_file(file_path)

    def current_changed(self, index:int=None) -> None:
        """Placeholder hook called when the current tab changes.

        Args:
            index: (int): - index of the newly selected tab.

        Returns:
            None.
        """
        pass

    def focus_menu(self, position=None) -> None:
        """Build and show a popup listing every open tab for quick navigation.

        Args:
            position: (object): - local position where the context menu was requested.

        Returns:
            None.
        """
        self.popup = qt.QMenu(self.focus)
        for index in range(self.count()):
            name = self.tabText(index)
            action = self.popup.addAction(name)
            if name == "Python":
                action.setIcon(qt.QIcon(os.path.join(__icons__, "python_compiled.png")))

            if name == "MEL":
                action.setIcon(qt.QIcon(os.path.join(__icons__, "mel_compiled.png")))

            if name.endswith(".py"):
                action.setIcon(qt.QIcon(os.path.join(__icons__, "editor_python2.png")))

            if name.endswith(".mel"):
                action.setIcon(qt.QIcon(os.path.join(__icons__, "editor_mel.png")))

            if name.endswith(".txt"):
                action.setIcon(qt.QIcon(os.path.join(__icons__, "file.png")))

            if name.endswith(".json"):
                action.setIcon(qt.QIcon(os.path.join(__icons__, "json.png")))

            if index == self.currentIndex():
                font = qt.QFont()
                font.setBold(True)
                action.setFont(font)

            action.triggered.connect(partial(self.set_current_tab_index, index))
        self.popup.exec_(self.focus.mapToGlobal(position))

    def set_current_tab_index(self, index:int=None) -> None:
        """Set the active tab to the given index, ignoring invalid values.

        Args:
            index: (int): - index of the tab to activate.

        Returns:
            None.
        """
        # Exception, not a bare except: a bare one also catches KeyboardInterrupt and SystemExit,
        # so a maya shutdown arriving here would be swallowed instead of allowed through
        try:
            self.setCurrentIndex(index)
        except Exception:
            pass

    @staticmethod
    def _is_referenced_code(file_path:str=None) -> bool:
        """True when a code file is a kata reference: its folder carries a code_reference marker, or it is locked
        read-only (references are OS read-only, editable only in their source krig).

        Args:
            file_path: (str): - the absolute code file path backing a tab.

        Returns:
            bool: True when the code is a read-only reference.
        """
        if not (file_path and os.path.isfile(file_path)):
            return False
        if os.path.isfile(os.path.join(os.path.dirname(file_path), "code_reference")):
            return True
        return not os.access(file_path, os.W_OK)

    def _mark_reference_tab(self, index:int=None, file_path:str=None) -> None:
        """Colour a tab's title blue when its code is a reference (read-only, synced from another krig).

        Args:
            index:     (int): - the tab index.
            file_path: (str): - the code file backing that tab.

        Returns:
            None.
        """
        if index is None or index < 0:
            return
        if self._is_referenced_code(file_path):
            self.tabBar().setTabTextColor(index, qt.QColor(90, 150, 230))
            self.setTabToolTip(index, "Reference (read-only, synced from its source krig)")

    def open_temp(self, file_temp:str=None) -> None:
        """Reopen a tab from a stored temporary file recorded in the workspace.

        Args:
            file_temp: (str): - temporary file key stored in the workspace json.

        Returns:
            None.
        """
        if self.workspace:
            codes = kcore.json.read(self.workspace)
            file_name, file_path = codes[file_temp]
            krig_root = os.path.dirname(os.path.dirname(self.workspace))
            file_temp_abs = _abs(file_temp, krig_root)
            file_path_abs = _abs(file_path, krig_root)
            code_widget = CodeWidget(workspace=self.workspace, file_temp=file_temp_abs, file_path=file_path_abs)
            code_widget.setObjectName(file_path_abs)
            code_widget.setAccessibleName(file_temp_abs)
            code_widget.text_has_been_changed.connect(lambda message: self.compare_code(message, file_path=file_path_abs, file_temp=file_temp_abs))
            code_widget.savingScript.connect(lambda x: self.save_code(x, file_path=file_path_abs, file_temp=file_temp_abs))
            self.addTab(code_widget, self.icon_from_path(file_temp_abs), file_name)
            self._mark_reference_tab(self.count() - 1, file_path_abs)
            self.setVisible(True)
            self.setCurrentIndex(self.count() -1)

    def open_file(self, file_path:str=None) -> None:
        """Open a script file in a new tab, creating a backing temporary copy.

        Args:
            file_path: (str): - path to the script file to open.

        Returns:
            None.
        """
        if self.workspace:
            codes = kcore.json.read(self.workspace)
            if os.path.exists(file_path):
                files = self.current_paths()
                if file_path in files:
                    self.setCurrentIndex(files.index(file_path))
                    return

                file_name = os.path.basename(file_path)
                if os.path.basename(file_path).endswith(('.py','.mel')):
                    file_temp = os.path.join(os.path.dirname(self.workspace), self.temporary_name(file_path.rsplit('.')[-1]))
                    kcore.folder.create_file(file_temp)
                    kcore.folder.write(file_temp, kcore.folder.read(file_path))
                    krig_root = os.path.dirname(os.path.dirname(self.workspace))
                    codes[_rel(file_temp, krig_root)] = [file_name, _rel(file_path, krig_root)]
                    kcore.json.write(self.workspace, codes)
                    code_widget = CodeWidget(workspace=self.workspace, file_temp=file_temp, file_path=file_path)
                    code_widget.setObjectName(file_path)
                    code_widget.setAccessibleName(file_temp)
                    code_widget.text_has_been_changed.connect(lambda message: self.compare_code(message, file_path=file_path, file_temp=file_temp))
                    code_widget.savingScript.connect(lambda x: self.save_code(x, file_path=file_path, file_temp=file_temp))
                    self.addTab(code_widget, self.icon_from_path(file_temp), file_name)
                    self._mark_reference_tab(self.count() - 1, file_path)
                    self.setVisible(True)
                    self.setCurrentIndex(self.count() -1)

    def goto_error(self, file_path:str=None, line:int=None) -> None:
        """Open (or focus) `file_path` and move the caret to `line` — used by the output console when a
        traceback line is double-clicked.

        Args:
            file_path: (str): - the script file from the traceback.
            line:      (int): - the 1-based line number to jump to.

        Returns:
            None.
        """
        if not file_path:
            return
        # focus the tab if already open, otherwise open it (only if the file still exists on disk)
        paths = self.current_paths()
        norm  = os.path.normpath(file_path)
        target_index = next((i for i, p in enumerate(paths) if os.path.normpath(p) == norm), None)
        if target_index is not None:
            self.setCurrentIndex(target_index)
        elif os.path.exists(file_path):
            self.open_file(file_path)
        else:
            return

        code_widget = self.widget(self.currentIndex())
        editor = getattr(code_widget, "code", None)
        if editor is not None and line:
            block  = editor.document().findBlockByNumber(max(0, int(line) - 1))
            cursor = editor.textCursor()
            cursor.setPosition(block.position())
            editor.setTextCursor(cursor)
            editor.centerCursor()
            editor.setFocus()

    def compare_code(self, message:str=None, file_path:str=None, file_temp:str=None, shearch_and_replace_widget:bool=None) -> None:
        """function that compare given message with the current tab layout text.
        if it's a temp file, will auto save, otherwhise will rename the editor with a *.

        Args:
            message:                     (str): - string to compare to a file path.
            file_path:                   (str): - string path to compare with message.
            file_temp:                   (str): - temporary file to check.
            shearch_and_replace_widget: (bool): - QtabWidget.

        Returns:
            None.
        """
        kcore.folder.write(file_temp, message)
        if file_path:
            if not os.path.exists(file_path):
                self.close_missing_file(file_path)
                return
            index = self.current_paths().index(file_path)
            tab_name = self.tabText(index).replace('*','')
            if kcore.folder.read(file_path) != message:
                self.setTabText(index, "*.".join(tab_name.rsplit('.')))
            else:
                self.setTabText(index, tab_name)

        #if shearch_and_replace_widget:
        #    shearch_text(directory=None, tab_layout=tab_layout, index=None, shearch=None, widget=shearch_and_replace_widget)

    def save_code(self, message:str=None, file_path:str=None, file_temp:str=None) -> None:
        """function that will save temp code inside finale code.

        Args:
            message:   (str): - code content (unused, taken from temp file).
            file_path: (str): - string path to compare with message.
            file_temp: (str): - temporary file to check.

        Returns:
            None.
        """
        file_path = file_path if file_path else self.widget(self.currentIndex()).objectName()
        file_temp = file_temp if file_temp else self.widget(self.currentIndex()).accessibleName()

        kcore.folder.write(file_path, kcore.folder.read(file_temp))
        index = self.current_paths().index(file_path)
        self.setTabText(index, self.tabText(index).replace('*',''))

    def icon_from_path(self, file_path:str=None) -> qt.QIcon:
        """Return an editor icon matching the given file's extension.

        Args:
            file_path: (str): - path used to select the matching icon.

        Returns:
            qt.QIcon: icon for the file type, empty icon if the path is missing.
        """
        if os.path.exists(file_path):
            if file_path.endswith(".py"):
                return qt.QIcon(os.path.join(__icons__, "editor_python2.png"))

            elif file_path.endswith(".mel"):
                return qt.QIcon(os.path.join(__icons__, "editor_mel.png"))

            else:
                return qt.QIcon(os.path.join(__icons__, "file_blue.png"))
        return qt.QIcon()

    def current_paths(self) -> list:
        """Return the file paths (or temp names) backing every open tab.

        Returns:
            list: file paths for each open tab in order.
        """
        paths = []
        for i in range(self.count()):
            file_path = self.widget(i).objectName()
            paths.append(file_path if file_path != "" else self.widget(i).accessibleName())

        return paths

    def temporary_name(self, format:str=None) -> str:
        """Return a timestamped temporary file name with the given extension.

        Args:
            format: (str): - file extension to append to the generated name.

        Returns:
            str: unique temporary file name built from the current time.
        """
        now = datetime.datetime.now()
        timestamp = now.strftime("%Y%m%d_%H%M%S")
        return f"temp_{timestamp}.{format}"

    def reload_codes(self, workspace:str=None) -> None:
        """Reconcile every open tab with its file on disk, prompting on conflicts.

        Args:
            workspace: (str): - path to the workspace json (unused).

        Returns:
            None.
        """
        missing = [self.widget(i).objectName() for i in range(self.count())
                   if not os.path.exists(self.widget(i).objectName())]
        for fp in missing:
            self.close_missing_file(fp)

        for i in range(self.count()):
            file_path, file_temp = self.widget(i).objectName(), self.widget(i).accessibleName()
            tab_name = self.tabText(i)

            if not tab_name.count("*"):
                tab_name = tab_name.replace('*','')
                if kcore.folder.read(file_path) != kcore.folder.read(file_temp):
                    self.setTabText(i, "*.".join(tab_name.rsplit('.')))
                    warning = kcore.message.warning(title="Warning", buttons=["Reload", "Save"], message_text='current "%s" has been modified.            '% tab_name, informative_text="do you want to reload code or save/override current?             ")
                    if warning == 0:
                        # appendPlainText on CodeWidget now REPLACES content (setPlainText under the hood),
                        # so reloading no longer duplicates.
                        self.widget(i).appendPlainText(kcore.folder.read(file_path))
                        self.setTabText(i, tab_name)
                    elif warning == 1:
                        kcore.folder.write(file_path, kcore.folder.read(file_temp))
                        self.setTabText(i, tab_name)


class CodeWidget(qt.QWidget):
    """Editor tab pairing a code text edit with a search-and-replace bar."""
    text_has_been_changed = qt.signal(object)
    savingScript = qt.signal(object)

    def __init__(self, workspace:str=None, file_temp:str=None, file_path:str=None) -> None:
        """Initialize the code editor, its search bar and keyboard shortcuts.

        Args:
            workspace: (str): - path to the workspace json backing the tab.
            file_temp: (str): - temporary file backing the editor content.
            file_path: (str): - path of the script file being edited.

        Returns:
            None.
        """
        super().__init__()
        self.code = editor.CodeTextEdit(file_temp=file_temp, file_path=file_path)
        self.code.set_completer(editor.CodeCompleter)

        self.file_temp = file_temp
        self.file_path = file_path
        self.workspace = workspace

        self.code.setDocumentTitle(file_temp)
        self.code.setObjectName(file_path)
        self.code.setAccessibleName(file_temp)

        self.code.text_has_been_changed.connect(self.text_has_been_changed)
        self.code.savingScript.connect(self.savingScript)

        self.search_bar = editor.SearchReplaceBar(self.code)

        self.mainLayout = qt.QGridLayout(self)
        self.mainLayout.setContentsMargins(0, 0, 0, 0)
        self.mainLayout.setSpacing(0)
        self.mainLayout.addWidget(self.code, 0, 0)
        self.mainLayout.addWidget(self.search_bar, 1, 0)

        shortcut_find = qt.QShortcut(qt.QKeySequence("Ctrl+F"), self)
        shortcut_find.setContext(qt.Qt.WidgetWithChildrenShortcut)
        shortcut_find.activated.connect(lambda: self.search_bar.show_bar('search'))

        shortcut_replace = qt.QShortcut(qt.QKeySequence("Ctrl+H"), self)
        shortcut_replace.setContext(qt.Qt.WidgetWithChildrenShortcut)
        shortcut_replace.activated.connect(lambda: self.search_bar.show_bar('replace'))

        shortcut_esc = qt.QShortcut(qt.QKeySequence("Escape"), self)
        shortcut_esc.setContext(qt.Qt.WidgetWithChildrenShortcut)
        shortcut_esc.activated.connect(self.on_escape)

        self.show()

    def on_escape(self) -> None:
        """Close the search bar when Escape is pressed and it is visible.

        Returns:
            None.
        """
        if self.search_bar.isVisible():
            self.search_bar.close_bar()

    def closeEvent(self, event=None) -> None:
        """Prompt to save unsaved changes before the tab closes.

        Args:
            event: (object): - Qt close event accepted or ignored based on the choice.

        Returns:
            None.
        """
        if os.path.exists(self.file_path) and os.path.exists(self.file_temp):
            if kcore.folder.read(self.file_path) != kcore.folder.read(self.file_temp):
                confirmation = kcore.message.information("Save", ["Yes", "No", "Cancel"], '"%s" has been modified. Save Changes?       '% os.path.basename(self.file_path))
                if confirmation == 0:
                    kcore.folder.write(self.file_path, kcore.folder.read(self.file_temp))
                    event.accept()

                elif confirmation == 1:
                    event.accept()

                elif confirmation == 2:
                    event.ignore()
            else:
                event.accept()
        else:
            event.accept()

    def appendPlainText(self, message:str=None) -> None:
        """Replace the editor content with the given text.

        Args:
            message: (str): - text to display in the editor.

        Returns:
            None.
        """
        self.code.setPlainText(message or "")   # replaces content in one shot (not clear()+append)
