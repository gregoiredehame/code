<p align=center>CODE EDITOR.</p>
<p align=center> A proper code editor for Autodesk Maya. Python, MEL, git, inside your session.</p>


---


 Supported Maya Versions
-----------------------

 The editor supports the same major versions as kata:
- 2022 `python3 | pyside2`
- 2023 `python3 | pyside2`
- 2024 `python3 | pyside2`
- 2025 `python3 | pyside6`
- 2026 `python3 | pyside6`
- 2027 `python3 | pyside6`

 The binding is detected at import time by the package's own wrapper, so nothing has to be
 configured per version.


 Requirements
-----------------------
 None beyond Maya itself.

 No pip packages, no pymel, no bundled binaries. The whole package imports `maya` and PySide and
 nothing else — including its own Qt wrapper, its own linter, and its own MEL to Python translator,
 written because pymel is gone from Maya 2026.

 `pyperclip` is used for the clipboard when it happens to be installed, and quietly ignored when it
 is not.


 Interface
-----------------------
 Open it as a floating window:

```py
from kata.ui.code_editor import main
main.show()
```

 Or docked into a Maya panel — it comes back where you left it after a restart:

```py
from kata.ui.code_editor import main
main.show(parent="AttributeEditor")
```

 Add it to Maya's menus, above the Script Editor. Call this from your `userSetup.py`: Maya rebuilds
 its menus on every launch, so nothing installed into them persists on its own.

```py
from kata.ui.code_editor import main
main.install_menu()                          # Windows > General Editors > Code Editor
main.install_menu(before=None)               # at the bottom of General Editors instead
main.install_menu(menu=None, before=None)    # at the bottom of the Windows menu
```

 If a placement ever goes wrong — dragged off-screen, or tabbed into a panel that no longer exists —
 the gear menu at the foot of the activity bar has **Reset Window Placement**.


 Editing
-----------------------
- Python and MEL highlighting, with bracket pairs coloured by depth, and indentation guides
- Code folding, sticky scroll, breadcrumbs, minimap, and an outline that follows the caret
- Multiple cursors: `Ctrl+D` for the next occurrence, `Ctrl+Shift+L` for all of them, `Alt+Click` to
  place one, `Alt+Drag` for a column selection
- Find and replace in the file, and across every file in the workspace
- `F12` Go to Definition and `F2` Rename Symbol, both resolved inside the current file
- Line transforms: sort, join, change case, and trailing whitespace trimmed on save
- Editing is non-destructive: work goes to a temp file until you save, and open tabs, layout and
  window geometry come back with the next session


 Completion & Quick Help
-----------------------
 Completion is driven by the **live Maya session** through `rlcompleter`, so it completes what
 actually exists: your modules, your variables, `cmds`. Inside a call's brackets it offers that
 call's own parameters first.

 The Quick Help panel shows the signature of the call you are inside, its return type, and a table
 of parameters with their types, defaults and descriptions:

- Python signatures and annotations where they exist — which covers your own functions, and is
  exactly what Maya's own Quick Help cannot see
- `cmds.help` flags for Maya commands, which are C functions and carry no Python signature at all
- Per-argument descriptions read from your own docstrings

 Double-click a row to drop it into the call.


 Diagnostics
-----------------------
 An AST-based checker plus CPython's own compiler warnings, listed per file in a PROBLEMS panel. It
 finds unused names, shadowed imports, mutable default arguments, bare `except`, invalid escape
 sequences and the rest — with no external linter to install.


 Git
-----------------------
 Read-only, deliberately:

- File decorations in the explorer, and a Source Control view splitting staged from unstaged
- A commit graph, and per-file history in the Timeline
- Diffs and patches, change bars in the gutter, and jump to next/previous change
- An inline peek on any change bar showing what `HEAD` had there, with a one-click revert of that
  hunk

 Staging, committing and pushing are **not** here. Those reach the network, and a blocking
 subprocess on Maya's main thread freezes the whole application; use your usual git client.


 Console
-----------------------
 The OUTPUT panel mirrors Maya's command echo, translated from MEL to Python with long flag names
 (`-constructionHistory` rather than `-ch`) by an in-house translator. The DEBUG CONSOLE is an
 interactive prompt sharing the editor's execution namespace, so you can run a script from a tab and
 then poke at its variables.


 Embedding
-----------------------
 The editor also runs as a panel inside a larger tool. `manager/` is that bridge for kata_manager: a
 chrome-less editor scoped to a workspace, with the file tree replaced by the host's own process
 tree.

 It is the only part of this package that assumes a host — everything under `core/` and `window.py`
 depends on nothing but Maya and PySide.

```py
from kata.ui.code_editor.manager import panel
widget = panel.Widget()
```


 Layout
-----------------------
```
core/          the engine: qt wrapper, editor widget, highlighters, linter, git, sessions
manager/       the kata_manager bridge (the only host-aware part)
window.py      the workbench: tabs, side bars, panels, menus, status bar
main.py        entry points: show(), close(), reset(), install_menu()
```
