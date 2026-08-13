"""Batch solver for the multi-worker assembly-line rebalancing problem.

The script reads task allocations produced by the balancing model and solves a
rebalancing problem for a new cycle time. The model jointly determines:

* the station assigned to every task;
* whether each station uses one or two workers;
* workload and ergonomic-load fairness across workers; and
* preservation of relevant task groupings through the pruned Mean Similarity
  Factor (MSF-p).

Expected inputs
---------------
``generated_csv_balanced_USED/``
    Balancing CSV files. Each file must contain ``task_id``, ``predecessors``,
    ``execution_time``, ``ergonomic_value``, ``area``, and
    ``previous_station``. Filenames should follow
    ``INSTANCE_runN_SN_WN_CTN.csv``.
``Scholl_parameters.xlsx``
    Parameter table with at least ``Inst_name`` and ``CT_reb`` columns.

Outputs
-------
For each input file, the script writes a task-to-station assignment and a
one-row results CSV. It also produces a combined batch summary.

The instances are processed in separate processes. Each Gurobi model uses a
small, configurable thread count to avoid CPU oversubscription.
"""

import gurobipy as gp
import numpy as np
from gurobipy import GRB
import pandas as pd
import ast
import os
import re
import time
import openpyxl  # needed by pandas.read_excel on .xlsx
from collections import deque
from datetime import datetime
from typing import Dict, List, Tuple, Optional
import multiprocessing as mp

# Objective weights: workload fairness, ergonomic fairness, and preservation.
ALPHA = 1/3
BETA  = 1/3
GAMMA = 1/3

# A short solve is used to identify the minimum feasible workforce. The selected
# workforce is then solved once with the final time limit.
TIME_LIMIT_PER_TRY = 120     # seconds
TIME_LIMIT_FINAL   = 5400   # seconds
MIP_FOCUS          = 1       # find feasible solutions earlier

# Keep the thread count per model low when several instances run in parallel.
GUROBI_THREADS_PER_PROCESS = 1
N_PROCESSES = None  # e.g., 12 to use all logical cores on your i5-1245U
PRINT_DONE_PER_FILE = True


# Paths are resolved relative to this script so it can run from any directory.
base_folder = os.path.dirname(__file__)
instances_folder = os.path.join(base_folder, "generated_csv_balanced_USED")        # balancing outputs
results_folder   = os.path.join(base_folder, "results_rebalancing_K30_checkreview")       # outputs
params_xlsx      = os.path.join(base_folder, "Scholl_parameters.xlsx")
os.makedirs(results_folder, exist_ok=True)

AREA_MAP = {"external": 0, "internal": 1}

def safe_X(var):
    """Return a Gurobi variable value, or ``None`` when it is unavailable."""
    try:
        return var.X
    except Exception:
        return None

def topo_order(tasks, preds_dict):
    """Return a Kahn topological order and successors for the precedence DAG."""
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

def load_ct_reb_map(xlsx_path: str) -> Dict[str, float]:
    """Load rebalancing cycle times, keyed by normalized instance name."""
    params = pd.read_excel(xlsx_path)
    needed = {"Inst_name", "CT_reb"}
    missing = needed - set(params.columns)
    if missing:
        raise ValueError(f"Excel missing columns: {missing}. Expected at least {sorted(needed)}")
    m = {}
    for _, r in params.iterrows():
        inst = str(r["Inst_name"]).strip().upper()
        if pd.isna(r["CT_reb"]):
            continue
        m[inst] = float(r["CT_reb"])
    return m

_BAL_FN_RE = re.compile(r"^(?P<inst>.+?)_run(?P<run>\d+)_S(?P<S>\d+)_W(?P<W>\d+)_CT(?P<CT>\d+)\.csv$", re.IGNORECASE)

def parse_balancing_filename(path: str) -> Tuple[str, int, Optional[int]]:
    """
    Return ``(instance_name, num_stations, run_index)`` from a result filename.

    The preferred format is ``INST_run2_S18_W23_CT192.csv``. A limited
    fallback accepts filenames that contain at least ``_S<number>``.
    """
    fn = os.path.basename(path)
    m = _BAL_FN_RE.match(fn)
    if not m:
        # fallback: try to find _S(\d+)
        m2 = re.search(r"_S(\d+)", fn)
        if not m2:
            raise ValueError(f"Cannot infer num_stations from filename: {fn}")
        S = int(m2.group(1))
        inst = os.path.splitext(fn)[0].split("_run")[0]
        return inst, S, None
    return m.group("inst"), int(m.group("S")), int(m.group("run"))

