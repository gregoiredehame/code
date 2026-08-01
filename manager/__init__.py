"""
CODE EDITOR.

Author: Gregoire Dehame
Created: Jul 21, 2026
Modified: Aug 01, 2026
Module: code_editor.manager
Execute: from code_editor import manager

The host-embedded code editor: a panel driven by the krig workspace (its codes/data.json and temp
files), tied to the host application. Built on top of core. Modules:

    panel     - Widget: the embedded panel (script tabs over the output console) the host docks.
    tabs      - TabWidget / CodeWidget: workspace-driven tabs (open_temp / reload_codes / save_code).
    workspace - workspace helpers (data.json path resolution) for the embedded panel.
"""
