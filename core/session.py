"""
CODE EDITOR.

Author: Gregoire Dehame
Created: Jul 22, 2026
Modified: Aug 01, 2026
Module: code_editor.core.session
Execute: from code_editor.core import session

Persistence for the editor: which tabs were open, and a backup of whatever was typed but not saved.

Two things are stored, and they answer two different questions:

    session.json    the layout - open tabs, their order, which one was active, where the caret was.
                    Reopening the editor puts you back exactly where you left off.
    backups/        a copy of every DIRTY buffer, refreshed as you type. It exists only so a Maya
                    crash (or closing the window and answering "Don't Save" by accident) does not
                    take unsaved work with it.

The backups are deliberately NOT a working copy: the editor never writes to the real file until you
save, so the original is already safe. What the real file needs is a save that cannot half-write it,
which is what `write_atomic` below is for - a sibling temp file swapped in with os.replace(), an
operation the filesystem guarantees is all-or-nothing.
"""

import os
import json
import hashlib


def _root() -> str:
    """The per-user directory holding the editor's state (created on demand).

    Returns:
        str: the per-user code_editor state directory.
    """
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
            or os.path.expanduser("~"))
    return os.path.join(base, "the host", "code_editor")


def write_atomic(path:str, text:str) -> None:
    """Write `text` to `path` without ever leaving it truncated.

    A plain open(path, "w") empties the file first, so anything that goes wrong between that and the
    last byte written destroys the previous content. Writing a sibling temp and swapping it in makes
    the change atomic: readers see either the old file or the new one, never a half of either.

    Args:
        path: (str): - file path to write to.
        text: (str): - text content to write.

    Returns:
        None.
    """
    folder = os.path.dirname(os.path.abspath(path)) or "."
    temporary = os.path.join(folder, ".%s.the host-tmp" % os.path.basename(path))
    try:
        with open(temporary, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())        # the bytes are on disk before the swap, not just cached
        os.replace(temporary, path)
    except Exception:
        try:
            os.remove(temporary)
        except Exception:
            pass
        raise


class Session(object):
    """The state of one editor, keyed by `name` so several can coexist in the same folder.

    `folder` decides WHERE the state lives, and that is the whole difference between the two editors:
    the standalone window keeps it per-user, while the host panel points it at the krig
    workspace so the backups travel with the project instead of with the machine.
    """

    def __init__(self, name:str="standalone", folder:str=None, enabled:bool=True) -> None:
        """Set up the session paths for editor `name` under `folder`.

        Args:
            name:     (str): - editor name, keys the session file.
            folder:   (str): - directory holding the state; per-user root default.
            enabled: (bool): - False remembers nothing, e.g. no project is loaded.

        Returns:
            None.
        """
        self.name = name
        self.enabled = enabled           # False = remember nothing, e.g. no project is loaded
        self.folder = folder or _root()
        self.backups = os.path.join(self.folder, "backups")
        self.path = os.path.join(self.folder, "%s.json" % name)

    # ---- layout

    def load(self) -> dict:
        """The stored layout, or an empty one when there is nothing (or it is unreadable).

        Returns:
            dict: the stored layout, or {} when missing or unreadable.
        """
        if not self.enabled:
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save(self, data:dict) -> None:
        """Store the layout. Failures stay silent: losing a session must never break the editor.

        Args:
            data: (dict): - the layout to store.

        Returns:
            None.
        """
        if not self.enabled:
            return
        try:
            os.makedirs(self.folder, exist_ok=True)
            write_atomic(self.path, json.dumps(data, indent=2))
        except Exception:
            pass

    # ---- backups of unsaved buffers

    @staticmethod
    def key(path:str, fallback:str="") -> str:
        """A stable filename-safe id for a buffer (its path, or `fallback` for untitled ones).

        Args:
            path:     (str): - buffer file path.
            fallback: (str): - id seed for untitled buffers.

        Returns:
            str: an md5 hex id.
        """
        seed = os.path.normcase(os.path.abspath(path)) if path else "untitled:%s" % fallback
        return hashlib.md5(seed.encode("utf-8", "replace")).hexdigest()

    def _backup_path(self, key:str) -> str:
        """The on-disk path of the backup file for `key`.

        Args:
            key: (str): - buffer id.

        Returns:
            str: the .bak file path.
        """
        return os.path.join(self.backups, "%s.bak" % key)

    def backup(self, key:str, text:str) -> None:
        """Keep a copy of an unsaved buffer.

        Args:
            key:  (str): - buffer id.
            text: (str): - buffer content.

        Returns:
            None.
        """
        if not self.enabled:
            return
        try:
            os.makedirs(self.backups, exist_ok=True)
            write_atomic(self._backup_path(key), text)
        except Exception:
            pass

    def recover(self, key:str) -> str:
        """The stored copy of an unsaved buffer, or None.

        Args:
            key: (str): - buffer id.

        Returns:
            str: the backup content, or None when absent.
        """
        if not self.enabled:
            return None
        try:
            with open(self._backup_path(key), "r", encoding="utf-8", newline="") as handle:
                return handle.read()
        except Exception:
            return None

    def discard(self, key:str) -> None:
        """Drop a backup: the buffer was saved or closed, so it is no longer at risk.

        Args:
            key: (str): - buffer id.

        Returns:
            None.
        """
        if not self.enabled:
            return
        try:
            os.remove(self._backup_path(key))
        except Exception:
            pass

    def sweep(self, keep:set) -> None:
        """Delete every backup whose key is not in `keep` (tabs closed while we were not looking).

        Args:
            keep: (set): - buffer ids to keep.

        Returns:
            None.
        """
        if not self.enabled:
            return
        try:
            for name in os.listdir(self.backups):
                if name.endswith(".bak") and name[:-4] not in keep:
                    os.remove(os.path.join(self.backups, name))
        except Exception:
            pass
