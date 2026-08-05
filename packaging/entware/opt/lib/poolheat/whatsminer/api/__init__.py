"""Public Whatsminer API clients (V1 / V2 / V3) + command catalog."""

from __future__ import annotations

from .catalog import (
    ApiCommand,
    all_commands,
    commands_by_family,
    summary_table,
    summary_table as api_summary_table,
    V1_METHOD_MAP,
    V2_METHOD_MAP,
    V3_METHOD_MAP,
)
from .v1 import V1_ONLY_CMDS, V1_READ_CMDS, V1_WRITE_CMDS, WhatsminerV1
from .v2 import V2_READ_CMDS, V2_WRITE_CMDS, WhatsminerV2
from .v3 import V3_ENCRYPTED_PARAMS, V3_GET_CMDS, V3_SET_CMDS, WhatsminerV3

__all__ = [
    "WhatsminerV1",
    "WhatsminerV2",
    "WhatsminerV3",
    "V1_READ_CMDS",
    "V1_WRITE_CMDS",
    "V1_ONLY_CMDS",
    "V2_READ_CMDS",
    "V2_WRITE_CMDS",
    "V3_GET_CMDS",
    "V3_SET_CMDS",
    "V3_ENCRYPTED_PARAMS",
    "ApiCommand",
    "all_commands",
    "commands_by_family",
    "summary_table",
    "api_summary_table",
    "V1_METHOD_MAP",
    "V2_METHOD_MAP",
    "V3_METHOD_MAP",
]
