"""Offline data-repair tools vendored into the freight planner (2026-07-13
codebase separation): verified-leg truth regeneration (verify_legs), the
telematics replay export it depends on (export_replay), and the vehicle-master
build (build_vehicle_master). Run as modules, e.g.::

    python -m freight_planner.tools.verify_legs
    python -m freight_planner.tools.build_vehicle_master

Output artifacts live in freight_planner/data/ — vehicle_master.csv (historical home)
and verified_legs.csv + mot_results.csv (rehomed there 2026-07-13 when planning_agent/
was archived; shared.verified_legs and paths.DEFAULT_VERIFIED_LEGS read the new home).
"""
