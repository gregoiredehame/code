"""
KATA. (c)

Author: Gregoire Dehame
Created: Wed 11, 2024
Modified: Jul 21, 2026
Module: ui.code_editor
Execute: from kata.ui.kata_manager import code

Code editor package:

    main.py      Entry point for the standalone window: show() / close() driving a Maya workspaceControl.
                 Opened with:  from kata.ui import main ; main.code_editor()
    window.py    The standalone window itself (Editor): File/Edit/View/Run menu, file-tree sidebar,
                 direct-to-disk files, isolated namespace. Built only on core/.

    core/        The self-contained editor engine (CodeTextEdit, output console, PySide wrapper, dialog
                 and workspaceControl shims, session store, icons). Imports NOTHING from the rest of
                 kata, so it can be reused - or lifted out - freely.

    manager/     The code editor as it lives inside kata_manager's "Codes" tab. It is the SAME
                 window.Editor built with chrome=False - tabs over the panel, no menu bar, no activity
                 bar, no Explorer or Source Control - with its session pointed at the krig workspace,
                 so open tabs and unsaved buffers live under codes/ and travel with the project.
                 One editor, two chrome levels: a fix to one lands in both.

Editing is non-destructive on two levels: a script on disk is untouched until Ctrl+S, and that save goes
through an atomic temp-then-replace (core/session.write_atomic) so a failure can never truncate it.
Whatever is typed but not saved is mirrored to a backup file, which is deleted the moment it is saved.

No third-party dependency: everything here runs on Maya's stock python. The Problems panel is powered by
core/lint.py, a scope-aware checker written on the standard `ast` module and validated against pyflakes
(same findings on undefined names, unused imports and unused locals, with no extra false positive).
"""


def reload_stack(verbose:bool=True) -> None:
    """Reload the whole editor package, in dependency order, without restarting Maya.

    Both entry points need this and neither may keep its own list, or one of them silently runs stale
    code: the standalone window (main.show) and kata_manager, which caches `code.manager.panel` from
    the first launch and would otherwise never see an edit at all.

    Failures are printed, never swallowed: a reload that fails halfway leaves the OLD module in memory,
    and a change that simply "does not show up" is the hardest kind of bug to chase.
    """
    import importlib
    import pkgutil
    import traceback

    from . import core as core_package

    # The core modules are DISCOVERED, not listed. A hand-kept list has to be edited whenever a
    # module is added - and since reload_stack lives in this file, which cannot reload itself, that
    # edit only takes effect one launch later. The symptom is nasty and hard to read: half the
    # package running new code against the other half's old data.
    ordered = ["qt", "compat", "dock", "vcs", "seti_map", "editor", "languages", "find",
               "palette", "lint", "session", "mel2py", "signature", "console"]
    found = [name for _finder, name, _pkg in pkgutil.iter_modules(core_package.__path__)]
    names = ordered + sorted(name for name in found if name not in ordered)

    modules = []
    for name in names:
        if name not in found:
            continue                        # listed above but deleted since
        try:
            modules.append(importlib.import_module(".core." + name, __name__))
        except Exception:
            if verbose:
                print("[code_editor] import failed for core.%s:" % name)
                traceback.print_exc()

    from .core import editor, console        # named below, after everything has been reloaded
    from . import window
    from .manager import workspace, panel
    modules += [window, workspace, panel]    # then the window, then what embeds it

    for module in modules:
        try:
            importlib.reload(module)
        except Exception:
            if verbose:
                print("[code_editor] reload failed for %s:" % module.__name__)
                traceback.print_exc()

    # both editors draw on the dark surfaces; the shared engine still defaults to the lighter tint
    editor.__light__ = False
    console.__light__ = False
    editor.__surface__ = "#121314"          # gutter + current line match the editor background