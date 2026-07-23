"""
CODE EDITOR.

Author: Gregoire Dehame
Created: Jul 21, 2026
Module: code_editor.main
Execute: from code_editor import main

Entry point for the standalone code editor window (dockable). Mirrors the host convention
a show()/close() pair driving a Maya workspaceControl.
"""

# built on the self-contained engine in core (no dependency on the rest of the host)
from .core import qt
from .core import dock
from . import window

__dockable__ = dock.__dockable__
screen_width, screen_height = dock.screen_size()


def _entry(call:str) -> str:
    """A one-liner maya can run later to reach this module.

    Derived from `__name__` rather than written out, so the package works wherever it is installed:
    on its own as `code_editor`, or nested inside a larger tool. Maya stores these strings in the
    workspace prefs and runs them at the NEXT launch - a hard-coded path would break the moment the
    package moved, and the failure would surface a restart later, far from the cause.
    """
    return "import %s as main; main.%s" % (__name__, call)


def _reload_modules() -> None:
    """Reload the editor stack so code changes apply on the next open without restarting Maya.

    The list itself lives in the package, so this entry point and the host's cannot drift apart.
    """
    from . import reload_stack
    reload_stack()


def show(parent:str=None, width:int=None, height:int=None, restore:bool=None) -> "window.Editor":
    """Launch the standalone code editor, docked or floating.

    Args:
        parent:  (str):  - Maya panel name to dock into (e.g. "AttributeEditor"). None = float.
        width:   (int):  - window width in pixels. None uses screen_width / 3.
        height:  (int):  - window height in pixels. None uses screen_height / 1.4.
        restore: (bool): - True restores the panel state after a Maya restart.

    Returns:
        window.Editor: the window instance, or None on restore.
    """
    import maya.cmds as cmds

    if restore and isinstance(restore, bool):
        if window.Editor.window_instance is None:
            window.Editor.window_instance = window.Editor(function_name=("code_editor", parent))
        dock.restore_workspace_control(window.Editor.window_instance.objectName())
        return

    _reload_modules()          # pick up code changes on every open (dev convenience)

    window_name = window.Editor.title + "WorkspaceControl"
    # keep_state: close whatever is open, but leave maya's record of WHERE it was. Removing that
    # record here is what made the panel come back floating after every restart.
    dock.delete_workspace_instances(window_name, keep_state=True)
    window.Editor.window_instance = None

    instance = window.Editor(function_name=("code_editor", parent))
    window.Editor.window_instance = instance

    w = width  if width  else screen_width  / 3
    h = height if height else screen_height / 1.4

    if parent and dock.workspace_exists(workspace=parent):
        cmds.setParent(parent)
        instance.show(dockable=True, area="right", floating=False, width=w, height=h)
        cmds.workspaceControl(
            window_name, edit=True,
            tabToControl=[parent, __dockable__.get(parent, -1)],
            # retain=True is what makes maya write the control into the workspace prefs and rebuild
            # it on the next launch by running uiScript. With retain=False it was thrown away on exit.
            widthProperty="preferred", width=w, height=h, retain=True,
            uiScript=_entry("show(restore=True)"),
            closeCommand=_entry("close()"),
        )
    else:
        instance.show(
            dockable=True, area="right", floating=True, width=w, height=h, retain=True,
            uiScript=_entry("show(restore=True)"),
        )

    instance.raise_()
    instance.setAttribute(qt.Qt.WA_DeleteOnClose, True)
    return instance


MENU_ITEM = "codeEditorMenuItem"        # fixed name, so a re-install replaces rather than piles up
WINDOWS_MENU = "MayaWindow|mainWindowMenu"


def _build_menu(menu:str) -> None:
    """Force a maya menu to fill itself in.

    Maya builds its main menus the first time they are opened, not at startup: querying the items of
    an unopened menu returns nothing, and anything inserted into it is wiped when it finally builds.
    Running its postMenuCommand is what makes it real.
    """
    import maya.cmds as cmds
    import maya.mel as mel
    try:
        command = cmds.menu(menu, query=True, postMenuCommand=True)
    except Exception:
        return
    if not command:
        return
    if callable(command):
        command()
    else:
        mel.eval(command)


def _find_submenu(menu:str, label:str) -> str:
    """The full path of the submenu of `menu` carrying `label`, or ''."""
    import maya.cmds as cmds
    for item in (cmds.menu(menu, query=True, itemArray=True) or []):
        path = "%s|%s" % (menu, item)
        try:
            if cmds.menuItem(path, query=True, subMenu=True) and \
                    cmds.menuItem(path, query=True, label=True) == label:
                return path
        except Exception:
            continue
    return ""


def install_menu(menu:str="General Editors", before:str="Script Editor",
                 label:str="Code Editor") -> str:
    """Add the editor to maya's Windows menu. Returns the item created, or ''.

    Args:
        menu:   (str): - submenu of Windows to put it in. None places it in Windows itself.
        before: (str): - label to sit ABOVE. None appends at the bottom instead.
        label:  (str): - what the entry reads.

    Safe to call twice: the item has a fixed name and an existing one is removed first. Call it from
    your startup (userSetup) for it to be there in every session - maya rebuilds its menus per
    launch, so nothing installed into them persists on its own.

    Returns:
        str: the menu item path, or '' if the menu could not be found.
    """
    import maya.cmds as cmds

    _build_menu(WINDOWS_MENU)
    parent = _find_submenu(WINDOWS_MENU, menu) if menu else WINDOWS_MENU
    if not parent:
        return ""
    if menu:
        _build_menu(parent)                  # the submenu builds lazily too

    if cmds.menuItem(MENU_ITEM, query=True, exists=True):
        cmds.deleteUI(MENU_ITEM, menuItem=True)

    # maya can only insert AFTER an item, so "above Script Editor" means "after whatever precedes it"
    after = ""
    if before:
        items = cmds.menu(parent, query=True, itemArray=True) or []
        for index, item in enumerate(items):
            path = "%s|%s" % (parent, item)
            try:
                if cmds.menuItem(path, query=True, label=True) == before:
                    after = "%s|%s" % (parent, items[index - 1]) if index else ""
                    break
            except Exception:
                continue

    options = {"parent": parent, "label": label,
               "command": _entry("show()")}
    if after:
        options["insertAfter"] = after
    return cmds.menuItem(MENU_ITEM, **options)


def uninstall_menu() -> None:
    """Remove the Windows menu entry, if it is there."""
    import maya.cmds as cmds
    if cmds.menuItem(MENU_ITEM, query=True, exists=True):
        cmds.deleteUI(MENU_ITEM, menuItem=True)


def close(**kwargs) -> None:
    """Close the standalone code editor and delete its workspace control.

    The PLACEMENT is kept: closing a panel says "not now", not "forget where I put it". Use reset()
    to actually discard it.

    Returns:
        None.
    """
    window_name = window.Editor.title + "WorkspaceControl"
    dock.delete_workspace_instances(window_name, keep_state=True)
    window.Editor.window_instance = None


def reset() -> None:
    """Forget where the panel was docked and reopen it floating, at the default size.

    The escape hatch for a placement that has gone wrong - dragged off-screen, or tabbed into a
    panel that no longer exists. Nothing else removes the stored state any more.

    Returns:
        None.
    """
    window_name = window.Editor.title + "WorkspaceControl"
    dock.delete_workspace_instances(window_name, keep_state=False)
    window.Editor.window_instance = None
    return show()
