"""
KATA. (c)

Author: Gregoire Dehame
Created: Wed 11, 2024
Modified: Jul 22, 2026
Module: ui.code_editor.manager.panel
Execute: from kata.ui.code_editor.manager import panel

The code editor as it lives inside kata_manager's "Codes" tab.

It is the SAME editor as the standalone window, built with chrome=False: the tabs over the panel
(PROBLEMS / OUTPUT / DEBUG CONSOLE) and nothing else - no menu bar, no activity bar, no Explorer or
Source Control. kata_manager already owns the file browsing, so the editor only has to edit.

Everything it remembers lives INSIDE the krig workspace, under codes/:

    codes/session.json   open tabs, their order, the active one, and where each caret was
    codes/backups/       the live buffer of every tab with unsaved work
    codes/data.json      the same tab list in the historical {temp: [title, script]} shape, kept so
                         kata_manager's path migration (ui.update_paths) keeps working unchanged

That makes the editing non-destructive on two levels: the script on disk is never touched until you
hit Ctrl+S (and then through an atomic write, see core.session.write_atomic), and what you typed but
did not save survives a Maya crash - inside the project, so it travels with it.
"""

import os
import logging

from ..core import qt
from ..core import session as session_module
from .. import window as editor_window
from . import workspace as util
from ..... import core as kcore

log = logging.getLogger("code")
log.setLevel(logging.INFO)


def _rel(path:str, root:str) -> str:
    """`path` relative to `root`; left absolute when outside it or on another drive."""
    if not path or not root or not os.path.isabs(path):
        return path
    try:
        relative = os.path.relpath(path, root)
        return relative if not relative.startswith("..") else path
    except ValueError:
        return path


