#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Runtime experiments for SWC Target Cover.
# Label-granularity only.
#
# This script does three things:
#   1. Finds a threshold setting that is high enough to be nontrivial
#      but still feasible on all T=4,6,8,10 files.
#   2. Runs fixed-budget horizon-scaling experiments for k=1,2,3.
#   3. Runs minimum-budget experiments and collects all result JSONs
#      into CSV/Markdown summary tables.
#
# Expected files in the current directory:
#   main_timed.py
#   controller_level_summary_T_4.csv
#   controller_level_summary_T_6.csv
#   controller_level_summary_T_8.csv
#   controller_level_summary_T_10.csv
#
# Usage:
#   chmod +x run_runtime_experiments_moderate_label.sh
#   ./run_runtime_experiments_moderate_label.sh
#
# Optional overrides:
#   PYTHON_CMD=python ./run_runtime_experiments_moderate_label.sh
#   DATA_DIR=/path/to/csvs ./run_runtime_experiments_moderate_label.sh
#   MAIN=/path/to/main_timed.py ./run_runtime_experiments_moderate_label.sh
# ============================================================

PYTHON_CMD="${PYTHON_CMD:-python3}"
MAIN="${MAIN:-main_timed.py}"
DATA_DIR="${DATA_DIR:-.}"
OUT_DIR="${OUT_DIR:-runtime_results_moderate_label}"
SOLVER="${SOLVER:-z3}"

# Fixed-budget values for the horizon-scaling runs.
BUDGETS="${BUDGETS:-1 2 3}"

mkdir -p "$OUT_DIR"
mkdir -p "$OUT_DIR/calibration"

if [[ ! -f "$MAIN" ]]; then
  echo "ERROR: Cannot find $MAIN"
  echo "Run this script from the directory containing main_timed.py, or set MAIN=/path/to/main_timed.py"
  exit 1
fi

# Resolve CSV files. The first filename in each list is the preferred local name.
# The second/third options are fallbacks for the uploaded timestamped files.
find_csv_for_T() {
  local T="$1"
  shift
  local candidates=("$@")
  for name in "${candidates[@]}"; do
    if [[ -f "$DATA_DIR/$name" ]]; then
      echo "$DATA_DIR/$name"
      return 0
    fi
  done
  echo "ERROR: Could not find CSV for T=$T in DATA_DIR=$DATA_DIR" >&2
  echo "Looked for: ${candidates[*]}" >&2
  return 1
}

CSV_T4=$(find_csv_for_T 4 "controller_level_summary_T_4.csv")
CSV_T6=$(find_csv_for_T 6 "controller_level_summary_T_6.csv" "controller_level_summary_20260609_145541.csv")
CSV_T8=$(find_csv_for_T 8 "controller_level_summary_T_8.csv" "controller_level_summary_20260609_145940.csv")
CSV_T10=$(find_csv_for_T 10 "controller_level_summary_T_10.csv" "controller_level_summary_20260609_144814.csv")

CSV_FILES=("$CSV_T4" "$CSV_T6" "$CSV_T8" "$CSV_T10")

# Threshold candidates are ordered from stricter to easier.
# The calibration step chooses the first candidate that is feasible for all horizons.
# These are depth-specific thresholds, applied uniformly to dry/medium/wet labels.
THRESHOLD_CANDIDATES=(
  "1in:24.8,5in:24.6,9in:24.4"
  "1in:24.6,5in:24.4,9in:24.2"
  "1in:24.4,5in:24.2,9in:24.0"
  "1in:24.2,5in:24.0,9in:23.8"
  "1in:24.0,5in:23.8,9in:23.6"
  "1in:23.5,5in:23.3,9in:23.1"
  "1in:23.0,5in:22.8,9in:22.6"
  "1in:22.0,5in:21.8,9in:21.6"
  "1in:21.0,5in:20.8,9in:20.6"
  "1in:20.0,5in:19.8,9in:19.6"
)

# Extract horizon from CSV header by finding the largest theta_*_t# column.
get_horizon() {
  "$PYTHON_CMD" - "$1" <<'PY'
import csv, re, sys
path = sys.argv[1]
with open(path, newline='') as f:
    reader = csv.reader(f)
    header = next(reader)
T = -1
for col in header:
    m = re.match(r"theta_.+_t(\d+)$", col)
    if m:
        T = max(T, int(m.group(1)))
if T < 0:
    raise SystemExit(f"No theta_*_t# columns found in {path}")
print(T)
PY
}

