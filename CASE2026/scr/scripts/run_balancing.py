"""Generate balanced reference allocations for assembly-line instances.

For every instance and station count listed in ``Scholl_parameters.xlsx``, this
script solves a multi-worker assembly-line balancing model.  The model assigns
each task to a station while respecting precedence, cycle-time, and task-area
constraints.  Among feasible allocations, it minimizes workload and ergonomic
load deviations across workers.

The script searches for the smallest feasible number of workers, starting with
one worker per station and allowing at most two.  Each successful run produces
a copy of the input CSV with an additional ``previous_station`` column.  These
allocations can subsequently be used as reference configurations in
rebalancing experiments.

Expected files next to this script
----------------------------------
generated_csv/
    One CSV per instance. Required columns are ``task_id``, ``predecessors``,
    ``execution_time``, ``ergonomic_value``, and ``area``.
Scholl_parameters.xlsx
    Required columns are ``Inst_name``, ``st1``, ``st2``, ``st3``, and ``CT``.

Outputs are written to ``generated_csv_balanced_mp/``.  Runs for different
instances are executed in separate processes; each process owns an independent
Gurobi model and uses ``GUROBI_THREADS_PER_PROCESS`` solver threads.

"""

import gurobipy as gp
from gurobipy import GRB
import pandas as pd
import ast
import os
import openpyxl  # needed by pandas.read_excel on .xlsx
from collections import deque
from typing import Dict, List, Tuple
import multiprocessing as mp
from datetime import datetime

# -------------------- Objective weights --------------------
ALPHA = 0.5  # workload fairness weight
BETA  = 0.5  # ergonomic fairness weight

# -------------------- solve policy --------------------
TIME_LIMIT_PER_TRY = 120     # seconds per try: we only need a feasible previous allocation
MIP_FOCUS          = 1       # focus on feasibility

# -------------------- parallel policy --------------------
# IMPORTANT with Gurobi:
# - Each process runs its own solver instance (uses license tokens accordingly).
# - To avoid CPU oversubscription, keep Threads small (often 1).
GUROBI_THREADS_PER_PROCESS = 1

# Number of Python worker processes. Set to None for a conservative automatic value.
N_PROCESSES = 4  # e.g., 4

# -------------------- IO paths (relative to this file) --------------------
BASE_DIR = os.path.dirname(__file__)
INSTANCES_DIR = os.path.join(BASE_DIR, "generated_csv")
PARAMETERS_XLSX = os.path.join(BASE_DIR, "Scholl_parameters.xlsx")
OUTPUT_DIR = os.path.join(BASE_DIR, "generated_csv_balanced_mp")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Binary encoding used by the model; labels must match the input CSVs exactly.
AREA_MAP = {"external": 0, "internal": 1}

# -------------------- graph helpers --------------------
def topo_order(tasks: List[int], preds_dict: Dict[int, List[int]]) -> Tuple[List[int], Dict[int, List[int]]]:
    """Validate the precedence graph and return a topological task order.

    Kahn's algorithm is used only as an input validation step. The returned
    successor dictionary is currently not needed by the optimization model but
    is useful to callers that need both graph directions.

    Raises:
        ValueError: If the graph contains a cycle or references an unknown task.
    """
    indeg = {t: 0 for t in tasks}
    succ = {t: [] for t in tasks}
    for j in tasks:
        for i in preds_dict[j]:
            succ[i].append(j)
            indeg[j] += 1
    q = deque([t for t in tasks if indeg[t] == 0])
    order = []
    while q:
        u = q.popleft()
        order.append(u)
        for v in succ[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)
    if len(order) != len(tasks):
        raise ValueError("Precedence graph has a cycle or missing tasks.")
    return order, succ

