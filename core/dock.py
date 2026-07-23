"""
KATA. (c)

Author: Gregoire Dehame
Created: Jul 21, 2026
Module: ui.code_editor.core.dock
Execute: from kata.ui.code_editor.core import dock

Self-contained Maya workspaceControl helpers so `code/` needs nothing from kata's ui.util. Provides only
what the standalone entry point uses: screen size, dockable-panel map, and the create / delete / restore
workspace-control lifecycle.
"""

from . import qt

# tab-index hint per Maya panel when tabbing the control into it (mirrors kata.ui.__dockable__)
__dockable__ = {
    "AttributeEditor": -1,
    "ChannelBoxLayerEditor": -1,
    "Outliner": 0,
    "Shelf": -1,
    "TimeSlider": -1,
    "RangeSlider": -1,
    "CommandLine": -1,
    "HelpLine": -1,
    "ToolBox": -1,
    "ToolSettings": 0,
    "polyTexturePlacementPanel1Window": 0,
    "UVToolkitDockControl": 0,
}


def screen_size() -> list:
    """Return the primary screen resolution as [width, height] (defaults to [1920, 1080])."""
    try:
        app = qt.QApplication.instance() or qt.QApplication([])
        screen = app.primaryScreen().availableGeometry()
        return [screen.width(), screen.height()]
    except Exception:
        return [1920, 1080]


def workspace_exists(workspace:str=None) -> bool:
    """Return True if the given workspaceControl exists in the current Maya session."""
    if not workspace:
        return False
    import maya.cmds as cmds
    return bool(cmds.workspaceControl(workspace, exists=True))


def delete_workspace_control(name:str=None, keep_state:bool=True) -> None:
    """Delete the window and the workspaceControl for `name`.

    `keep_state` leaves the workspaceControlState alone, and that is the DEFAULT for a reason: that
    state is where maya records whether the panel was docked, against which edge, and how big. It
    survives a restart - but only if nothing removes it. Deleting it on the way to re-opening, as
    this used to, quietly reset the panel to floating every single time.

    Pass False only to deliberately forget the placement.
    """
    if not name:
        return
    import maya.cmds as cmds
    if cmds.window(name, query=True, exists=True):
        cmds.deleteUI(name)
    if cmds.workspaceControl(name, query=True, exists=True):
        cmds.deleteUI(name, control=True)
    if not keep_state and cmds.workspaceControlState(name, query=True, exists=True):
        cmds.workspaceControlState(name, remove=True)


def delete_workspace_instances(name:str=None, delete_control:bool=True,
                               keep_state:bool=True) -> None:
    """Close the Qt widget and (optionally) delete the workspaceControl for the given name."""
    if not name:
        return
    import maya.OpenMayaUI as OpenMayaUI
    control = OpenMayaUI.MQtUtil.findControl(name)
    if control:
        widget = qt.wrap_instance(control, qt.QWidget)
        if widget:
            widget.close()
    if delete_control:
        delete_workspace_control(name=name, keep_state=keep_state)
    if name.count("WorkspaceControl"):
        delete_workspace_instances(name=name.replace("WorkspaceControl", ""),
                                   delete_control=delete_control, keep_state=keep_state)


def restore_workspace_control(name:str=None) -> None:
    """Restore a workspaceControl widget into the current Maya layout (uiScript restore path)."""
    if not name:
        return
    import maya.OpenMayaUI as OpenMayaUI
    workspace_control = OpenMayaUI.MQtUtil.getCurrentParent()
    pointer = OpenMayaUI.MQtUtil.findControl(name)
    if pointer:
        OpenMayaUI.MQtUtil.addWidgetToMayaLayout(int(pointer), int(workspace_control))
