#!/usr/bin/env bash
set -euo pipefail

# Run SWC Target Cover timing experiments and collect a runtime table.
#
# Expected layout:
#   main_timed.py
#   controller_level_summary_*.csv
#
# Usage:
#   chmod +x run_runtime_experiments.sh
#   ./run_runtime_experiments.sh
#
# Optional overrides:
#   PYTHON_CMD=python3 MAIN=main_timed.py DATA_DIR=. OUT_DIR=runtime_results ./run_runtime_experiments.sh

PYTHON_CMD="${PYTHON_CMD:-python3}"
MAIN="${MAIN:-main_timed.py}"
DATA_DIR="${DATA_DIR:-.}"
OUT_DIR="${OUT_DIR:-runtime_results}"
SOLVER="${SOLVER:-z3}"

# Fixed budgets for the main horizon-scaling experiment.
BUDGETS="${BUDGETS:-1 2 3}"

# Extra comparison budget used for case-level and all-deadlines runs.
COMPARE_BUDGET="${COMPARE_BUDGET:-2}"

# Label-specific thresholds for label-level experiments.
LABEL_THRESHOLDS='dry:1in=22,5in=20,9in=18;medium:1in=24,5in=22,9in=20;wet:1in=26,5in=24,9in=22'

# Depth-only thresholds for case-level experiments, because main_timed.py does not allow
# --label-thresholds with --granularity case.
DEPTH_THRESHOLDS='1in:22,5in:20,9in:18'

mkdir -p "$OUT_DIR"

if [[ ! -f "$MAIN" ]]; then
  echo "ERROR: Cannot find $MAIN. Run this script from the directory containing main_timed.py, or set MAIN=/path/to/main_timed.py." >&2
  exit 1
fi

mapfile -t CSV_FILES < <(find "$DATA_DIR" -maxdepth 1 -type f -name 'controller_level_summary_*.csv' | sort)

