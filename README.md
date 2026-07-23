<p align=center>CODE EDITOR.</p>
<p align=center> A proper code editor for Autodesk Maya. Python, MEL, git, inside your session.</p>


<img width="1916" height="1125" alt="Capture d&#39;écran 2026-07-22 210640" src="https://github.com/user-attachments/assets/9d011020-940b-4991-af8e-80889176150c" />


---


 Supported Maya Versions
-----------------------

 The editor supports six major versions of Maya:
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
 nothing else. That includes its own Qt wrapper, its own linter, and its own MEL to Python
 translator, written because pymel is gone from Maya 2026.

 `pyperclip` is used for the clipboard when it happens to be installed, and quietly ignored when it
 is not.


 Interface
-----------------------
 Open it as a floating window:

```py
from code_editor import main
main.show()
```

 Or docked into a Maya panel. It comes back where you left it after a restart:

```py
from code_editor import main
main.show(parent="AttributeEditor")
```

 Add it to Maya's menus, above the Script Editor. Call this from your `userSetup.py`: Maya rebuilds
 its menus on every launch, so nothing installed into them persists on its own.

```py
from code_editor import main
main.install_menu()                          # Windows > General Editors > Code Editor
main.install_menu(before=None)               # at the bottom of General Editors instead
main.install_menu(menu=None, before=None)    # at the bottom of the Windows menu
```

 If a placement ever goes wrong (dragged off-screen, or tabbed into a panel that no longer
 exists), the gear menu at the foot of the activity bar has **Reset Window Placement**.


 Feature Comparison
-----------------------

 The right-hand column is Maya's own Script Editor, as shipped in 2025 / 2026.

### General

| | Code Editor | Maya Script Editor |
|---|:---:|:---:|
| Modern user interface | ✓ | |
| Dockable Maya panel | ✓ | ✓ |
| Placement restored after a Maya restart | ✓ | ✓ |
| Multi-document (tabs) | ✓ | ✓ |
| Preview tabs (single click opens, replaces) | ✓ | |
| Movable tabs | ✓ | ✓ |
| Open editors list | ✓ | |
| Workspace file tree | ✓ | |
| Breadcrumbs with folder drop-downs | ✓ | |
| Right-click context menus | ✓ | ✓ |
| Command palette | ✓ | |
| Colour themes | 2 | |
| Non-destructive editing (temp file until save) | ✓ | |
| Tabs, layout and geometry restored per session | ✓ | ✓ |
| Embeddable as a panel in another tool | ✓ | |

### Editing

| | Code Editor | Maya Script Editor |
|---|:---:|:---:|
| Python syntax highlighting | ✓ | ✓ |
| MEL syntax highlighting | ✓ | ✓ |
| Other languages (json, xml, cpp, md, …) | ✓ | |
| Bracket pair colourisation by depth | ✓ | |
| Bracket match highlighting | ✓ | ✓ |
| Indent guides | ✓ | |
| Smart indenting | ✓ | ✓ |
| Auto-closing brackets and quotes | ✓ | |
| Toggle comment | ✓ | ✓ |
| Line numbers | ✓ | ✓ |
| Minimap | ✓ | |
| Sticky scroll (enclosing class / def pinned) | ✓ | |
| Code folding | ✓ | |
| Word wrap | ✓ | ✓ |
| Zoom (Ctrl + wheel, remembered) | ✓ | ✓ |
| Multiple cursors | ✓ | |
| Column selection (Alt + drag) | ✓ | |
| Expand / shrink selection | ✓ | |
| Move and copy lines | ✓ | |
| Duplicate line / selection | ✓ | ✓ |
| Sort lines, join lines, change case | ✓ | |
| Trim trailing whitespace, optionally on save | ✓ | |
| Highlight other occurrences of the symbol | ✓ | |
| TODO / FIXME markers in the gutter | ✓ | |

### Search & Navigation

| | Code Editor | Maya Script Editor |
|---|:---:|:---:|
| Find and replace in the file | ✓ | ✓ |
| Regex, case and whole-word options | ✓ | ✓ |
| Find in selection | ✓ | |
| Find and replace across the workspace | ✓ | |
| Go to file | ✓ | |
| Go to symbol in the file | ✓ | |
| Go to definition (F12) | ✓ | |
| Rename symbol (F2) | ✓ | |
| Go to line / column | ✓ | ✓ |
| Go to bracket | ✓ | |
| Outline that follows the caret | ✓ | |
| Back / forward navigation history | ✓ | |
| Next / previous problem | ✓ | |
| Next / previous git change | ✓ | |

### Completion & Help

| | Code Editor | Maya Script Editor |
|---|:---:|:---:|
| Completion from the live session | ✓ | basic |
| Chained attribute completion (`cmds.poly…`) | ✓ | |
| Symbol icons by kind | ✓ | |
| Call arguments offered inside the brackets | ✓ | |
| Signature tooltip on `(` | ✓ | |
| Quick Help panel | ✓ | |
| Parameter table with types and defaults | ✓ | |
| Per-argument descriptions from docstrings | ✓ | |
| Maya command flags from `cmds.help` | ✓ | ✓ |
| Return type | ✓ | |
| Double-click a parameter to insert it | ✓ | |

### Diagnostics

| | Code Editor | Maya Script Editor |
|---|:---:|:---:|
| Live syntax check | ✓ | |
| Unused names, shadowed imports, bare `except`… | ✓ | |
| CPython compiler warnings | ✓ | |
| Problems panel grouped per file | ✓ | |
| Error and warning tally in the status bar | ✓ | |
| Jump from a traceback to the line | ✓ | ✓ |

