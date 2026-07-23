"""
KATA. (c)

Author: Gregoire Dehame
Created: Jul 21, 2026
Module: ui.code_editor.core.compat
Execute: from kata.ui.code_editor.core import compat

Tiny self-contained helpers so `code/` needs nothing from kata's core/util. Provides the few utilities the
editor/output use: read a text file, an input prompt, clipboard copy, and a DPI scale.
"""

import os
from . import qt


class folder:
    """kata.core.folder shim (only what the editor uses)."""

    @staticmethod
    def read(path:str=None) -> str:
        """Return the text content of a file, tolerant of non-UTF-8 encodings.

        Tries UTF-8 (with BOM), then common Windows/Latin encodings, and finally decodes UTF-8 with
        invalid bytes replaced so opening a mis-encoded or partly binary file never raises.
        """
        with open(path, "rb") as fh:
            raw = fh.read()
        for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def write(path:str=None, string:str=None) -> str:
        """Write utf-8 text to a file."""
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(string or "")
        return path


class message:
    """kata.core.message shim (only what the editor uses).

    Every dialog takes a `parent`, and every caller passes the widget it was raised from. That is not
    politeness: a Qt stylesheet travels down the PARENT chain, so a dialog created with parent=None
    is styled by nothing and comes up in the host application's default grey. Parented to a widget
    inside the editor, it inherits the editor's sheet - including the theme that editor happens to
    be running, which a module-level stylesheet could not do with two editors open at once.
    """

    @staticmethod
    def prompt(title:str="Prompt", label:str="", text:str="", multilines:bool=False,
               parent=None) -> str:
        """Single/multi-line input dialog. Returns the entered text or None if cancelled."""
        if multilines:
            value, ok = qt.QInputDialog.getMultiLineText(parent, title, label, text)
        else:
            value, ok = qt.QInputDialog.getText(parent, title, label, qt.QLineEdit.Normal, text)
        return value if ok else None

    @staticmethod
    def _box(title:str, message_text:str, informative_text:str, buttons:list, icon, parent=None) -> str:
        """Message box with named buttons. Returns the clicked button's label (or None)."""
        box = qt.QMessageBox(parent)
        box.setWindowTitle(title or "")
        box.setText(message_text or "")
        if informative_text:
            box.setInformativeText(informative_text)
        box.setIcon(icon)
        refs = {box.addButton(label, qt.QMessageBox.ActionRole): label for label in (buttons or ["OK"])}
        box.exec_() if hasattr(box, "exec_") else box.exec()
        return refs.get(box.clickedButton())

    @staticmethod
    def critical(title:str="Error", message_text:str="", informative_text:str="", buttons:list=None,
                 parent=None) -> str:
        """Critical (error) message box. Returns the clicked button's label."""
        return message._box(title, message_text, informative_text, buttons,
                            qt.QMessageBox.Critical, parent)

    @staticmethod
    def warning(title:str="Warning", message_text:str="", informative_text:str="", buttons:list=None,
                parent=None) -> str:
        """Warning message box. Returns the clicked button's label."""
        return message._box(title, message_text, informative_text, buttons,
                            qt.QMessageBox.Warning, parent)

    @staticmethod
    def file(title:str="Open File", directory:str="", filter:str="All Files (*.*)", parent=None) -> str:
        """Open-file dialog. Returns the chosen path or None if cancelled.

        Left NATIVE on purpose: the OS dialog brings recent places, drive shortcuts and shell
        integration that a styled Qt copy would throw away for the sake of matching colours.
        """
        path, _ = qt.QFileDialog.getOpenFileName(parent, title, directory or "", filter)
        return path or None

    @staticmethod
    def save(title:str="Save As", directory:str="", filter:str="All Files (*.*)", parent=None) -> str:
        """Save-file dialog. Returns the chosen path or None if cancelled."""
        path, _ = qt.QFileDialog.getSaveFileName(parent, title, directory or "", filter)
        return path or None


def copy(string:str=None) -> None:
    """Copy `string` to the system clipboard (kata.ui.util.copy replacement)."""
    try:
        import pyperclip
        pyperclip.copy(string or "")
        return
    except Exception:
        pass
    try:
        qt.QApplication.clipboard().setText(string or "")
    except Exception:
        pass


def scale_dpi(value):
    """Multiply a value by Maya's DPI scale (kata.ui.util.scale_dpi replacement)."""
    return round(value * qt.scale_multiplier)
