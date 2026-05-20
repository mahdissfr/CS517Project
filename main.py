#!/usr/bin/env python3
"""
SWC Target Cover using PySMT.

This version uses the SUMMARY file, where each row represents one
(initial_case_id, schedule_id) trajectory, and theta values are stored
in columns such as:

    theta_1in_t0, theta_1in_t1, ..., theta_1in_t4
    theta_5in_t0, theta_5in_t1, ..., theta_5in_t4
    theta_9in_t0, theta_9in_t1, ..., theta_9in_t4

The problem is modeled as Set Cover:

    Universe elements:
        SWC target requirements.

    Sets:
        Candidate irrigation schedules.

    Goal:
        Select at most k schedules so that every SWC target requirement
        is covered.

Install:

    pip install pandas pysmt
    pysmt-install --z3

Basic depth-threshold run:

    python main.py `
        --summary-csv controller_level_summary_20260518_211049.csv `
        --budget 2 `
        --thresholds 1in:22,5in:20,9in:18 `
        --deadlines 4 `
        --granularity label `
        --coverage-mode all `
        --deadline-mode at_or_before `
        --solver z3 `
        --out-prefix swc_depth_thresholds

Label-specific threshold run:

    python main.py `
  --summary-csv controller_level_summary_20260518_211049.csv `
  --budget 2 `
  --label-thresholds "dry:1in=24.8,5in=24.6,9in=24.4;medium:1in=24.9,5in=24.7,9in=24.5;wet:1in=24.95,5in=24.8,9in=24.6" `
  --deadlines 4 `
  --granularity label `
  --coverage-mode all `
  --deadline-mode at_or_before `
  --solver z3 `
  --out-prefix swc_label_thresholds

Find minimum budget:

    python main.py `
  --summary-csv controller_level_summary_20260518_211049.csv `
  --find-min-budget `
  --label-thresholds "dry:1in=24.8,5in=24.6,9in=24.4;medium:1in=24.9,5in=24.7,9in=24.5;wet:1in=24.95,5in=24.8,9in=24.6" `
  --deadlines 4 `
  --granularity label `
  --coverage-mode all `
  --deadline-mode at_or_before `
  --solver z3 `
  --out-prefix swc_min_budget
"""

import argparse
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional

import pandas as pd

from pysmt.shortcuts import (
    Symbol,
    And,
    Or,
    Bool,
    Int,
    Plus,
    Ite,
    LE,
    is_sat,
    get_model,
)
from pysmt.typing import BOOL


# ============================================================
# Data structures
# ============================================================

@dataclass(frozen=True)
class Requirement:
    req_id: str
    initial_group: str
    depth: str
    deadline: int
    threshold: float
    description: str


@dataclass(frozen=True)
class Schedule:
    schedule_id: str
    binary_sequence: str


# ============================================================
# Parsing theta columns
# ============================================================

def parse_theta_column(col: str) -> Optional[Tuple[str, int]]:
    """
    Parse summary-file theta columns.

    Example:
        theta_1in_t4 -> ("1in", 4)
        theta_5in_t2 -> ("5in", 2)
        theta_9in_t0 -> ("9in", 0)
    """
    match = re.match(r"^theta_(.+?)_t(\d+)$", col)
    if match is None:
        return None

    depth = match.group(1)
    time_step = int(match.group(2))
    return depth, time_step


def find_theta_columns(df: pd.DataFrame) -> Dict[Tuple[str, int], str]:
    """
    Find all theta columns in the summary file.

    Returns:
        {
            ("1in", 0): "theta_1in_t0",
            ("1in", 1): "theta_1in_t1",
            ...
        }
    """
    theta_cols = {}

    for col in df.columns:
        parsed = parse_theta_column(col)
        if parsed is not None:
            theta_cols[parsed] = col

    return theta_cols


def depth_sort_key(depth: str) -> int:
    """
    Sort depths like 1in, 5in, 9in, 11in numerically.
    """
    nums = re.findall(r"\d+", depth)
    if nums:
        return int(nums[0])
    return 10**9


def get_depths_and_times(theta_cols: Dict[Tuple[str, int], str]) -> Tuple[List[str], List[int]]:
    depths = sorted({depth for depth, _ in theta_cols.keys()}, key=depth_sort_key)
    times = sorted({time_step for _, time_step in theta_cols.keys()})
    return depths, times