class Widget(qt.QWidget):
    """kata_manager's Codes tab: the editor bound to the current krig workspace."""

    def __init__(self, parent=None, **kwargs) -> None:
        """Build the panel, optionally bound to a workspace given as a `workspace` keyword."""
        super().__init__(parent=parent)
        self.workspace = None                 # path to codes/data.json, or None

        # No engine globals are touched here on purpose: __light__ / __surface__ are module-wide, and
        # this panel can be open at the same time as the standalone window on a different theme. The
        # surface travels per widget instead (Editor(theme=...) -> EditorPage -> CodeTextEdit).

        layout = qt.QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # parented straight away: an unparented Editor would flash as a top-level window first.
        # session_enabled=False until update_workspace binds it: with no krig there is nothing to
        # remember, and it must not read or write the standalone window's per-user state.
        self.editor = editor_window.Editor(parent=self, chrome=False, session_name="session",
                                           session_folder=None, session_enabled=False, theme="kata")
        self.editor.sessionStored.connect(self._mirror_data_json)
        layout.addWidget(self.editor)

        self.update_workspace(kwargs.get("workspace"))   # also scopes the Explorer to nothing when unbound

    # ------------------------------------------------------------------ workspace

    def alive(self) -> bool:
        """True while the embedded editor's C++ side is still there.

        kata_manager keeps calling into this panel on every tab click, and a Maya reload or a stray
        deleteUI can take the editor out from under it. Touching a dead widget raises, and that
        traceback surfaces as a broken manager rather than as a missing editor.
        """
        return qt.is_valid(getattr(self, "editor", None)) and qt.is_valid(self.editor.tabs)

    def _store_folder(self) -> str:
        """Where the editor keeps its state: the workspace's codes/ folder, or nothing when unbound."""
        return os.path.dirname(self.workspace) if self.workspace else None

    def _krig_root(self) -> str:
        """The krig workspace root (codes/data.json sits two levels down)."""
        return os.path.dirname(os.path.dirname(self.workspace)) if self.workspace else ""

    def update_workspace(self, workspace:str=None) -> None:
        """Bind the editor to `workspace` (a codes/data.json path), or unbind it when None.

        Args:
            workspace: (str): - path to the workspace's codes/data.json.

        Returns:
            None.
        """
        if not self.alive():
            return
        valid = bool(workspace) and os.path.basename(workspace) == "%s.json" % util.__data__
        self.workspace = workspace if valid else None

        # the store you are LEAVING is flushed before the switch, so a project you walk away from with
        # unsaved code comes back exactly as you left it. Unbound = remember nothing, touch nothing.
        self.editor.set_session_folder(self._store_folder(), "session", enabled=bool(self.workspace))
        self.apply_scope()

        if self.workspace and not self.editor.tabs.count():
            self._seed_from_data_json()

    # ------------------------------------------------------------------ explorer scope

    def process_tree(self) -> list:
        """The krig's process as Explorer nodes: a Pre group and a Post group, actions nested inside.

        process/config.json holds {"pre": {name: [status, path, parent]}, "post": {...}}: dict order is
        run order, and `parent` is the action an entry hangs under. Folders on disk are irrelevant here
        - what you want to browse is the process, in the order it executes.
        """
        root = self._krig_root()
        config = os.path.join(root, "process", "config.json") if root else ""
        if not config or not os.path.isfile(config):
            return []
        try:
            data = kcore.json.read(config) or {}
        except Exception:
            return []

        return [{"label": title, "children": self._section_nodes(data.get(key) or {}, root)}
                for key, title in (("pre", "Pre"), ("post", "Post"))]

    def _section_nodes(self, entries:dict, root:str) -> list:
        """Turn one section of config.json into nested nodes, preserving its order."""
        children = {}                            # parent action name -> [(name, entry), ...]
        for name, entry in entries.items():
            parent = entry[2] if isinstance(entry, (list, tuple)) and len(entry) > 2 else None
            children.setdefault(parent if parent in entries else None, []).append((name, entry))

        emitted = set()

        def build(name, entry, seen):
            if name in seen:
                return None                      # a parent chain that loops must not recurse forever
            emitted.add(name)
            path = entry[1] if isinstance(entry, (list, tuple)) and len(entry) > 1 else None
            if path and not os.path.isabs(path):
                path = os.path.normpath(os.path.join(root, path))
            missing = not (path and os.path.isfile(path))
            node = {"label": name,
                    "path": None if missing else path,       # a gone script is shown, not openable
                    "dim": missing,
                    "children": [n for n in (build(child, data, seen | {name})
                                             for child, data in children.get(name, [])) if n]}
            if missing:
                node["tooltip"] = "%s  •  script not found" % (path or "no script")
            return node

        nodes = [n for n in (build(name, entry, set()) for name, entry in children.get(None, [])) if n]
        # actions caught in a parent cycle hang off nothing, so the walk above never reaches them.
        # Surface each at the top - tested one at a time, because building one emits the rest of its
        # cycle and they must not then appear a second time.
        for name, entry in entries.items():
            if name in emitted:
                continue
            node = build(name, entry, set())
            if node:
                nodes.append(node)
        return nodes

    def apply_scope(self) -> None:
        """Show the krig's process in the Explorer (an empty tree when no project is loaded)."""
        if not self.alive():
            return
        self.editor.sidebar.workspace.set_tree(self.process_tree() if self.workspace else [])

    def _seed_from_data_json(self) -> None:
        """First open on a project that predates session.json: reopen what data.json listed.

        The old format stored a temp copy per tab; those temps are ignored on purpose. The script paths
        are what matter, and their content on disk is the truth.
        """
        try:
            entries = kcore.json.read(self.workspace) or {}
        except Exception:
            return
        root = self._krig_root()
        for value in entries.values():
            try:
                script = value[1]
            except Exception:
                continue
            if not script:
                continue
            path = script if os.path.isabs(script) else os.path.normpath(os.path.join(root, script))
            if os.path.isfile(path):
                self.editor.open_file(path, preview=False)

    def _mirror_data_json(self, data:dict) -> None:
        """Mirror the editor's tab list into codes/data.json, in its historical shape.

        kata_manager rewrites that file to keep every path relative when a project moves
        (ui.update_paths), and it expects exactly {temp: [title, script]}. Writing it here keeps that
        contract while session.json carries the details the old format could not hold.
        """
        if not self.workspace:
            return
        root, mirror = self._krig_root(), {}
        for entry in data.get("tabs") or []:
            path = entry.get("path")
            if entry.get("kind") != "editor" or not path:
                continue                       # untitled buffers have no script to point at
            backup = os.path.join(self._store_folder(), "backups",
                                  "%s.bak" % session_module.Session.key(path))
            mirror[_rel(backup, root)] = [os.path.basename(path), _rel(path, root)]
        try:
            if kcore.json.read(self.workspace) != mirror:
                kcore.json.write(self.workspace, mirror)
        except Exception:
            kcore.json.write(self.workspace, mirror)

    # ------------------------------------------------------------------ kata_manager API

    def open_file(self, file_path:str=None) -> None:
        """Open a script in a pinned tab.

        Args:
            file_path: (str): - file path to open into the editor.

        Returns:
            None.
        """
        if not self.alive():
            return
        if file_path:
            self.editor.open_file(file_path, preview=False)

    def close_file(self, file_path:str=None) -> None:
        """Close the tab showing `file_path`, if any. Used when the script is deleted on disk.

        Args:
            file_path: (str): - file path to close from the editor.

        Returns:
            None.
        """
        if not self.alive():
            return
        index = self._index_of(file_path)
        if index is not None:
            self.editor.close_tab(index)

    def save_current(self) -> None:
        """Save the active tab to its file.

        Returns:
            None.
        """
        if not self.alive():
            return
        self.editor.save_file()

    def reload_codes(self, workspace:str=None) -> None:
        """Re-sync open tabs with what is on disk, called when the Codes tab regains focus.

        A tab with no unsaved change follows the file silently - that is what you want after a git
        checkout or an edit made elsewhere. A tab that HAS unsaved work is left strictly alone: its
        buffer is the only copy of what you typed, so it is never overwritten behind your back.

        Args:
            workspace: (str): - workspace being reloaded (unused, the binding already holds).

        Returns:
            None.
        """
        if not self.alive():
            return
        for index in range(self.editor.tabs.count()):
            page = self.editor.tabs.widget(index)
            path = getattr(page, "file_path", None)
            if not isinstance(page, editor_window.EditorPage) or not path:
                continue
            if page.is_modified() or not os.path.isfile(path):
                continue
            try:
                on_disk = kcore.folder.read(path)
            except Exception:
                continue
            if on_disk != page.code.toPlainText():
                cursor = page.code.textCursor().position()
                page.code.setPlainText(on_disk)
                page.code.document().setModified(False)
                text_cursor = page.code.textCursor()
                text_cursor.setPosition(min(cursor, len(on_disk)))
                page.code.setTextCursor(text_cursor)
                self.editor._refresh_tab_title(page)
        self.apply_scope()          # actions may have been added or removed from the process meanwhile
        self.editor.problems.refresh()

    def closeEvent(self, event) -> None:
        """Flush the session before kata_manager goes away, rather than waiting on the debounce."""
        try:
            self.editor._store_session()
        except Exception:
            pass
        super().closeEvent(event)

    # ------------------------------------------------------------------ helpers

    def _index_of(self, file_path:str) -> int:
        """The tab index showing `file_path`, or None."""
        if not self.alive():
            return None
        if not file_path:
            return None
        target = os.path.normpath(file_path)
        for index in range(self.editor.tabs.count()):
            path = getattr(self.editor.tabs.widget(index), "file_path", None)
            if path and os.path.normpath(path) == target:
                return index
        return None