### Git

| | Code Editor | Maya Script Editor |
|---|:---:|:---:|
| File status decorations in the tree | ✓ | |
| Source Control view (staged / unstaged) | ✓ | |
| Commit graph | ✓ | |
| File history (Timeline) | ✓ | |
| Diff and patch views | ✓ | |
| Change bars in the gutter | ✓ | |
| Inline peek of what `HEAD` had | ✓ | |
| Revert a single hunk | ✓ | |
| Open on GitHub | ✓ | |
| Stage, commit, push | | |

### Execution & Console

| | Code Editor | Maya Script Editor |
|---|:---:|:---:|
| Run all | ✓ | ✓ |
| Run selection | ✓ | ✓ |
| Run current line | ✓ | |
| Never deletes the code on execute | ✓ | ✓ |
| Save to shelf | ✓ | ✓ |
| Maya command echo | ✓ | ✓ |
| Echo translated to Python | ✓ | |
| Long flag names (`-constructionHistory`) | ✓ | |
| Output syntax highlighting | ✓ | |
| Interactive prompt sharing the namespace | ✓ | |
| Isolated namespace per editor | ✓ | |

 Staging, committing and pushing are deliberately absent: they reach the network, and a blocking
 subprocess on Maya's main thread freezes the whole application. Use your usual git client.


 Settings
-----------------------

 Everything below is remembered between sessions. Panel toggles and folding live under
 **View**, the rest under the gear at the foot of the activity bar.

| Setting | Values | Default |
|---|---|---|
| Colour theme | Dark (VS Code), Dark (neutral) | Dark (VS Code) |
| Word wrap | on / off | off |
| Minimap | on / off | on |
| Sticky scroll | on / off | on |
| Auto save | on / off | off |
| Trim trailing whitespace on save | on / off | off |
| Font size | Ctrl + wheel, or Ctrl + / Ctrl - | 9 pt |
| Output language | MEL, Python, Python (longname) | Python (longname) |
| Command echo | Echo All, Normal | follows Maya |
| Primary side bar, panel, secondary side bar | shown / hidden, and their widths | shown / shown / hidden |


 Keyboard Shortcuts
-----------------------

| | | | |
|---|---|---|---|
| New file | `Ctrl+N` | Find | `Ctrl+F` |
| Open file | `Ctrl+O` | Replace | `Ctrl+H` |
| Save | `Ctrl+S` | Find in files | `Ctrl+Shift+F` |
| Save as | `Ctrl+Shift+S` | Find all occurrences | `Ctrl+Shift+L` |
| Save all | `Ctrl+Alt+S` | Go to file | `Ctrl+P` |
| Close editor | `Ctrl+W` | Go to symbol | `Ctrl+Shift+O` |
| Close all editors | `Ctrl+K W` | Go to definition | `F12` |
| Reopen closed editor | `Ctrl+Shift+T` | Rename symbol | `F2` |
| Undo / redo | `Ctrl+Z` / `Ctrl+Shift+Z` | Go to line | `Ctrl+G` |
| Toggle comment | `Ctrl+/` | Go to bracket | `Ctrl+Shift+\` |
| Delete line | `Ctrl+Shift+K` | Back / forward | `Alt+←` / `Alt+→` |
| Copy line up / down | `Shift+Alt+↑` / `↓` | Next / previous problem | `F8` / `Shift+F8` |
| Move line up / down | `Alt+↑` / `Alt+↓` | Next / previous change | `Alt+F3` / `Shift+Alt+F3` |
| Expand / shrink selection | `Shift+Alt+→` / `←` | Next / previous editor | `Ctrl+PgDn` / `Ctrl+PgUp` |
| Expand line selection | `Ctrl+L` | Command palette | `Ctrl+Shift+P` |
| Add cursor above / below | `Ctrl+Alt+↑` / `↓` | Explorer | `Ctrl+Shift+E` |
| Add next occurrence | `Ctrl+D` | Source control | `Ctrl+Shift+G` |
| Cancel multiple cursors | `Esc` | Problems | `Ctrl+Shift+M` |
| Join lines | `Ctrl+Shift+J` | Output | `Ctrl+Shift+U` |
| Fold / unfold | `Ctrl+Shift+[` / `]` | Debug console | `Ctrl+Shift+Y` |
| Zoom in / out | `Ctrl+=` / `Ctrl+-` | Toggle side bar | `Ctrl+B` |
| Word wrap | `Alt+Z` | Toggle panel | `Ctrl+J` |
| Run selection | `Ctrl+Enter` | Toggle secondary side bar | `Ctrl+Alt+B` |
| Run all | `Ctrl+Shift+Enter` | | |

 The full list is in **Help > Keyboard Shortcuts Reference**.


 Embedding
-----------------------
 The editor also runs as a panel inside a larger tool: a chrome-less editor scoped to a workspace,
 with the file tree replaced by the host's own tree.

 `manager/` is that bridge, and it is the **only** part of this package that reaches outside it:
 it imports the host's own helpers, so it works only when the package is installed inside one.
 Everything under `core/` and `window.py` depends on nothing but Maya and PySide, and the editor
 loads perfectly well with `manager/` absent or unimportable.

```py
from code_editor.manager import panel
widget = panel.Widget()
```


 Layout
-----------------------
```
core/          the engine: qt wrapper, editor widget, highlighters, linter, git, sessions
window.py      the workbench: tabs, side bars, panels, menus, status bar
main.py        entry points: show(), close(), reset(), install_menu()
manager/       the optional host bridge - the only part that reaches outside the package
```