# ============================================================
# Threshold parsing
# ============================================================

def parse_thresholds(threshold_arg: str, depths: List[str]) -> Dict[str, float]:
    """
    Parse depth-only thresholds.

    Supported formats:

    1. same:20
       Every depth has threshold 20.

    2. 1in:22,5in:20,9in:18
       Depth-specific thresholds.

    These thresholds apply to every initial label/group.
    """
    threshold_arg = threshold_arg.strip()

    if threshold_arg.startswith("same:"):
        value = float(threshold_arg.split(":", 1)[1])
        return {depth: value for depth in depths}

    thresholds = {}

    parts = [p.strip() for p in threshold_arg.split(",") if p.strip()]
    for part in parts:
        if ":" not in part:
            raise ValueError(
                f"Invalid threshold format: {part}. "
                f"Use same:20 or 1in:22,5in:20,9in:18."
            )

        depth, value = part.split(":", 1)
        thresholds[depth.strip()] = float(value)

    missing = set(depths) - set(thresholds.keys())
    if missing:
        raise ValueError(
            f"Missing threshold values for depths {sorted(missing)}. "
            f"Available depths are {depths}."
        )

    extra = set(thresholds.keys()) - set(depths)
    if extra:
        raise ValueError(
            f"Thresholds provided for unknown depths {sorted(extra)}. "
            f"Available depths are {depths}."
        )

    return thresholds


def parse_label_thresholds(
    label_threshold_arg: str,
    groups: List[str],
    depths: List[str],
) -> Dict[str, Dict[str, float]]:
    """
    Parse label-specific thresholds.

    Example:

        dry:1in=22,5in=20,9in=18;
        medium:1in=24,5in=22,9in=20;
        wet:1in=26,5in=24,9in=22

    Command-line format:

        --label-thresholds "dry:1in=22,5in=20,9in=18;medium:1in=24,5in=22,9in=20;wet:1in=26,5in=24,9in=22"

    Returns:

        {
            "dry": {"1in": 22, "5in": 20, "9in": 18},
            "medium": {"1in": 24, "5in": 22, "9in": 20},
            "wet": {"1in": 26, "5in": 24, "9in": 22},
        }
    """
    label_threshold_arg = label_threshold_arg.strip()

    if not label_threshold_arg:
        raise ValueError("--label-thresholds was provided but is empty.")

    result: Dict[str, Dict[str, float]] = {}

    label_blocks = [
        block.strip()
        for block in label_threshold_arg.split(";")
        if block.strip()
    ]

    for block in label_blocks:
        if ":" not in block:
            raise ValueError(
                f"Invalid label-threshold block: {block}. "
                f"Expected format like dry:1in=22,5in=20,9in=18"
            )

        label, depth_part = block.split(":", 1)
        label = label.strip()

        if not label:
            raise ValueError(f"Empty label in block: {block}")

        if label in result:
            raise ValueError(f"Duplicate label in --label-thresholds: {label}")

        result[label] = {}

        assignments = [
            item.strip()
            for item in depth_part.split(",")
            if item.strip()
        ]

        for assignment in assignments:
            if "=" not in assignment:
                raise ValueError(
                    f"Invalid depth assignment: {assignment}. "
                    f"Expected format like 1in=22"
                )

            depth, value = assignment.split("=", 1)
            depth = depth.strip()
            value = value.strip()

            if not depth:
                raise ValueError(f"Empty depth in assignment: {assignment}")

            result[label][depth] = float(value)

    # Validate labels.
    dataset_groups = set(groups)
    provided_groups = set(result.keys())

    missing_groups = dataset_groups - provided_groups
    if missing_groups:
        raise ValueError(
            f"--label-thresholds is missing labels {sorted(missing_groups)}. "
            f"Dataset labels are {groups}."
        )

    extra_groups = provided_groups - dataset_groups
    if extra_groups:
        raise ValueError(
            f"--label-thresholds contains labels not found in the dataset: {sorted(extra_groups)}. "
            f"Dataset labels are {groups}."
        )

    # Validate depths for each label.
    dataset_depths = set(depths)

    for label, depth_map in result.items():
        provided_depths = set(depth_map.keys())

        missing_depths = dataset_depths - provided_depths
        if missing_depths:
            raise ValueError(
                f"--label-thresholds for label '{label}' is missing depths "
                f"{sorted(missing_depths)}. Available depths are {depths}."
            )

        extra_depths = provided_depths - dataset_depths
        if extra_depths:
            raise ValueError(
                f"--label-thresholds for label '{label}' contains unknown depths "
                f"{sorted(extra_depths)}. Available depths are {depths}."
            )

    return result