# -------------------- model builder/solver --------------------
def solve_balancing(
    df: pd.DataFrame,
    instance_name: str,
    num_stations: int,
    cycle_time: float,
    num_workers: int,
    time_limit: int,
    log: bool = False,  # multiprocessing -> keep logs quiet (avoids interleaving)
) -> Tuple[Dict[int, int], float]:
    """Solve one balancing model for fixed staffing and cycle time.

    Args:
        df: Task data for one assembly-line instance.
        instance_name: Label used in the Gurobi model name and error messages.
        num_stations: Fixed number of stations in the line.
        cycle_time: Maximum workload available to each worker.
        num_workers: Fixed total number of workers across all stations.
        time_limit: Gurobi time limit in seconds.
        log: Whether to print the Gurobi log for this solve.

    Returns:
        A pair containing the task-to-station mapping and line efficiency,
        defined as total processing time divided by total worker capacity.

    Raises:
        ValueError: If the input data or staffing level is invalid.
        RuntimeError: If Gurobi finds no feasible incumbent within the limit.
    """
    # Parse predecessors safely (expect list-like strings)
    if df["predecessors"].dtype != object:
        df["predecessors"] = df["predecessors"].astype(str)
    df_local = df.copy()
    df_local["predecessors"] = df_local["predecessors"].apply(ast.literal_eval)

    # Map area to {0,1}
    df_local["area_binary"] = df_local["area"].map(AREA_MAP)
    if df_local["area_binary"].isna().any():
        bad = df_local[df_local["area_binary"].isna()]["area"].unique()
        raise ValueError(f"{instance_name}: unknown area labels: {bad}")

    tasks = df_local["task_id"].astype(int).tolist()
    stations = list(range(num_stations))

    # Hard feasibility bounds due to u[s] modeling: each station has 1 or 2 workers
    if not (num_stations <= num_workers <= 2 * num_stations):
        raise ValueError(
            f"{instance_name}: infeasible staffing: num_workers={num_workers} must be in "
            f"[num_stations, 2*num_stations]=[{num_stations},{2*num_stations}]"
        )

    time_dict = dict(zip(df_local["task_id"].astype(int), df_local["execution_time"].astype(float)))
    ergo_dict = dict(zip(df_local["task_id"].astype(int), df_local["ergonomic_value"].astype(float)))
    area_dict = dict(zip(df_local["task_id"].astype(int), df_local["area_binary"].astype(int)))
    preds_dict = {int(row["task_id"]): list(row["predecessors"]) for _, row in df_local.iterrows()}

    # Validate the precedence data before constructing solver expressions.
    topo_order(tasks, preds_dict)

    # Reference loads used by the mean-absolute-deviation fairness terms.
    Tavg = float(sum(time_dict.values()) / num_workers)
    Eavg = float(sum(ergo_dict.values()) / num_workers)

    m = gp.Model(f"ALR_balancing_{instance_name}_S{num_stations}_W{num_workers}_CT{int(cycle_time)}")
    m.Params.OutputFlag = 1 if log else 0
    m.Params.TimeLimit = time_limit
    m.Params.MIPFocus = MIP_FOCUS
    m.Params.Threads = GUROBI_THREADS_PER_PROCESS

    # z[i,s] = 1 iff task i is assigned to station s.
    z = m.addVars(tasks, stations, vtype=GRB.BINARY, name="z")

    # u[s] = 1 -> station has 1 worker; u[s] = 0 -> station has 2 workers
    u = m.addVars(stations, vtype=GRB.BINARY, name="u")

    # Workload (W) and ergonomic load (E), split by work area. In a two-worker
    # station, the external and internal areas represent the two worker loads.
    Wext = m.addVars(stations, vtype=GRB.CONTINUOUS, lb=0.0, name="Wext")
    Wint = m.addVars(stations, vtype=GRB.CONTINUOUS, lb=0.0, name="Wint")
    W    = m.addVars(stations, vtype=GRB.CONTINUOUS, lb=0.0, name="W")
    Eext = m.addVars(stations, vtype=GRB.CONTINUOUS, lb=0.0, name="Eext")
    Eint = m.addVars(stations, vtype=GRB.CONTINUOUS, lb=0.0, name="Eint")
    E    = m.addVars(stations, vtype=GRB.CONTINUOUS, lb=0.0, name="E")

    # Absolute-deviation variables for the two possible station structures:
    # separate external/internal workers or one worker covering the whole station.
    devT_ext = m.addVars(stations, vtype=GRB.CONTINUOUS, lb=0.0, name="devT_ext")
    devT_int = m.addVars(stations, vtype=GRB.CONTINUOUS, lb=0.0, name="devT_int")
    devT_al  = m.addVars(stations, vtype=GRB.CONTINUOUS, lb=0.0, name="devT_alone")

    devE_ext = m.addVars(stations, vtype=GRB.CONTINUOUS, lb=0.0, name="devE_ext")
    devE_int = m.addVars(stations, vtype=GRB.CONTINUOUS, lb=0.0, name="devE_int")
    devE_al  = m.addVars(stations, vtype=GRB.CONTINUOUS, lb=0.0, name="devE_alone")

    # (1) each task assigned to exactly one station
    for i in tasks:
        m.addConstr(gp.quicksum(z[i, s] for s in stations) == 1, name=f"assign_once_{i}")

    # (2) Preserve precedence. st_pos is the station index implied by z; allowing
    # equality permits a predecessor and successor to be assigned together.
    st_pos = m.addVars(tasks, vtype=GRB.CONTINUOUS, lb=0, ub=num_stations - 1, name="st_pos")
    for i in tasks:
        m.addConstr(st_pos[i] == gp.quicksum(s * z[i, s] for s in stations), name=f"stpos_def_{i}")
    for j in tasks:
        for i in preds_dict[j]:
            m.addConstr(st_pos[i] <= st_pos[j], name=f"prec_{i}_{j}")

    # (3) total workers fixed
    m.addConstr(gp.quicksum(2 - u[s] for s in stations) == num_workers, name="total_workers")

    # (4) station totals and capacity
    for s in stations:
        m.addConstr(Wext[s] == gp.quicksum(time_dict[i] * z[i, s] for i in tasks if area_dict[i] == 0), name=f"Wext_{s}")
        m.addConstr(Wint[s] == gp.quicksum(time_dict[i] * z[i, s] for i in tasks if area_dict[i] == 1), name=f"Wint_{s}")
        m.addConstr(W[s]    == Wext[s] + Wint[s], name=f"W_{s}")

        m.addConstr(Eext[s] == gp.quicksum(ergo_dict[i] * z[i, s] for i in tasks if area_dict[i] == 0), name=f"Eext_{s}")
        m.addConstr(Eint[s] == gp.quicksum(ergo_dict[i] * z[i, s] for i in tasks if area_dict[i] == 1), name=f"Eint_{s}")
        m.addConstr(E[s]    == Eext[s] + Eint[s], name=f"E_{s}")

        # Total capacity: CT for a one-worker station and 2*CT for two workers.
        m.addConstr(W[s] <= cycle_time * (2 - u[s]), name=f"cap_station_{s}")

        # With two workers (u=0), each area is individually limited by CT. With
        # one worker (u=1), these constraints relax to 2*CT; the tighter total
        # station capacity above then enforces the actual CT limit.
        m.addConstr(Wext[s] <= cycle_time * (1 + u[s]), name=f"cap_ext_{s}")
        m.addConstr(Wint[s] <= cycle_time * (1 + u[s]), name=f"cap_int_{s}")

    # (5) Linearize absolute deviations from average worker loads. Big-M terms
    # activate the variables that correspond to the chosen station structure.
    Mwork = 2.0 * cycle_time
    Merg  = float(sum(ergo_dict.values())) if len(ergo_dict) > 0 else 0.0

    for s in stations:
        # External-worker deviation is active only when u[s] = 0 (two workers).
        m.addConstr(devT_ext[s] >=  Wext[s] - Tavg - Mwork * u[s], name=f"devT_ext_pos_{s}")
        m.addConstr(devT_ext[s] >= -Wext[s] + Tavg - Mwork * u[s], name=f"devT_ext_neg_{s}")
        m.addConstr(devT_ext[s] <= Mwork * (1 - u[s]),            name=f"devT_ext_off_{s}")

        m.addConstr(devE_ext[s] >=  Eext[s] - Eavg - Merg * u[s],  name=f"devE_ext_pos_{s}")
        m.addConstr(devE_ext[s] >= -Eext[s] + Eavg - Merg * u[s],  name=f"devE_ext_neg_{s}")
        m.addConstr(devE_ext[s] <= Merg * (1 - u[s]),              name=f"devE_ext_off_{s}")

        # Internal-worker deviation is active only when u[s] = 0 (two workers).
        m.addConstr(devT_int[s] >=  Wint[s] - Tavg - Mwork * u[s],  name=f"devT_int_pos_{s}")
        m.addConstr(devT_int[s] >= -Wint[s] + Tavg - Mwork * u[s],  name=f"devT_int_neg_{s}")
        m.addConstr(devT_int[s] <= Mwork * (1 - u[s]),              name=f"devT_int_off_{s}")

        m.addConstr(devE_int[s] >=  Eint[s] - Eavg - Merg * u[s],   name=f"devE_int_pos_{s}")
        m.addConstr(devE_int[s] >= -Eint[s] + Eavg - Merg * u[s],   name=f"devE_int_neg_{s}")
        m.addConstr(devE_int[s] <= Merg * (1 - u[s]),               name=f"devE_int_off_{s}")

        # Whole-station deviation is active only when u[s] = 1 (one worker).
        m.addConstr(devT_al[s]  >=  W[s] - Tavg - Mwork * (1 - u[s]), name=f"devT_al_pos_{s}")
        m.addConstr(devT_al[s]  >= -W[s] + Tavg - Mwork * (1 - u[s]), name=f"devT_al_neg_{s}")
        m.addConstr(devT_al[s]  <= Mwork * u[s],                      name=f"devT_al_off_{s}")

        m.addConstr(devE_al[s]  >=  E[s] - Eavg - Merg * (1 - u[s]),  name=f"devE_al_pos_{s}")
        m.addConstr(devE_al[s]  >= -E[s] + Eavg - Merg * (1 - u[s]),  name=f"devE_al_neg_{s}")
        m.addConstr(devE_al[s]  <= Merg * u[s],                        name=f"devE_al_off_{s}")

    # Normalize both MAD numerators to make the two fairness criteria comparable.
    fairT = gp.quicksum(devT_ext[s] + devT_int[s] + devT_al[s] for s in stations)
    fairE = gp.quicksum(devE_ext[s] + devE_int[s] + devE_al[s] for s in stations)

    eps = 1e-9
    f1_norm = fairT / (num_workers * cycle_time + eps)
    f2_norm = fairE / (sum(ergo_dict.values()) + eps)

    m.setObjective(ALPHA * f1_norm + BETA * f2_norm, GRB.MINIMIZE)

    # General Gurobi tuning only; no variable Start attributes are assigned.
    m.Params.Heuristics = 0.2
    m.Params.Cuts = 2
    m.Params.Presolve = 2

    m.optimize()

    if m.SolCount == 0:
        raise RuntimeError(f"{instance_name}: no feasible solution (S={num_stations}, W={num_workers}, CT={cycle_time})")

    prev_station = {}
    for i in tasks:
        best_s = max(stations, key=lambda s: z[i, s].X)
        prev_station[int(i)] = int(best_s)

    total_time = sum(time_dict.values())
    total_capacity = num_workers * cycle_time
    line_efficiency = float(total_time / total_capacity) if total_capacity > 0 else 0.0

    return prev_station, line_efficiency