if [[ ${#CSV_FILES[@]} -eq 0 ]]; then
  echo "ERROR: No controller_level_summary_*.csv files found in $DATA_DIR." >&2
  exit 1
fi

get_horizon() {
  "$PYTHON_CMD" - "$1" <<'PY'
import re
import sys
import pandas as pd
path = sys.argv[1]
cols = pd.read_csv(path, nrows=0).columns
T = max(int(m.group(1)) for c in cols if (m := re.match(r"^theta_.+?_t(\d+)$", c)))
print(T)
PY
}

run_fixed_label_max() {
  local csv="$1"
  local k="$2"
  local T
  T="$(get_horizon "$csv")"
  local base
  base="$(basename "$csv" .csv)"
  local prefix="$OUT_DIR/${base}_T${T}_label_deadmax_k${k}"

  echo "[RUN] T=$T label deadlines=max k=$k"
  "$PYTHON_CMD" "$MAIN" \
    --summary-csv "$csv" \
    --budget "$k" \
    --label-thresholds "$LABEL_THRESHOLDS" \
    --deadlines max \
    --granularity label \
    --coverage-mode all \
    --deadline-mode at_or_before \
    --solver "$SOLVER" \
    --out-prefix "$prefix" \
    > "${prefix}.stdout.txt"
}

run_fixed_case_max() {
  local csv="$1"
  local k="$2"
  local T
  T="$(get_horizon "$csv")"
  local base
  base="$(basename "$csv" .csv)"
  local prefix="$OUT_DIR/${base}_T${T}_case_deadmax_k${k}"

  echo "[RUN] T=$T case deadlines=max k=$k"
  "$PYTHON_CMD" "$MAIN" \
    --summary-csv "$csv" \
    --budget "$k" \
    --thresholds "$DEPTH_THRESHOLDS" \
    --deadlines max \
    --granularity case \
    --coverage-mode all \
    --deadline-mode at_or_before \
    --solver "$SOLVER" \
    --out-prefix "$prefix" \
    > "${prefix}.stdout.txt"
}

run_fixed_label_all_deadlines() {
  local csv="$1"
  local k="$2"
  local T
  T="$(get_horizon "$csv")"
  local base
  base="$(basename "$csv" .csv)"
  local prefix="$OUT_DIR/${base}_T${T}_label_deadall_k${k}"

  echo "[RUN] T=$T label deadlines=all k=$k"
  "$PYTHON_CMD" "$MAIN" \
    --summary-csv "$csv" \
    --budget "$k" \
    --label-thresholds "$LABEL_THRESHOLDS" \
    --deadlines all \
    --granularity label \
    --coverage-mode all \
    --deadline-mode at_or_before \
    --solver "$SOLVER" \
    --out-prefix "$prefix" \
    > "${prefix}.stdout.txt"
}

run_min_budget_label_max() {
  local csv="$1"
  local T
  T="$(get_horizon "$csv")"
  local base
  base="$(basename "$csv" .csv)"
  local prefix="$OUT_DIR/${base}_T${T}_label_deadmax_minbudget"

  echo "[RUN] T=$T label deadlines=max find-min-budget"
  "$PYTHON_CMD" "$MAIN" \
    --summary-csv "$csv" \
    --find-min-budget \
    --label-thresholds "$LABEL_THRESHOLDS" \
    --deadlines max \
    --granularity label \
    --coverage-mode all \
    --deadline-mode at_or_before \
    --solver "$SOLVER" \
    --out-prefix "$prefix" \
    > "${prefix}.stdout.txt"
}

# ============================================================
# Experiments
# ============================================================

echo "Found ${#CSV_FILES[@]} CSV file(s):"
printf '  %s\n' "${CSV_FILES[@]}"
echo

# 1. Main horizon scaling: label-level, one final deadline, several fixed k values.
for csv in "${CSV_FILES[@]}"; do
  for k in $BUDGETS; do
    run_fixed_label_max "$csv" "$k"
  done
done

# 2. Minimum budget search: useful for reporting the smallest feasible k.
for csv in "${CSV_FILES[@]}"; do
  run_min_budget_label_max "$csv"
done

# 3. Case-level comparison: shows effect of q on m and preprocessing cost.
for csv in "${CSV_FILES[@]}"; do
  run_fixed_case_max "$csv" "$COMPARE_BUDGET"
done

# 4. All-deadlines comparison: shows effect of h on m.
for csv in "${CSV_FILES[@]}"; do
  run_fixed_label_all_deadlines "$csv" "$COMPARE_BUDGET"
done

# ============================================================
# Collect result JSON files into CSV and Markdown tables.
# ============================================================

OUT_DIR="$OUT_DIR" "$PYTHON_CMD" - <<'PY'
import csv
import glob
import json
import os
import re
from pathlib import Path

out_dir = Path(os.environ["OUT_DIR"])
result_paths = sorted(out_dir.glob("*.result.json"))

rows = []
for path in result_paths:
    with open(path, "r", encoding="utf-8") as f:
        r = json.load(f)

    stats = r.get("instance_statistics", {})
    timing = r.get("timing_seconds", {})
    settings = r.get("settings", {})

    name = path.stem.replace(".result", "")

    # Infer experiment label from file name.
    if "_minbudget" in name:
        experiment = "min-budget"
    elif "_case_" in name:
        experiment = "case-level"
    elif "_deadall_" in name:
        experiment = "all-deadlines"
    else:
        experiment = "horizon-scaling"

    rows.append({
        "experiment": experiment,
        "file": name,
        "summary_csv": Path(settings.get("summary_csv", "")).name,
        "T": stats.get("time_horizon", ""),
        "n_schedules": stats.get("num_schedules", ""),
        "csv_rows": stats.get("num_csv_rows", ""),
        "granularity": settings.get("granularity", ""),
        "deadlines": settings.get("deadlines", ""),
        "m_requirements": stats.get("num_requirements", ""),
        "L_coverage_incidences": stats.get("num_coverage_incidences", ""),
        "coverage_density": stats.get("coverage_density", ""),
        "budget": r.get("budget", ""),
        "minimum_budget": r.get("minimum_budget", ""),
        "budget_used": r.get("budget_used", ""),
        "solver_calls": r.get("num_solver_calls", ""),
        "satisfiable": r.get("satisfiable", ""),
        "preprocess_time_sec": timing.get("preprocess_time", ""),
        "solve_time_sec": timing.get("solve_time", ""),
        "total_time_sec": timing.get("total_time", ""),
    })

# Sort by T, experiment, deadlines, granularity, budget.
def sort_key(row):
    def as_int(x, default=10**9):
        try:
            return int(x)
        except Exception:
            return default
    return (
        as_int(row["T"]),
        row["experiment"],
        row["granularity"],
        str(row["deadlines"]),
        as_int(row["budget"]),
    )

rows.sort(key=sort_key)

csv_path = out_dir / "runtime_summary.csv"
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
    "budget",
    "minimum_budget",
    "budget_used",
    "solver_calls",
    "satisfiable",
    "preprocess_time_sec",
    "solve_time_sec",
    "total_time_sec",
    "summary_csv",
    "file",
]
with open(csv_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

# Markdown table with rounded numeric fields.
def fmt(x):
    if x is None:
        return ""
    if isinstance(x, bool):
        return str(x)
    try:
        val = float(x)
        if abs(val) < 1e-4 and val != 0:
            return f"{val:.2e}"
        return f"{val:.4f}"
    except Exception:
        return str(x)

md_cols = [
    "experiment",
    "T",
    "n_schedules",
    "granularity",
    "deadlines",
    "m_requirements",
    "budget",
    "minimum_budget",
    "satisfiable",
    "preprocess_time_sec",
    "solve_time_sec",
    "total_time_sec",
]
md_path = out_dir / "runtime_summary.md"
with open(md_path, "w", encoding="utf-8") as f:
    f.write("| " + " | ".join(md_cols) + " |\n")
    f.write("|" + "|".join(["---"] * len(md_cols)) + "|\n")
    for row in rows:
        f.write("| " + " | ".join(fmt(row.get(c, "")) for c in md_cols) + " |\n")

print()
print(f"Wrote summary CSV: {csv_path}")
print(f"Wrote Markdown table: {md_path}")
PY

echo
echo "Done. Open:"
echo "  $OUT_DIR/runtime_summary.csv"
echo "  $OUT_DIR/runtime_summary.md"