def parse_deadlines(deadline_arg: str, times: List[int]) -> List[int]:
    """
    Supported formats:

    max
        Use only the maximum available time step.

    all
        Use all time steps except t0.

    1,2,4
        Use explicitly listed deadlines.
    """
    deadline_arg = deadline_arg.strip().lower()

    if deadline_arg == "max":
        return [max(times)]

    if deadline_arg == "all":
        return [t for t in times if t > 0]

    deadlines = [int(x.strip()) for x in deadline_arg.split(",") if x.strip()]

    invalid = set(deadlines) - set(times)
    if invalid:
        raise ValueError(
            f"Invalid deadlines {sorted(invalid)}. "
            f"Available time steps are {times}."
        )

    return sorted(deadlines)


# ============================================================
# Group extraction
# ============================================================

def get_requirement_groups(df: pd.DataFrame, granularity: str) -> List[str]:
    """
    Return the initial groups used to create requirements.

    granularity = label:
        groups are initial_label values, e.g., dry, medium, wet.

    granularity = case:
        groups are initial_case_id values.
    """
    if granularity == "label":
        if "initial_label" not in df.columns:
            raise ValueError("CSV must contain initial_label for label-level requirements.")
        return sorted(df["initial_label"].dropna().astype(str).unique())

    if granularity == "case":
        if "initial_case_id" not in df.columns:
            raise ValueError("CSV must contain initial_case_id for case-level requirements.")
        return sorted(df["initial_case_id"].dropna().astype(str).unique())

    raise ValueError("granularity must be either 'label' or 'case'.")


def get_threshold_for_requirement(
    group: str,
    depth: str,
    depth_thresholds: Optional[Dict[str, float]],
    label_thresholds: Optional[Dict[str, Dict[str, float]]],
) -> float:
    """
    Choose threshold for one requirement.

    Priority:
        1. label_thresholds, if provided
        2. depth_thresholds
    """
    if label_thresholds is not None:
        return float(label_thresholds[group][depth])

    if depth_thresholds is not None:
        return float(depth_thresholds[depth])

    raise ValueError("No thresholds were provided.")


# ============================================================
# Build requirements and schedules
# ============================================================

def build_requirements(
    groups: List[str],
    depths: List[str],
    deadlines: List[int],
    granularity: str,
    depth_thresholds: Optional[Dict[str, float]] = None,
    label_thresholds: Optional[Dict[str, Dict[str, float]]] = None,
) -> List[Requirement]:
    """
    Build SWC target requirements.

    granularity = label:
        Requirements are grouped by initial_label:
            dry, medium, wet

        Example:
            dry, 1in, theta >= 22 by t4
            medium, 1in, theta >= 24 by t4
            wet, 1in, theta >= 26 by t4

    granularity = case:
        Requirements are grouped by initial_case_id.

    If label_thresholds is provided, thresholds depend on both:
        initial_group + depth.

    Otherwise, thresholds depend only on depth.
    """
    requirements = []

    for group in groups:
        for depth in depths:
            for deadline in deadlines:
                threshold = get_threshold_for_requirement(
                    group=group,
                    depth=depth,
                    depth_thresholds=depth_thresholds,
                    label_thresholds=label_thresholds,
                )

                req_id = (
                    f"R_{granularity}={group}"
                    f"_depth={depth}"
                    f"_t={deadline}"
                    f"_theta>={threshold:g}"
                )

                description = (
                    f"For initial {granularity} '{group}', "
                    f"depth {depth} must reach theta >= {threshold:g}% "
                    f"by time step {deadline}."
                )

                requirements.append(
                    Requirement(
                        req_id=req_id,
                        initial_group=group,
                        depth=depth,
                        deadline=deadline,
                        threshold=threshold,
                        description=description,
                    )
                )

    return requirements