def solve_rebalancing(df: pd.DataFrame,
                      instance_name: str,
                      num_stations: int,
                      cycle_time: float,
                      num_workers: int,
                      time_limit: int,
                      log: bool = False) -> Dict:
    """
    Solve one multi-worker rebalancing instance.

    Returns a dictionary containing ``assignment_df`` and ``result_row``.
    ``RuntimeError`` is raised if no incumbent is found within ``time_limit``.
    """

    # Parse predecessors
    df_local = df.copy()
    if df_local["predecessors"].dtype != object:
        df_local["predecessors"] = df_local["predecessors"].astype(str)
    df_local["predecessors"] = df_local["predecessors"].apply(ast.literal_eval)

    # area -> {0,1}
    df_local["area_binary"] = df_local["area"].map(AREA_MAP)
    if df_local["area_binary"].isna().any():
        bad = df_local[df_local["area_binary"].isna()]["area"].unique()
        raise ValueError(f"{instance_name}: unknown area labels: {bad}")

    tasks = df_local["task_id"].astype(int).tolist()
    stations = list(range(num_stations))

    # Need previous_station in input
    if "previous_station" not in df_local.columns:
        raise ValueError(f"{instance_name}: input CSV missing 'previous_station' column (must come from balancing output)")

    # Feasibility check: u implies 1 or 2 workers/station
    if not (num_stations <= num_workers <= 2 * num_stations):
        raise ValueError(
            f"{instance_name}: infeasible staffing: num_workers={num_workers} must be in "
            f"[num_stations, 2*num_stations]=[{num_stations},{2*num_stations}]"
        )

    time_dict = dict(zip(df_local["task_id"].astype(int), df_local["execution_time"].astype(float)))
    ergo_dict = dict(zip(df_local["task_id"].astype(int), df_local["ergonomic_value"].astype(float)))
    area_dict = dict(zip(df_local["task_id"].astype(int), df_local["area_binary"].astype(int)))
    preds_dict = {int(row["task_id"]): list(row["predecessors"]) for _, row in df_local.iterrows()}

    df_idx = df_local.set_index("task_id", drop=False)

    _, succ = topo_order(tasks, preds_dict)

    # Averages per worker (constants)
    Tavg = float(sum(time_dict.values()) / num_workers)
    Eavg = float(sum(ergo_dict.values()) / num_workers)

    # MSF-p protects only the most relevant tasks. Relevance combines normalized
    # processing time, ergonomic load, and structural degree in the precedence
    # graph. K_RELEVANT controls the fraction included in the similarity term.
    USE_RELEVANT_SUBSET = True
    K_RELEVANT = 0.30
    MAX_TIB = None

    indeg = {t: len(preds_dict[t]) for t in tasks}
    outdeg = {t: len(succ[t]) for t in tasks}
    deg = {t: indeg[t] + outdeg[t] for t in tasks}

    def _minmax_norm(values_dict):
        vals = np.array(list(values_dict.values()), dtype=float)
        vmin, vmax = float(vals.min()), float(vals.max())
        denom = (vmax - vmin) if (vmax - vmin) > 1e-12 else 1.0
        return {k: (float(v) - vmin) / denom for k, v in values_dict.items()}

    t_norm = _minmax_norm(time_dict)
    e_norm = _minmax_norm(ergo_dict)
    d_norm = _minmax_norm(deg)
    score = {i: 0.4*t_norm[i] + 0.4*e_norm[i] + 0.2*d_norm[i] for i in tasks}

    if USE_RELEVANT_SUBSET:
        k = max(1, int(np.ceil(K_RELEVANT * len(tasks))))
        R = set(sorted(tasks, key=lambda i: (-score[i], int(i)))[:k])
    else:
        R = set(tasks)

    TIB = {}
    for i in tasks:
        if i not in R:
            TIB[i] = []
            continue
        prev_i = int(df_idx.loc[i, "previous_station"])
        tib_i = [j for j in R if j != i and int(df_idx.loc[j, "previous_station"]) == prev_i]
        if MAX_TIB is not None and len(tib_i) > MAX_TIB:
            tib_i = sorted(tib_i, key=lambda j: (-score[j], int(j)))[:MAX_TIB]
        TIB[i] = tib_i

    # Build the mixed-integer linear model.
    m = gp.Model(f"REBAL_MSF_{instance_name}_S{num_stations}_W{num_workers}_CT{int(cycle_time)}")
    m.Params.OutputFlag = 1 if log else 0
    m.Params.TimeLimit = time_limit
    m.Params.MIPFocus = MIP_FOCUS
    m.Params.Presolve = 2
    m.Params.Cuts = 2
    m.Params.Threads = GUROBI_THREADS_PER_PROCESS
    m.Params.Heuristics = 0.2

    # z[i,s] assigns task i to station s. u[s] equals 1 for a single-worker
    # station and 0 for a two-worker station.
    z = m.addVars(tasks, stations, vtype=GRB.BINARY, name="z")
    u = m.addVars(stations, vtype=GRB.BINARY, name="u")  # 1 alone, 0 two-worker

    Wext = m.addVars(stations, vtype=GRB.CONTINUOUS, lb=0.0, name="Wext")
    Wint = m.addVars(stations, vtype=GRB.CONTINUOUS, lb=0.0, name="Wint")
    W    = m.addVars(stations, vtype=GRB.CONTINUOUS, lb=0.0, name="W")
    Eext = m.addVars(stations, vtype=GRB.CONTINUOUS, lb=0.0, name="Eext")
    Eint = m.addVars(stations, vtype=GRB.CONTINUOUS, lb=0.0, name="Eint")
    E    = m.addVars(stations, vtype=GRB.CONTINUOUS, lb=0.0, name="E")

    devT_ext = m.addVars(stations, lb=0.0, name="devT_ext")
    devT_int = m.addVars(stations, lb=0.0, name="devT_int")
    devT_al  = m.addVars(stations, lb=0.0, name="devT_alone")

    devE_ext = m.addVars(stations, lb=0.0, name="devE_ext")
    devE_int = m.addVars(stations, lb=0.0, name="devE_int")
    devE_al  = m.addVars(stations, lb=0.0, name="devE_alone")

    # Each task is assigned to exactly one station.
    for i in tasks:
        m.addConstr(gp.quicksum(z[i, s] for s in stations) == 1, name=f"assign_once_{i}")

    # Precedence is enforced through the station-index expression st_pos.
    st_pos = m.addVars(tasks, vtype=GRB.CONTINUOUS, lb=0, ub=num_stations-1, name="st_pos")
    for i in tasks:
        m.addConstr(st_pos[i] == gp.quicksum(s * z[i, s] for s in stations), name=f"stpos_def_{i}")
    for j in tasks:
        for i in preds_dict[j]:
            m.addConstr(st_pos[int(i)] <= st_pos[int(j)], name=f"prec_{i}_{j}")

    # Since each station has one or two workers, station s uses 2 - u[s].
    m.addConstr(gp.quicksum(2 - u[s] for s in stations) == num_workers, name="total_workers_1or2")

    Mwork = 2.0 * cycle_time
    Merg = float(sum(ergo_dict.values())) if len(ergo_dict) > 0 else 0.0

    # Define workload/ergonomic totals and absolute-deviation variables. At a
    # two-worker station, external and internal areas represent separate worker
    # loads. At a single-worker station, their combined load is used instead.
    for s in stations:
        m.addConstr(Wext[s] == gp.quicksum(time_dict[i] * z[i, s] for i in tasks if area_dict[i] == 0), name=f"Wext_def_{s}")
        m.addConstr(Wint[s] == gp.quicksum(time_dict[i] * z[i, s] for i in tasks if area_dict[i] == 1), name=f"Wint_def_{s}")
        m.addConstr(W[s]    == Wext[s] + Wint[s], name=f"W_def_{s}")

        m.addConstr(Eext[s] == gp.quicksum(ergo_dict[i] * z[i, s] for i in tasks if area_dict[i] == 0), name=f"Eext_def_{s}")
        m.addConstr(Eint[s] == gp.quicksum(ergo_dict[i] * z[i, s] for i in tasks if area_dict[i] == 1), name=f"Eint_def_{s}")
        m.addConstr(E[s]    == Eext[s] + Eint[s], name=f"E_def_{s}")

        # A single worker has CT capacity; two workers have 2*CT in total.
        m.addConstr(W[s] <= cycle_time * (2 - u[s]), name=f"station_capacity_{s}")
        # Each area has CT capacity with two workers. With one worker, the two
        # areas may be combined, so the per-area upper bound becomes 2*CT.
        m.addConstr(Wext[s] <= cycle_time * (1 + u[s]), name=f"area_cap_ext_{s}")
        m.addConstr(Wint[s] <= cycle_time * (1 + u[s]), name=f"area_cap_int_{s}")

        # External-area deviations are active only at two-worker stations.
        m.addConstr(devT_ext[s] >=  Wext[s] - Tavg - Mwork * u[s])
        m.addConstr(devT_ext[s] >= -Wext[s] + Tavg - Mwork * u[s])
        m.addConstr(devT_ext[s] <= Mwork * (1 - u[s]))

        m.addConstr(devE_ext[s] >=  Eext[s] - Eavg - Merg * u[s])
        m.addConstr(devE_ext[s] >= -Eext[s] + Eavg - Merg * u[s])
        m.addConstr(devE_ext[s] <= Merg * (1 - u[s]))

        # Internal-area deviations are active only at two-worker stations.
        m.addConstr(devT_int[s] >=  Wint[s] - Tavg - Mwork * u[s])
        m.addConstr(devT_int[s] >= -Wint[s] + Tavg - Mwork * u[s])
        m.addConstr(devT_int[s] <= Mwork * (1 - u[s]))

        m.addConstr(devE_int[s] >=  Eint[s] - Eavg - Merg * u[s])
        m.addConstr(devE_int[s] >= -Eint[s] + Eavg - Merg * u[s])
        m.addConstr(devE_int[s] <= Merg * (1 - u[s]))

        # Combined-load deviations are active only at single-worker stations.
        m.addConstr(devT_al[s] >=  W[s] - Tavg - Mwork * (1 - u[s]))
        m.addConstr(devT_al[s] >= -W[s] + Tavg - Mwork * (1 - u[s]))
        m.addConstr(devT_al[s] <= Mwork * u[s])

        m.addConstr(devE_al[s] >=  E[s] - Eavg - Merg * (1 - u[s]))
        m.addConstr(devE_al[s] >= -E[s] + Eavg - Merg * (1 - u[s]))
        m.addConstr(devE_al[s] <= Merg * u[s])

    # For every relevant pair previously grouped in one station, w_aux equals
    # z[i,s] * z[j,s]. The three inequalities provide its exact linearization.
    w_aux = {}
    SF_exprs = []
    R_list = [i for i in tasks if i in R]

    for i in R_list:
        TIB_i = TIB[i]
        denom = len(TIB_i)
        if denom == 0:
            SF_exprs.append(gp.LinExpr(1.0))
        else:
            common_expr = gp.LinExpr()
            for j in TIB_i:
                for s in stations:
                    w_aux[i, j, s] = m.addVar(vtype=GRB.BINARY, name=f"w_{i}_{j}_{s}")
                    m.addConstr(w_aux[i, j, s] <= z[i, s])
                    m.addConstr(w_aux[i, j, s] <= z[j, s])
                    m.addConstr(w_aux[i, j, s] >= z[i, s] + z[j, s] - 1)
                    common_expr += w_aux[i, j, s]
            SF_exprs.append(common_expr / denom)

    MSF_expr = (gp.quicksum(SF_exprs) / max(1, len(R_list))) if len(R_list) > 0 else gp.LinExpr(1.0)

    # Normalize the three components before applying the objective weights.
    fairT = gp.quicksum(devT_ext[s] + devT_int[s] + devT_al[s] for s in stations)
    fairE = gp.quicksum(devE_ext[s] + devE_int[s] + devE_al[s] for s in stations)

    eps = 1e-9
    f1_norm = fairT / (num_workers * cycle_time + eps)
    f2_norm = fairE / (sum(ergo_dict.values()) + eps)
    f3_norm = (1.0 - MSF_expr)

    m.setObjective(ALPHA * f1_norm + BETA * f2_norm + GAMMA * f3_norm, GRB.MINIMIZE)

    start = time.time()
    m.optimize()
    end = time.time()

    if m.SolCount == 0:
        raise RuntimeError(f"{instance_name}: no feasible solution (S={num_stations}, W={num_workers}, CT={cycle_time})")

    # Extract the incumbent task allocation.
    assign_rows = []
    for i in tasks:
        assigned_station = next(s for s in stations if z[i, s].X > 0.5)
        assign_rows.append([int(i), int(assigned_station), float(time_dict[i]), float(ergo_dict[i]), int(area_dict[i])])
    assignment_df = pd.DataFrame(assign_rows, columns=["task_id", "station", "execution_time", "ergonomic_value", "area_binary"])

    # Convert total absolute deviations to worker-level MAD and percentage MAD.
    fairT_val = float(sum(devT_ext[s].X + devT_int[s].X + devT_al[s].X for s in stations))
    fairE_val = float(sum(devE_ext[s].X + devE_int[s].X + devE_al[s].X for s in stations))

    workers_used = int(round(sum(2 - u[s].X for s in stations)))
    Tavg_val = float(Tavg)
    Eavg_val = float(Eavg)

    MAD_T = fairT_val / (workers_used + eps)
    MAD_E = fairE_val / (workers_used + eps)
    MADpct_T = 100.0 * MAD_T / (Tavg_val + eps)
    MADpct_E = 100.0 * MAD_E / (Eavg_val + eps)

    # Solver statistics and line efficiency at the prescribed cycle time.
    runtime = float(m.Runtime) if m.SolCount > 0 else None
    wallclock = float(end - start)
    nodecount = int(m.NodeCount)
    solcount = int(m.SolCount)
    mipgap = float(m.MIPGap) if m.SolCount > 0 else None
    objval = float(m.ObjVal) if m.SolCount > 0 else None
    objbound = float(m.ObjBound)

    total_work = float(sum(time_dict.values()))
    eff_givenCT = float(total_work / (num_workers * cycle_time + eps))

    result_row = {
        "instance": instance_name,
        "stations": int(num_stations),
        "CT_reb": float(cycle_time),
        "min_workers": int(num_workers),
        "runtime": runtime,
        "nodecount": nodecount,
        "solcount": solcount,
        "mipgap": mipgap,
        "objval": objval,
        "MSF": float(MSF_expr.getValue()) if m.SolCount > 0 else None,
        "eff_givenCT": eff_givenCT,

        # Fairness metrics reported in absolute and normalized form.
        "Tavg": Tavg_val,
        "fairT": fairT_val,
        "MAD_T": float(MAD_T),
        "MADpct_T": float(MADpct_T),
        "Eavg": Eavg_val,
        "fairE": fairE_val,
        "MAD_E": float(MAD_E),
        "MADpct_E": float(MADpct_E),
        "f1_workload_norm": float(f1_norm.getValue()),
        "f2_ergonomic_norm": float(f2_norm.getValue()),
        "f3_similarity_norm": float(f3_norm.getValue()),
    }

    return {
        "assignment_df": assignment_df,
        "result_row": result_row,
    }