# Check satisfiability of one threshold candidate for one CSV/deadline mode.
# Uses a very large budget so this tests whether the requirements are coverable at all.
calibration_run_is_sat() {
  local csv="$1"
  local threshold="$2"
  local deadlines="$3"
  local tag="$4"
  local T
  T=$(get_horizon "$csv")
  local prefix="$OUT_DIR/calibration/${tag}_T${T}_candidate"

  "$PYTHON_CMD" "$MAIN" \
    --summary-csv "$csv" \
    --budget 1000000 \
    --thresholds "$threshold" \
    --deadlines "$deadlines" \
    --granularity label \
    --coverage-mode all \
    --deadline-mode at_or_before \
    --solver "$SOLVER" \
    --out-prefix "$prefix" \
    > "${prefix}.stdout.txt"

  "$PYTHON_CMD" - "${prefix}.result.json" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
print("SAT" if data.get("satisfiable") else "UNSAT")
PY
}

# Choose the first threshold candidate that is SAT for all CSV files.
choose_threshold_for_deadlines() {
  local deadlines="$1"
  local tag="$2"

  for threshold in "${THRESHOLD_CANDIDATES[@]}"; do
    echo "Calibrating ${tag}: trying threshold: $threshold" >&2
    local all_sat=1
    for csv in "${CSV_FILES[@]}"; do
      result=$(calibration_run_is_sat "$csv" "$threshold" "$deadlines" "$tag")
      echo "  $(basename "$csv"): $result" >&2
      if [[ "$result" != "SAT" ]]; then
        all_sat=0
        break
      fi
    done

    if [[ "$all_sat" -eq 1 ]]; then
      echo "$threshold"
      return 0
    fi
  done

  echo "ERROR: None of the threshold candidates were feasible for deadlines=$deadlines" >&2
  return 1
}

# Run fixed-budget label-granularity experiment with final deadline only.
run_fixed_label_max() {
  local csv="$1"
  local k="$2"
  local threshold="$3"
  local T
  T=$(get_horizon "$csv")
  local base
  base=$(basename "$csv" .csv)
  local prefix="$OUT_DIR/${base}_T${T}_label_deadmax_k${k}"

  echo "Running horizon-scaling: T=$T, k=$k"

  "$PYTHON_CMD" "$MAIN" \
    --summary-csv "$csv" \
    --budget "$k" \
    --thresholds "$threshold" \
    --deadlines max \
    --granularity label \
    --coverage-mode all \
    --deadline-mode at_or_before \
    --solver "$SOLVER" \
    --out-prefix "$prefix" \
    > "${prefix}.stdout.txt"
}

# Run minimum-budget label-granularity experiment with final deadline only.
run_min_budget_label_max() {
  local csv="$1"
  local threshold="$2"
  local T
  T=$(get_horizon "$csv")
  local base
  base=$(basename "$csv" .csv)
  local prefix="$OUT_DIR/${base}_T${T}_label_deadmax_minbudget"

  echo "Running min-budget: T=$T, deadline=max"

  "$PYTHON_CMD" "$MAIN" \
    --summary-csv "$csv" \
    --find-min-budget \
    --thresholds "$threshold" \
    --deadlines max \
    --granularity label \
    --coverage-mode all \
    --deadline-mode at_or_before \
    --solver "$SOLVER" \
    --out-prefix "$prefix" \
    > "${prefix}.stdout.txt"
}

# Run minimum-budget label-granularity experiment with all deadlines.
# This uses a separately calibrated threshold because requiring all deadlines is stricter.
run_min_budget_label_all_deadlines() {
  local csv="$1"
  local threshold="$2"
  local T
  T=$(get_horizon "$csv")
  local base
  base=$(basename "$csv" .csv)
  local prefix="$OUT_DIR/${base}_T${T}_label_deadall_minbudget"

  echo "Running min-budget: T=$T, deadlines=all"

  "$PYTHON_CMD" "$MAIN" \
    --summary-csv "$csv" \
    --find-min-budget \
    --thresholds "$threshold" \
    --deadlines all \
    --granularity label \
    --coverage-mode all \
    --deadline-mode at_or_before \
    --solver "$SOLVER" \
    --out-prefix "$prefix" \
    > "${prefix}.stdout.txt"
}

# ------------------------------------------------------------
# 1. Calibrate thresholds.
# ------------------------------------------------------------

MAX_THRESHOLD=$(choose_threshold_for_deadlines "max" "deadmax")
ALL_THRESHOLD=$(choose_threshold_for_deadlines "all" "deadall")

echo
printf "Chosen threshold for deadline=max: %s\n" "$MAX_THRESHOLD"
printf "Chosen threshold for deadlines=all: %s\n" "$ALL_THRESHOLD"
printf "deadline_max_threshold,%s\n" "$MAX_THRESHOLD" > "$OUT_DIR/chosen_thresholds.csv"
printf "deadlines_all_threshold,%s\n" "$ALL_THRESHOLD" >> "$OUT_DIR/chosen_thresholds.csv"