def build_schedules(df: pd.DataFrame) -> List[Schedule]:
    """
    Build candidate schedules from schedule_id and binary_sequence.

    schedule_id identifies the candidate irrigation schedule.
    binary_sequence is the on/off irrigation pattern, such as 0101.
    """
    if "schedule_id" not in df.columns:
        raise ValueError("CSV must contain schedule_id.")

    if "binary_sequence" in df.columns:
        schedule_df = (
            df[["schedule_id", "binary_sequence"]]
            .drop_duplicates()
            .sort_values("schedule_id")
        )

        schedules = [
            Schedule(
                schedule_id=str(row["schedule_id"]),
                binary_sequence=str(row["binary_sequence"]),
            )
            for _, row in schedule_df.iterrows()
        ]

    else:
        schedule_ids = sorted(df["schedule_id"].dropna().astype(str).unique())
        schedules = [
            Schedule(schedule_id=sid, binary_sequence="")
            for sid in schedule_ids
        ]

    return schedules


# ============================================================
# Coverage computation
# ============================================================

def row_satisfies_requirement(
    row: pd.Series,
    req: Requirement,
    theta_cols: Dict[Tuple[str, int], str],
    deadline_mode: str,
    include_t0: bool,
) -> bool:
    """
    Check whether one row, meaning one trajectory for one
    initial_case_id and one schedule_id, satisfies a requirement.

    deadline_mode = exact:
        Check theta only at the exact deadline.

    deadline_mode = at_or_before:
        Check whether theta reaches the threshold at any time step
        from t1 to deadline by default.

        If include_t0=True, t0 is also allowed.
    """
    if deadline_mode == "exact":
        col = theta_cols.get((req.depth, req.deadline))
        if col is None:
            return False

        value = row[col]
        if pd.isna(value):
            return False

        return float(value) >= req.threshold

    if deadline_mode == "at_or_before":
        min_t = 0 if include_t0 else 1

        candidate_cols = [
            col
            for (depth, time_step), col in theta_cols.items()
            if depth == req.depth and min_t <= time_step <= req.deadline
        ]

        if not candidate_cols:
            return False

        for col in candidate_cols:
            value = row[col]
            if pd.isna(value):
                continue

            if float(value) >= req.threshold:
                return True

        return False

    raise ValueError("deadline_mode must be either 'exact' or 'at_or_before'.")


def compute_coverage(
    df: pd.DataFrame,
    requirements: List[Requirement],
    schedules: List[Schedule],
    theta_cols: Dict[Tuple[str, int], str],
    granularity: str,
    coverage_mode: str,
    deadline_mode: str,
    include_t0: bool,
) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    """
    Compute which requirements each schedule covers.

    coverage_mode = all:
        A schedule covers a label-level requirement only if it satisfies
        the requirement for all sampled initial cases in that label.

        Example:
            schedule A covers "dry, 1in, theta >= 22 by t4"
            only if it succeeds for every dry sampled case.

    coverage_mode = any:
        A schedule covers a requirement if it satisfies at least one
        matching row.
    """
    schedule_ids = [schedule.schedule_id for schedule in schedules]

    schedule_to_requirements = {sid: set() for sid in schedule_ids}
    requirement_to_schedules = {req.req_id: set() for req in requirements}

    for schedule in schedules:
        sched_df = df[df["schedule_id"].astype(str) == schedule.schedule_id]

        for req in requirements:
            if granularity == "label":
                matching_rows = sched_df[
                    sched_df["initial_label"].astype(str) == req.initial_group
                ]
            else:
                matching_rows = sched_df[
                    sched_df["initial_case_id"].astype(str) == req.initial_group
                ]

            if matching_rows.empty:
                covers = False
            else:
                checks = [
                    row_satisfies_requirement(
                        row=row,
                        req=req,
                        theta_cols=theta_cols,
                        deadline_mode=deadline_mode,
                        include_t0=include_t0,
                    )
                    for _, row in matching_rows.iterrows()
                ]

                if coverage_mode == "all":
                    covers = all(checks)
                elif coverage_mode == "any":
                    covers = any(checks)
                else:
                    raise ValueError("coverage_mode must be either 'all' or 'any'.")

            if covers:
                schedule_to_requirements[schedule.schedule_id].add(req.req_id)
                requirement_to_schedules[req.req_id].add(schedule.schedule_id)

    return schedule_to_requirements, requirement_to_schedules


