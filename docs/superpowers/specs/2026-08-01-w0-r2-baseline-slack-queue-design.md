# W0 R2 baseline and slack queue

## Objective

Run the four remaining W0 R2 baseline seeds followed by all fifteen travel-speed slack runs, sequentially in the existing isolated W0 lane, without interfering with the active C0 process.

## Run order

1. `W0_R2_baseline_s1` through `W0_R2_baseline_s4`.
2. `W0_R2_slack_speed_minus_10pct_s0` through `s4`.
3. `W0_R2_slack_speed_minus_20pct_s0` through `s4`.
4. `W0_R2_slack_speed_minus_30pct_s0` through `s4`.

`W0_baseline_v2` is the completed seed-0 baseline comparator and is not rerun.

## Execution

- Commands and output directories come from `freight_planner/result_runs/manifest.json`.
- Exactly one W0 solver runs at a time.
- The controller and its child solvers run below normal process priority.
- Every run uses the existing W0-only OSRM and postcode cache files under `_overnight_isolation/W0`.
- The controller refuses to overwrite an existing output directory or run log.
- C0 is neither waited on nor modified.

## Failure handling

After each run, the controller requires:

- process exit code zero;
- an empty stderr log;
- the dynamic completion marker in `alns_progress.log`;
- zero hard-feasibility violations;
- zero non-anticipativity violations;
- zero commitment/backdating violations;
- zero option conflicts.

The queue stops on the first failed check. Later runs remain unstarted so a defect cannot contaminate the campaign.

## Outputs

- Each run writes to its manifest-defined `W0_R2/.../seed_n` directory.
- Each run receives distinct stdout, stderr and audit files at the `result_runs` root.
- A queue log records start time, completion time, exit code and audit outcome for every run.