def find_min_workers(df: pd.DataFrame, instance_name: str, num_stations: int, cycle_time: float) -> Tuple[int, Dict[int, int], float]:
    """Return a solution using the first feasible worker count.

    Worker counts are tested in ascending order from one to two workers per
    station. Consequently, the first feasible result uses the minimum tested
    number of workers. A timed-out solve without an incumbent is treated as
    unsuccessful and the search continues with one additional worker.
    """
    for w in range(num_stations, 2 * num_stations + 1):
        try:
            mapping, eff = solve_balancing(
                df=df,
                instance_name=instance_name,
                num_stations=num_stations,
                cycle_time=cycle_time,
                num_workers=w,
                time_limit=TIME_LIMIT_PER_TRY,
                log=False,
            )
            return w, mapping, eff
        except RuntimeError:
            continue
    raise RuntimeError(
        f"{instance_name}: infeasible for all W in [{num_stations}, {2*num_stations}] (S={num_stations}, CT={cycle_time})"
    )

# -------------------- IO helpers --------------------
def load_parameters(xlsx_path: str) -> pd.DataFrame:
    """Load the experiment table and verify its required columns."""
    params = pd.read_excel(xlsx_path)
    needed = {"Inst_name", "st1", "st2", "st3", "CT"}
    missing = needed - set(params.columns)
    if missing:
        raise ValueError(f"Excel missing columns: {missing}. Expected {sorted(needed)}")
    return params