# ============================================================
# PySMT solving
# ============================================================

def safe_symbol_name(name: str) -> str:
    """
    Convert schedule IDs into safe PySMT symbol names.
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", str(name))


def solve_with_pysmt(
    schedules: List[Schedule],
    requirements: List[Requirement],
    requirement_to_schedules: Dict[str, Set[str]],
    budget: int,
    solver_name: str,
) -> Dict:
    """
    Build the formula:

        For every requirement r_j:
            OR over all schedules that cover r_j.

        Budget:
            Sum of selected schedules <= k.

    Then solve with PySMT.
    """
    x = {
        schedule.schedule_id: Symbol(
            f"select_{safe_symbol_name(schedule.schedule_id)}",
            BOOL,
        )
        for schedule in schedules
    }

    constraints = []

    # Coverage constraints:
    # Every requirement must be covered by at least one selected schedule.
    for req in requirements:
        covering_schedule_ids = sorted(requirement_to_schedules[req.req_id])

        if covering_schedule_ids:
            constraints.append(
                Or([x[schedule_id] for schedule_id in covering_schedule_ids])
            )
        else:
            # If no schedule covers this requirement, the formula is UNSAT.
            constraints.append(Bool(False))

    # Budget constraint:
    # Sum_i x_i <= budget
    selected_count_terms = [
        Ite(x[schedule.schedule_id], Int(1), Int(0))
        for schedule in schedules
    ]

    if selected_count_terms:
        selected_count = Plus(selected_count_terms)
    else:
        selected_count = Int(0)

    constraints.append(LE(selected_count, Int(int(budget))))

    formula = And(constraints)

    sat = is_sat(formula, solver_name=solver_name)

    if not sat:
        impossible_requirements = [
            req.req_id
            for req in requirements
            if len(requirement_to_schedules[req.req_id]) == 0
        ]

        return {
            "satisfiable": False,
            "budget": budget,
            "budget_used": 0,
            "selected_schedule_ids": [],
            "requirements_with_no_possible_cover": impossible_requirements,
            "message": "UNSAT: no set of schedules covers all requirements within the budget.",
        }

    model = get_model(formula, solver_name=solver_name)

    selected_schedule_ids = []

    for schedule in schedules:
        val = model.get_value(x[schedule.schedule_id])
        if val.is_true():
            selected_schedule_ids.append(schedule.schedule_id)

    return {
        "satisfiable": True,
        "budget": budget,
        "budget_used": len(selected_schedule_ids),
        "selected_schedule_ids": selected_schedule_ids,
        "message": "SAT: found a set of schedules covering all requirements within the budget.",
    }


def find_minimum_budget(
    schedules: List[Schedule],
    requirements: List[Requirement],
    requirement_to_schedules: Dict[str, Set[str]],
    solver_name: str,
) -> Dict:
    """
    Try k = 0, 1, 2, ..., number of schedules until SAT.
    """
    for k in range(len(schedules) + 1):
        result = solve_with_pysmt(
            schedules=schedules,
            requirements=requirements,
            requirement_to_schedules=requirement_to_schedules,
            budget=k,
            solver_name=solver_name,
        )

        if result["satisfiable"]:
            result["minimum_budget"] = k
            return result

    return {
        "satisfiable": False,
        "minimum_budget": None,
        "budget": None,
        "budget_used": 0,
        "selected_schedule_ids": [],
        "message": "UNSAT: no selection of schedules covers all requirements.",
    }


# ============================================================
# Output helpers
# ============================================================

def write_instance_json(
    out_path: Path,
    budget: Optional[int],
    requirements: List[Requirement],
    schedules: List[Schedule],
    schedule_to_requirements: Dict[str, Set[str]],
) -> None:
    """
    Write the generated Set Cover instance.
    """
    data = {
        "budget": budget,
        "requirements": [asdict(req) for req in requirements],
        "actions": [
            {
                "id": schedule.schedule_id,
                "binary_sequence": schedule.binary_sequence,
                "covers": sorted(schedule_to_requirements[schedule.schedule_id]),
            }
            for schedule in schedules
        ],
    }

    out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_coverage_csv(
    out_path: Path,
    schedules: List[Schedule],
    requirements: List[Requirement],
    schedule_to_requirements: Dict[str, Set[str]],
) -> None:
    """
    Write a readable coverage matrix in long CSV format.
    """
    rows = []

    for schedule in schedules:
        covered = schedule_to_requirements[schedule.schedule_id]

        for req in requirements:
            rows.append(
                {
                    "schedule_id": schedule.schedule_id,
                    "binary_sequence": schedule.binary_sequence,
                    "requirement_id": req.req_id,
                    "covers": int(req.req_id in covered),
                    "initial_group": req.initial_group,
                    "depth": req.depth,
                    "deadline": req.deadline,
                    "threshold": req.threshold,
                    "requirement_description": req.description,
                }
            )

    pd.DataFrame(rows).to_csv(out_path, index=False)


def add_solution_details(
    result: Dict,
    schedules: List[Schedule],
    requirements: List[Requirement],
    schedule_to_requirements: Dict[str, Set[str]],
) -> Dict:
    """
    Add selected schedule details and covered requirements to result JSON.
    """
    selected_ids = set(result.get("selected_schedule_ids", []))

    selected_schedules = []
    covered_requirements = set()

    for schedule in schedules:
        if schedule.schedule_id in selected_ids:
            covers = sorted(schedule_to_requirements[schedule.schedule_id])
            covered_requirements.update(covers)

            selected_schedules.append(
                {
                    "schedule_id": schedule.schedule_id,
                    "binary_sequence": schedule.binary_sequence,
                    "covers": covers,
                }
            )

    requirement_lookup = {req.req_id: req for req in requirements}

    result["selected_schedules"] = selected_schedules
    result["covered_requirements"] = sorted(covered_requirements)
    result["num_requirements"] = len(requirements)
    result["num_covered_requirements"] = len(covered_requirements)
    result["all_requirements_covered"] = (
        len(covered_requirements) == len(requirements)
    )
    result["requirements"] = [
        asdict(requirement_lookup[req_id])
        for req_id in sorted(requirement_lookup.keys())
    ]

    return result


# ============================================================
# Main
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Solve SWC Target Cover using PySMT and the summary CSV file."
    )

    parser.add_argument(
        "--summary-csv",
        required=True,
        help="Path to controller_level_summary CSV file.",
    )

    parser.add_argument(
        "--budget",
        type=int,
        default=None,
        help="Maximum number of schedules to select.",
    )

    parser.add_argument(
        "--find-min-budget",
        action="store_true",
        help="Search for the minimum feasible budget instead of using one fixed budget.",
    )

    parser.add_argument(
        "--thresholds",
        default="same:20",
        help=(
            "Depth-only theta thresholds. Use same:20 or depth-specific format, "
            "for example 1in:22,5in:20,9in:18. "
            "Ignored if --label-thresholds is provided."
        ),
    )

    parser.add_argument(
        "--label-thresholds",
        default=None,
        help=(
            "Label-specific theta thresholds. Example: "
            "\"dry:1in=22,5in=20,9in=18;"
            "medium:1in=24,5in=22,9in=20;"
            "wet:1in=26,5in=24,9in=22\". "
            "This overrides --thresholds."
        ),
    )

    parser.add_argument(
        "--deadlines",
        default="max",
        help="Deadlines to use: max, all, or comma-separated values like 1,2,3,4.",
    )

    parser.add_argument(
        "--granularity",
        choices=["label", "case"],
        default="label",
        help=(
            "label = requirements are dry/medium/wet level. "
            "case = requirements are per initial_case_id."
        ),
    )

    parser.add_argument(
        "--coverage-mode",
        choices=["all", "any"],
        default="all",
        help=(
            "all = schedule must satisfy all sampled cases in the group. "
            "any = schedule satisfies the requirement if any matching sampled case succeeds."
        ),
    )

    parser.add_argument(
        "--deadline-mode",
        choices=["exact", "at_or_before"],
        default="at_or_before",
        help=(
            "exact = theta at exactly the deadline must satisfy the target. "
            "at_or_before = theta can satisfy the target at any time <= deadline."
        ),
    )

    parser.add_argument(
        "--include-t0",
        action="store_true",
        help=(
            "Allow t0 to count for at_or_before requirements. "
            "By default, only t1,...,deadline are checked."
        ),
    )

    parser.add_argument(
        "--solver",
        default="z3",
        help="PySMT solver name, for example z3, cvc5, or msat.",
    )

    parser.add_argument(
        "--out-prefix",
        default="swc_target_cover",
        help="Prefix for output files.",
    )

    args = parser.parse_args()

    if not args.find_min_budget and args.budget is None:
        raise ValueError("Provide --budget or use --find-min-budget.")

    if args.budget is not None and args.budget < 0:
        raise ValueError("--budget must be nonnegative.")

    if args.label_thresholds is not None and args.granularity != "label":
        raise ValueError(
            "--label-thresholds currently requires --granularity label, "
            "because thresholds are specified by initial_label."
        )

    summary_path = Path(args.summary_csv)
    out_prefix = Path(args.out_prefix)

    df = pd.read_csv(summary_path)

    # Keep schedule IDs stable as strings.
    if "schedule_id" in df.columns:
        df["schedule_id"] = df["schedule_id"].astype(str)

    # Find theta columns.
    theta_cols = find_theta_columns(df)

    if not theta_cols:
        raise ValueError(
            "No theta columns found. Expected columns like theta_1in_t0, theta_5in_t4, etc."
        )

    depths, times = get_depths_and_times(theta_cols)
    deadlines = parse_deadlines(args.deadlines, times)
    groups = get_requirement_groups(df, args.granularity)

    # Choose threshold mode.
    depth_thresholds = None
    label_thresholds = None

    if args.label_thresholds is not None:
        label_thresholds = parse_label_thresholds(
            label_threshold_arg=args.label_thresholds,
            groups=groups,
            depths=depths,
        )
    else:
        depth_thresholds = parse_thresholds(args.thresholds, depths)

    requirements = build_requirements(
        groups=groups,
        depths=depths,
        deadlines=deadlines,
        granularity=args.granularity,
        depth_thresholds=depth_thresholds,
        label_thresholds=label_thresholds,
    )

    schedules = build_schedules(df)

    schedule_to_requirements, requirement_to_schedules = compute_coverage(
        df=df,
        requirements=requirements,
        schedules=schedules,
        theta_cols=theta_cols,
        granularity=args.granularity,
        coverage_mode=args.coverage_mode,
        deadline_mode=args.deadline_mode,
        include_t0=args.include_t0,
    )

    # Write generated Set Cover instance.
    write_instance_json(
        out_path=Path(f"{out_prefix}.instance.json"),
        budget=args.budget,
        requirements=requirements,
        schedules=schedules,
        schedule_to_requirements=schedule_to_requirements,
    )

    # Write coverage matrix.
    write_coverage_csv(
        out_path=Path(f"{out_prefix}.coverage.csv"),
        schedules=schedules,
        requirements=requirements,
        schedule_to_requirements=schedule_to_requirements,
    )

    # Solve.
    if args.find_min_budget:
        result = find_minimum_budget(
            schedules=schedules,
            requirements=requirements,
            requirement_to_schedules=requirement_to_schedules,
            solver_name=args.solver,
        )
    else:
        result = solve_with_pysmt(
            schedules=schedules,
            requirements=requirements,
            requirement_to_schedules=requirement_to_schedules,
            budget=args.budget,
            solver_name=args.solver,
        )

    result = add_solution_details(
        result=result,
        schedules=schedules,
        requirements=requirements,
        schedule_to_requirements=schedule_to_requirements,
    )

    result["settings"] = {
        "summary_csv": str(summary_path),
        "thresholds": args.thresholds,
        "label_thresholds": args.label_thresholds,
        "deadlines": args.deadlines,
        "granularity": args.granularity,
        "coverage_mode": args.coverage_mode,
        "deadline_mode": args.deadline_mode,
        "include_t0": args.include_t0,
        "solver": args.solver,
    }

    result_path = Path(f"{out_prefix}.result.json")

    result_path.write_text(
        json.dumps(result, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(result, indent=2))
    print()
    print(f"Wrote instance to: {out_prefix}.instance.json")
    print(f"Wrote coverage matrix to: {out_prefix}.coverage.csv")
    print(f"Wrote result to: {out_prefix}.result.json")


if __name__ == "__main__":
    main()