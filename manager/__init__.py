"""
KATA. (c)

Author: Gregoire Dehame
Created: Jul 21, 2026
Module: ui.code_editor.manager
Execute: from kata.ui.code_editor import manager

The kata_manager-embedded code editor: a panel driven by the krig workspace (its codes/data.json and temp
files), tied to kata (kcore/qt). Built on top of code.core. Modules:

    panel     - Widget: the embedded panel (script tabs over the output console) kata_manager docks.
    tabs      - TabWidget / CodeWidget: workspace-driven tabs (open_temp / reload_codes / save_code).
    workspace - workspace helpers (data.json path resolution) for the embedded panel.
"""
