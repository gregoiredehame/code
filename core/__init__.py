"""
CODE EDITOR.

Author: Gregoire Dehame
Created: Jul 21, 2026
Module: code_editor.core
Execute: from code_editor import core

Self-contained editor engine: the reusable pieces both the standalone window and the host panel
build on, with NO dependency on the rest of the host. Modules:

    qt       - PySide binding wrapper (PySide6/2 detection, signal(), wrap_instance(), stylesheet, DPI).
    compat   - I/O + dialog shims (folder.read/write, message.prompt/critical/warning/file/save, copy).
    dock     - Maya workspaceControl lifecycle (screen_size, create/delete/restore, __dockable__ map).
    editor   - CodeTextEdit (the code editor), CodeCompleter, SearchReplaceBar, syntax highlighters.
    console  - the Maya output console widget (stdout/stderr capture, clickable tracebacks).
    icons/   - the icon set used by editor/console.
"""