def find_min_workers_reb(df: pd.DataFrame,
                         instance_name: str,
                         num_stations: int,
                         cycle_time: float) -> int:
    """
    Return the smallest workforce for which the model finds an incumbent.

    Candidate workforce sizes are tested in ascending order from ``S`` to
    ``2*S`` using the short feasibility-search time limit.
    """
    for w in range(num_stations, 2 * num_stations + 1):
        try:
            _ = solve_rebalancing(df, instance_name, num_stations, cycle_time, w, time_limit=TIME_LIMIT_PER_TRY, log=False)
            return w
        except RuntimeError:
            continue
    raise RuntimeError(f"{instance_name}: infeasible for all W in [{num_stations}, {2*num_stations}] with CT_reb={cycle_time} and S={num_stations}")

def _process_one_file(args: Dict) -> Dict:
    """
    Process one balancing output and write its allocation and result files.

    A result row is returned even on failure, allowing the parent process to
    record the status in the combined batch summary.
    """
    file = args["file"]
    ct_reb_map = args["ct_reb_map"]

    inst_raw, S, run_idx = parse_balancing_filename(file)
    inst_key = str(inst_raw).strip().upper()

    if inst_key not in ct_reb_map:
        return {
            "instance": inst_raw,
            "stations": S,
            "CT_reb": None,
            "min_workers": None,
            "status": "SKIP_NO_CT_REB",
            "input_file": os.path.basename(file),
        }

    CT_reb = float(ct_reb_map[inst_key])

    try:
        df = pd.read_csv(file)

        # Search for the minimum feasible workforce at the rebalancing CT.
        Wmin = find_min_workers_reb(df, inst_raw, S, CT_reb)

        # Solve the selected workforce using the final time limit.
        out = solve_rebalancing(df, inst_raw, S, CT_reb, Wmin, time_limit=TIME_LIMIT_FINAL, log=False)
        assignment_df = out["assignment_df"]
        result_row = out["result_row"]

        tag = f"run{run_idx}" if run_idx is not None else "runX"
        assign_path = os.path.join(results_folder, f"{inst_raw}_{tag}_S{S}_W{Wmin}_assignment.csv")
        assignment_df.to_csv(assign_path, index=False)

        res_path = os.path.join(results_folder, f"{inst_raw}_{tag}_S{S}_W{Wmin}_results.csv")
        pd.DataFrame([result_row]).to_csv(res_path, index=False)

        result_row["status"] = "OK"
        result_row["input_file"] = os.path.basename(file)
        result_row["output_results"] = os.path.basename(res_path)
        result_row["output_assignment"] = os.path.basename(assign_path)
        return result_row

    except Exception as e:
        return {
            "instance": inst_raw,
            "stations": S,
            "CT_reb": CT_reb,
            "min_workers": None,
            "status": "FAIL",
            "error": str(e),
            "input_file": os.path.basename(file),
        }

