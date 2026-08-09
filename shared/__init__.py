"""Shared network-model library vendored into the freight planner (2026-07-13
codebase separation, spec docs/superpowers/specs/2026-07-13-freight-planner-separation-design.md).

Verbatim copies of the load-bearing legacy modules — cambridge/{config,scope,
plan_types,verified_legs}, simulation/{postcode_resolver,routing},
operational_analysis/fleet_replay_data — with only two kinds of edit: imports
now resolve inside this package, and every data-file path derives from
``paths.LOGISTICS_ROOT`` (a copied module one level deeper than its data
silently reads nothing — that failure crippled run partb6).
"""
