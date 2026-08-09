"""The ONE data anchor for the vendored shared library.

Every data file the planner reads lives under the logistics root (data/,
depot_data/, fleet_replay_exports/, .cache/) — NOT inside freight_planner/.
All shared modules derive their paths from LOGISTICS_ROOT so the package can
move within the tree without silently losing its data (the partb6 lesson:
a __file__-relative anchor in a relocated copy read no vehicle list and
halved the seed with no error).
"""
from pathlib import Path

# shared/paths.py -> [0]=shared -> [1]=freight_planner -> [2]=logistics root
LOGISTICS_ROOT = Path(__file__).resolve().parents[2]