def run_all_rebalancing_parallel():
    """Solve all balancing-output CSV files and save a combined summary."""
    if not os.path.isdir(instances_folder):
        raise FileNotFoundError(f"Input folder not found: {instances_folder}")
    if not os.path.isfile(params_xlsx):
        raise FileNotFoundError(f"Parameters Excel not found: {params_xlsx}")

    ct_reb_map = load_ct_reb_map(params_xlsx)

    files = [os.path.join(instances_folder, f) for f in os.listdir(instances_folder) if f.lower().endswith(".csv")]
    # Keep only balancing outputs that expose run and station metadata.
    files = [f for f in files if "_run" in os.path.basename(f).lower() and "_s" in os.path.basename(f).lower()]
    files = sorted(files)

    print(f"[REB-MP] Found {len(files)} balancing-output CSVs in {instances_folder}")
    print(f"[REB-MP] Loaded CT_reb for {len(ct_reb_map)} instances from {params_xlsx}")
    print(f"[REB-MP] Output folder: {results_folder}")
    print(f"[REB-MP] Threads per process: {GUROBI_THREADS_PER_PROCESS}")

    # Use all logical cores unless an explicit process count is configured.
    if N_PROCESSES is None:
        nproc = max(1, (os.cpu_count() or 2))  # use all logical cores by default
    else:
        nproc = int(N_PROCESSES)
    print(f"[REB-MP] Processes: {nproc}")

    # ``spawn`` is portable and avoids sharing a Gurobi environment by fork.
    ctx = mp.get_context("spawn")

    args_list = [{"file": f, "ct_reb_map": ct_reb_map} for f in files]

    start_time = datetime.now() if "datetime" in globals() else None

    all_rows = []
    completed = 0
    total = len(args_list)

    with ctx.Pool(processes=nproc, maxtasksperchild=1) as pool:
        for row in pool.imap_unordered(_process_one_file, args_list):
            all_rows.append(row)
            completed += 1
            if PRINT_DONE_PER_FILE:
                inst = row.get("instance", "UNKNOWN")
                run = None
                try:
                    # Recover the run index solely for the progress message.
                    m = _BAL_FN_RE.match(row.get("input_file",""))
                    run = m.group("run") if m else None
                except Exception:
                    run = None
                run_str = f" run{run}" if run else ""
                print(f"[DONE] {inst}{run_str} ({completed}/{total}) status={row.get('status')}", flush=True)

    # Store both successful runs and diagnostic rows for skipped/failed files.
    if all_rows:
        summary_path = os.path.join(results_folder, "rebalancing_batch_summary.csv")
        pd.DataFrame(all_rows).to_csv(summary_path, index=False)
        print(f"[REB-MP] Batch summary saved to {summary_path}")

    if start_time is not None:
        elapsed = datetime.now() - start_time
        print(f"[REB-MP] Done. Elapsed: {elapsed}")
    else:
        print("[REB-MP] Done.")

if __name__ == "__main__":
    run_all_rebalancing_parallel()
