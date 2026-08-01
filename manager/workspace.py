"""
CODE EDITOR.

Author: Gregoire Dehame
Created: Wed 11, 2024
Modified: Aug 01, 2026
Module: code_editor.manager.workspace
Execute: from code_editor.manager import workspace
"""

from ... import util
from .... import core as kcore

import os

__data__ = "data"


def get_workspace(workspace:str=None) -> str:
    """Return a correct workspace path based on a given workspace.

    Args:
        workspace: (str): - String path to query data from.

    Returns:
        str: The resolved workspace file path.
    """
    if workspace and os.path.basename(workspace) == "%s.json"% __data__:
        return workspace

    usersetup = util.usersetup()
    workspace = os.path.join(usersetup, "codes",  "%s.json"% __data__)
    kcore.json.write(os.path.join(usersetup, "codes",  "%s.json"% __data__), {"codes": {}})

    return workspace
