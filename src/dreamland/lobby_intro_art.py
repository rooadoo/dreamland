"""Fixed ASCII artwork for the connection introduction."""

from __future__ import annotations

# Seven-row block letters sit between network rails. Trailing spaces keep every
# row the same width so the reveal can slice columns without shifting the art.
INTRO_ART: tuple[str, ...] = (
    "o---o-----o-----o-----o-----o-----o-----o-----o-----o---o",
    "  ####  ####  #####  ###  #   # #      ###  #   # ####   ",
    "  #   # #   # #     #   # ## ## #     #   # ##  # #   #  ",
    "  #   # #   # #     #   # # # # #     #   # ##  # #   #  ",
    "  #   # ####  ####  ##### # # # #     ##### # # # #   #  ",
    "  #   # # #   #     #   # #   # #     #   # #  ## #   #  ",
    "  #   # #  #  #     #   # #   # #     #   # #  ## #   #  ",
    "  ####  #   # ##### #   # #   # ##### #   # #   # ####   ",
    "o---o-----o-----o-----o-----o-----o-----o-----o-----o---o",
)
INTRO_ART_WIDTH = len(INTRO_ART[0])
INTRO_ART_HEIGHT = len(INTRO_ART)