# ------------------------------------------------------------
# 2. Fixed-budget horizon scaling, deadline=max.
# ------------------------------------------------------------

for csv in "${CSV_FILES[@]}"; do
  for k in $BUDGETS; do
    run_fixed_label_max "$csv" "$k" "$MAX_THRESHOLD"
  done
done

# ------------------------------------------------------------
# 3. Minimum budget, deadline=max.
# ------------------------------------------------------------

for csv in "${CSV_FILES[@]}"; do
  run_min_budget_label_max "$csv" "$MAX_THRESHOLD"
done

# ------------------------------------------------------------
# 4. Minimum budget, deadlines=all.
# ------------------------------------------------------------

for csv in "${CSV_FILES[@]}"; do
  run_min_budget_label_all_deadlines "$csv" "$ALL_THRESHOLD"
done

# ------------------------------------------------------------
# 5. Collect all non-calibration result JSONs into summary tables.
# ------------------------------------------------------------

OUT_DIR="$OUT_DIR" "$PYTHON_CMD" - <<'PY'
import csv
import glob
import json
import os
from pathlib import Path

out_dir = Path(os.environ["OUT_DIR"])
result_files = sorted(
    p for p in out_dir.glob("*.result.json")
    if "calibration" not in str(p)
)

rows = []
for path in result_files:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    stats = data.get("instance_statistics", {})
    timing = data.get("timing_seconds", {})
    settings = data.get("settings", {})
    name = path.name

    if "_deadall_" in name:
        experiment = "all-deadlines-minbudget"
    elif "_minbudget" in name:
        experiment = "max-deadline-minbudget"
    else:
        experiment = "horizon-scaling-fixed-k"

    rows.append({
        "experiment": experiment,
        "T": stats.get("time_horizon"),
        "n_schedules": stats.get("num_schedules"),
        "csv_rows": stats.get("num_csv_rows"),
        "granularity": settings.get("granularity"),
        "deadlines": settings.get("deadlines"),
        "m_requirements": stats.get("num_requirements"),
        "L_coverage_incidences": stats.get("num_coverage_incidences"),
        "coverage_density": stats.get("coverage_density"),
        "thresholds": settings.get("thresholds"),
        "budget": data.get("budget"),
        "minimum_budget": data.get("minimum_budget"),
        "budget_used": data.get("budget_used"),
        "solver_calls": data.get("num_solver_calls"),
        "satisfiable": data.get("satisfiable"),
        "preprocess_time_sec": timing.get("preprocess_time"),
        "solve_time_sec": timing.get("solve_time"),
        "total_time_sec": timing.get("total_time"),
        "result_file": str(path),
    })

# Sort in a useful order.
order = {
    "horizon-scaling-fixed-k": 0,
    "max-deadline-minbudget": 1,
    "all-deadlines-minbudget": 2,
}
rows.sort(key=lambda r: (order.get(r["experiment"], 99), int(r["T"] or 0), str(r.get("budget"))))

summary_csv = out_dir / "runtime_summary_moderate_label.csv"
summary_md = out_dir / "runtime_summary_moderate_label.md"

fieldnames = [
    "experiment",
    "T",
    "n_schedules",
    "csv_rows",
    "granularity",
    "deadlines",
    "m_requirements",
    "L_coverage_incidences",
    "coverage_density",
    "thresholds",
    "budget",
    "minimum_budget",
    "budget_used",
    "solver_calls",
    "satisfiable",
    "preprocess_time_sec",
    "solve_time_sec",
    "total_time_sec",
    "result_file",
]

with open(summary_csv, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

# Markdown table: shorter set of columns for the report.
md_cols = [
    "experiment",
    "T",
    "n_schedules",
    "deadlines",
    "m_requirements",
    "budget",
    "minimum_budget",
    "satisfiable",
    "preprocess_time_sec",
    "solve_time_sec",
    "total_time_sec",
]

def fmt(x):
    if x is None:
        return ""
    if isinstance(x, float):
        return f"{x:.6f}"
    return str(x)

with open(summary_md, "w", encoding="utf-8") as f:
    f.write("| " + " | ".join(md_cols) + " |\n")
    f.write("| " + " | ".join(["---"] * len(md_cols)) + " |\n")
    for r in rows:
        f.write("| " + " | ".join(fmt(r.get(c)) for c in md_cols) + " |\n")

print()
print(f"Wrote summary CSV: {summary_csv}")
print(f"Wrote summary Markdown table: {summary_md}")
print(f"Wrote chosen thresholds: {out_dir / 'chosen_thresholds.csv'}")
PY

echo
echo "Done. Look at:"
echo "  $OUT_DIR/chosen_thresholds.csv"
echo "  $OUT_DIR/runtime_summary_moderate_label.csv"
echo "  $OUT_DIR/runtime_summary_moderate_label.md"