def build_csv_index(instances_dir: str) -> Dict[str, str]:
    """Map case-insensitive instance names to CSV paths."""
    idx = {}
    for f in os.listdir(instances_dir):
        if not f.lower().endswith(".csv"):
            continue
        name = os.path.splitext(f)[0].upper()
        idx[name] = os.path.join(instances_dir, f)
    return idx

# -------------------- worker --------------------
def _process_one_instance(job: Dict) -> List[Dict]:
    """Solve every requested station configuration for one instance.

    This function is the multiprocessing entry point. It writes successful
    allocations immediately and returns one summary dictionary per attempted
    configuration; exceptions are recorded as failed runs rather than stopping
    the complete batch.
    """
    inst = job["Inst_name"]
    csv_path = job["csv_path"]
    CT = float(job["CT"])
    station_list = job["stations"]  # [(run_idx, S), ...]

    # ensure output dir exists (safe in multiproc)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    df = pd.read_csv(csv_path)
    summary_rows: List[Dict] = []

    for run_idx, S in station_list:
        if S <= 0:
            summary_rows.append({
                "Inst_name": inst,
                "run": run_idx,
                "stations": S,
                "CT": CT,
                "min_workers": None,
                "line_efficiency": None,
                "input_csv": os.path.basename(csv_path),
                "output_csv": None,
                "status": "SKIPPED_S<=0",
            })
            continue

        try:
            W, mapping, eff = find_min_workers(df, inst, int(S), CT)

            df_out = df.copy()
            df_out["previous_station"] = df_out["task_id"].astype(int).map(mapping).astype(int)

            out_name = f"{inst}_run{run_idx}_S{int(S)}_W{int(W)}_CT{int(CT)}.csv"
            out_path = os.path.join(OUTPUT_DIR, out_name)
            df_out.to_csv(out_path, index=False)

            summary_rows.append({
                "Inst_name": inst,
                "run": run_idx,
                "stations": int(S),
                "CT": CT,
                "min_workers": int(W),
                "line_efficiency": float(eff),
                "input_csv": os.path.basename(csv_path),
                "output_csv": out_name,
                "status": "OK",
            })

        except Exception as e:
            summary_rows.append({
                "Inst_name": inst,
                "run": run_idx,
                "stations": int(S),
                "CT": CT,
                "min_workers": None,
                "line_efficiency": None,
                "input_csv": os.path.basename(csv_path),
                "output_csv": None,
                "status": "FAIL",
                "error": str(e),
            })

    return summary_rows

# -------------------- main parallel runner --------------------
def run_all_instances_parallel():
    """Build the experiment jobs, run them in parallel, and save a summary."""
    if not os.path.isdir(INSTANCES_DIR):
        raise FileNotFoundError(
            f"Instances folder not found: {INSTANCES_DIR}\n"
            f"Expected a folder named 'generated_csv' next to this script."
        )
    if not os.path.isfile(PARAMETERS_XLSX):
        raise FileNotFoundError(
            f"Parameters Excel not found: {PARAMETERS_XLSX}\n"
            f"Expected 'Scholl_parameters.xlsx' next to this script."
        )

    params = load_parameters(PARAMETERS_XLSX)
    csv_idx = build_csv_index(INSTANCES_DIR)

    # The spreadsheet is authoritative: unrelated CSVs are ignored.
    jobs: List[Dict] = []
    for _, row in params.iterrows():
        inst = str(row["Inst_name"]).strip()
        inst_key = inst.upper()
        if inst_key not in csv_idx:
            # still return a summary line so you see it in the summary csv
            jobs.append({
                "Inst_name": inst,
                "csv_path": None,
                "CT": float(row["CT"]),
                "stations": [],
                "missing_csv": True,
            })
            continue

        station_list = [int(row["st1"]), int(row["st2"]), int(row["st3"])]
        runs = [(k, S) for k, S in enumerate(station_list, start=1)]  # worker will skip S<=0
        jobs.append({
            "Inst_name": inst,
            "csv_path": csv_idx[inst_key],
            "CT": float(row["CT"]),
            "stations": runs,
            "missing_csv": False,
        })

    print(f"[BAL-MP] Found {len(csv_idx)} CSV files in {INSTANCES_DIR}")
    print(f"[BAL-MP] Loaded {len(params)} parameter rows from {PARAMETERS_XLSX}")
    print(f"[BAL-MP] Jobs to process: {len(jobs)} (extra CSVs are ignored by design)")
    print(f"[BAL-MP] Output folder: {OUTPUT_DIR}")
    print(f"[BAL-MP] Gurobi Threads per process: {GUROBI_THREADS_PER_PROCESS}")

    # Decide number of processes
    if N_PROCESSES is None:
        # conservative default for laptops: half CPUs, at least 1
        nproc = max(1, (os.cpu_count() or 2) // 2)
    else:
        nproc = int(N_PROCESSES)

    # ``spawn`` is portable across operating systems and avoids inheriting a
    # partially initialized solver environment from the parent process.
    ctx = mp.get_context("spawn")

    summary_rows: List[Dict] = []
    start = datetime.now()

    # First handle rows whose CSV is missing (no need to spawn)
    jobs_real = []
    for j in jobs:
        if j.get("missing_csv"):
            summary_rows.append({
                "Inst_name": j["Inst_name"],
                "run": None,
                "stations": None,
                "CT": j["CT"],
                "min_workers": None,
                "line_efficiency": None,
                "input_csv": None,
                "output_csv": None,
                "status": "SKIPPED_MISSING_CSV",
                "error": f"CSV not found in {INSTANCES_DIR}",
            })
        else:
            jobs_real.append(j)

    # Parallel solve
    if jobs_real:
        with ctx.Pool(processes=nproc, maxtasksperchild=1) as pool:
            for rows in pool.imap_unordered(_process_one_instance, jobs_real):
                summary_rows.extend(rows)

    # Save summary
    if summary_rows:
        summary_df = pd.DataFrame(summary_rows)
        summary_path = os.path.join(OUTPUT_DIR, "balancing_batch_summary.csv")
        summary_df.to_csv(summary_path, index=False)
        print(f"[BAL-MP] Summary saved to {summary_path}")

    elapsed = datetime.now() - start
    print(f"[BAL-MP] Done. Elapsed: {elapsed}")

def main():
    """Run the batch experiment from the command line."""
    run_all_instances_parallel()

if __name__ == "__main__":
    main()
