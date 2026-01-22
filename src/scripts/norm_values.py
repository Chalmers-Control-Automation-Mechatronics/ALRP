import gurobipy as gp
from gurobipy import GRB
import pandas as pd
import math
import ast
import os
import time
import re
from multiprocessing import Pool

'''
This script computes the nadir and utopia reference values for the three
optimization objectives considered in this work.

Since the objectives (Mean Similarity Factor, workload imbalance, and
ergonomic imbalance) have different scales, a normalization step is
required before applying multi-objective optimization techniques.
In this work, a nadir–utopia normalization is adopted.

For a given objective:
  - the *utopia value* corresponds to the best achievable value when optimizing that objective alone;
  - the *nadir value* corresponds to the worst value attained among these single-objective optima.

These values define a problem-dependent normalization range and are computed separately for each
instance.

For each feasible instance provided as input, the instance is solved
three times using the same mathematical model, activating one objective
at a time:
(i) minimization of workload imbalance,
(ii) minimization of ergonomic imbalance,
(iii) maximization of the Mean Similarity Factor (MSF).

Each run produces a feasible solution and the corresponding values of
all three objectives. The collection of objective values obtained from
these three single-objective optimizations defines the utopia and nadir
bounds for each objective of the given instance.

The results are saved to a CSV file (one per instance), containing the
objective values obtained from the three runs. These files are then used
to extract the final nadir and utopia values required for objective
normalization.

'''


# =======================
# PATHS
# =======================
BASE_DIR = os.path.dirname(__file__)
instances_folder = os.path.join(BASE_DIR, "instances20", "opt_instances")

# Output folder (one folder)
output_folder = os.path.join(BASE_DIR, "20tasks_nadir_utopia_results")
os.makedirs(output_folder, exist_ok=True)

# Summary file (one file for all instances)
summary_csv = os.path.join(output_folder, "20tasks_utopia_nadir_summary.csv")

# 3 single-objective runs (alpha, beta, gamma)
weight_sets = [
    (1.0, 0.0, 0.0),  # workload only
    (0.0, 1.0, 0.0),  # ergonomics only
    (0.0, 0.0, 1.0),  # MSF only
]

# =======================
# Model runner
# =======================
def run_instance(file_path, weights):
    alpha, beta, gamma = weights
    instance_name = os.path.basename(file_path)

    try:
        instance_num = int(''.join([c for c in instance_name if c.isdigit()][-3:])) # extract 3 final digits
    except ValueError:
        instance_num = None

    df = pd.read_csv(file_path)
    df["predecessors"] = df["predecessors"].apply(ast.literal_eval)
    df["area_binary"] = df["area"].map({"external": 0, "internal": 1})

    tasks = df["task_id"].tolist()
    task_times = df["execution_time"].tolist()
    ergonomic_value = df["ergonomic_value"].tolist()


    # --- params ---
    num_stations = 3
    cycle_time = 20

    if instance_num in [7]:
        num_workers = 6
    elif instance_num in [1, 2, 8, 10]:
        num_workers = 5
    else:
        num_workers = 4

    stations = list(range(num_stations))
    workers = list(range(num_workers))

    min_workers_per_station = num_workers // num_stations
    max_workers_per_station = math.ceil(num_workers / num_stations)

    task_area = dict(zip(df["task_id"], df["area_binary"]))

    # ----------------------------------------GUROBI MODEL-------------------------------------------------
    m = gp.Model("assembly_line_rebalancing")

    # Binary: task i assigned to worker w
    x = m.addVars(tasks, workers, vtype=GRB.BINARY, name="x")

    # Binary: worker w assigned to station s
    y = m.addVars(stations, workers, vtype=GRB.BINARY, name="y")

    # Binary: task i assigned to station s
    z = m.addVars(tasks, stations, vtype=GRB.BINARY, name="z")

    # Variables (continuous) : workload for each worker
    workload = m.addVars(workers, vtype=GRB.CONTINUOUS, name="workload")

    # Variables (continuous) : ergonomic load for each worker
    ergonomic_load = m.addVars(workers, vtype=GRB.CONTINUOUS, name="ergonomic_load")

    # Variables (continuous) : for the workload balance
    max_time = m.addVar(vtype=GRB.CONTINUOUS, name="max_time")
    min_time = m.addVar(vtype=GRB.CONTINUOUS, name="min_time")

    # Variables (continuous) : for the ergonomic load balance
    min_erg = m.addVar(vtype=GRB.CONTINUOUS, name="min_ergonomic_load")
    max_erg = m.addVar(vtype=GRB.CONTINUOUS, name="max_ergonomic_load")

    # Variables (continuous): terms of the objective function
    w_balance = m.addVar(name="workload_balance")
    erg_balance = m.addVar(name="ergonomic_balance")

    # Variable : shared station
    shared = m.addVars(stations, vtype=GRB.BINARY, name="shared")
    for s in stations:
        m.addConstr(gp.quicksum(y[s, w] for w in workers) >= 2 * shared[s])
        m.addConstr(gp.quicksum(y[s, w] for w in workers) <= 1 + (len(workers) - 1) * shared[s])

    # Variable : worker in shared station
    worker_shared = m.addVars(workers, vtype=GRB.BINARY, name="worker_shared")
    for w in workers:
        m.addConstr(worker_shared[w] >= gp.quicksum(shared[s] * y[s, w] for s in stations) / len(stations))

    # CONSTRAINTS
    # Constraint 1 : each task assigned to a single worker
    for i in tasks:
        m.addConstr(gp.quicksum(x[i, w] for w in workers) == 1, name=f"assign_once_t{i}")

    # Constraint 1.1 : each task assigned to a single station
    for i in tasks:
        m.addConstr(gp.quicksum(z[i, s] for s in stations) == 1, name=f"assign_station_once_t{i}")

    # Constraint 2 : each worker is assigned to at most one station
    for w in workers:
        m.addConstr(gp.quicksum(y[s, w] for s in stations) <= 1, name=f"worker_one_station_w{w}")

    # Constraint 3 : Calculating workload per worker
    for w in workers:
        m.addConstr(workload[w] == gp.quicksum(task_times[i] * x[i, w] for i in tasks),
                    name=f"workload_def_w{w}")

    # Constraint 4 & 5 : Definition of the max_time and min_time
    for w in workers:
        m.addConstr(workload[w] <= max_time, name=f"max_workload_w{w}")
        m.addConstr(workload[w] >= min_time, name=f"min_workload_w{w}")

    # Constraint 6 : The workload of each worker has to be less or equal the cycle time
    for w in workers:
        m.addConstr(workload[w] <= cycle_time, name=f"workload_limit_w{w}")

    # Constraint 7 : Calculating ergonomic load per worker
    for w in workers:
        m.addConstr(ergonomic_load[w] == gp.quicksum(x[i, w] * ergonomic_value[i] for i in tasks),
                    name=f"ergonomic_def_w{w}")

    # Constraint 8 & 9 : Definition of the max_erg and min_erg
    for w in workers:
        m.addConstr(ergonomic_load[w] >= min_erg, name=f"min_erg_w{w}")
        m.addConstr(ergonomic_load[w] <= max_erg, name=f"max_erg_w{w}")

    # Constraint 10 : If worker w execute a task in station s, then w is assigned to s
    for i in tasks:
        for s in stations:
            for w in workers:
                m.addConstr(x[i, w] <= y[s, w] + (1 - z[i, s]), name=f"consistency_{i}_{s}_{w}")

    # Constraint 11 : Upper e Lower bound for the number of worker in each station
    for s in stations:
        m.addConstr(gp.quicksum(y[s, w] for w in workers) >= min_workers_per_station,
                    name=f"min_workers_station_{s}")
        m.addConstr(gp.quicksum(y[s, w] for w in workers) <= max_workers_per_station,
                    name=f"max_workers_station_{s}")

    # Constraint 12 : Precedence relation between task i and j (i before j)
    for j in tasks:
        preds = df.loc[j, "predecessors"]
        for i in preds:
            for s1 in stations:
                for s2 in stations:
                    if s1 > s2:
                        m.addConstr(z[i, s1] + z[j, s2] <= 1, name=f"precedence_illegal_{i}_{j}_s{s1}_s{s2}")

    # Constraint 13 : Mismatch area constraint only if the worker is in a shared station
    for w in workers:
        m.addConstr(
            gp.quicksum(x[i, w] * task_area[i] for i in tasks) *
            gp.quicksum(x[i, w] * (1 - task_area[i]) for i in tasks)
            <= (1 - worker_shared[w]) * len(tasks) ** 2
        )

    # Constraint 14 & 15: Objective function terms
    m.addConstr(w_balance == max_time - min_time, name="workload_balance_def")
    m.addConstr(erg_balance == max_erg - min_erg, name="ergonomic_balance_def")

    # Compute the MSF (Mean Similarity Factor)

    TIB = dict()
    for i in df["task_id"]:
        TIB[i] = [j for j in df["task_id"] if
                  j != i and df.loc[j, "previous_station"] == df.loc[i, "previous_station"]]

    w = {}  # auxiliary variables
    SF_exprs = []

    for i in tasks:
        TIB_i = TIB[i]
        denom = len(TIB_i)

        if denom == 0:
            SF_exprs.append(gp.LinExpr(1.0))  # for isolated tasks
        else:
            common_expr = gp.LinExpr()

            for j in TIB_i:
                for s in stations:
                    # Auxiliary binary variable for z[i,s] * z[j,s]
                    w[i, j, s] = m.addVar(vtype=GRB.BINARY, name=f"w_{i}_{j}_{s}")

                    # Linearization constraints
                    m.addConstr(w[i, j, s] <= z[i, s], name=f"w_ub1_{i}_{j}_{s}")
                    m.addConstr(w[i, j, s] <= z[j, s], name=f"w_ub2_{i}_{j}_{s}")
                    m.addConstr(w[i, j, s] >= z[i, s] + z[j, s] - 1, name=f"w_lb_{i}_{j}_{s}")

                    # Add to the numerator
                    common_expr += w[i, j, s]

            SF_exprs.append(common_expr / denom)

        # MSF = mean of all the SF(i)
    MSF_expr = gp.quicksum(SF_exprs) / len(tasks)



    # f1 = workload balance (min), f2 = ergonomic balance (min), f3 = MSF (max)
    f1_expr = gp.LinExpr(w_balance)
    f2_expr = gp.LinExpr(erg_balance)
    f3_expr = gp.LinExpr(MSF_expr)

    # OBJECTIVE
    # obj1: minimize the difference between maximum and minimum workload
    # obj2: minimize the difference between maximum and minimum ergonomic load
    # obj3: maximize the MSF
    m.setObjective(alpha * f1_expr + beta * f2_expr - gamma * f3_expr, GRB.MINIMIZE)

    m.Params.NonConvex = 2
    m.Params.Threads = 1 # Fix number of threads for consistency

    m.optimize()

    if not (m.status == GRB.OPTIMAL or m.SolCount > 0):
        return {
            "instance": instance_name,
            "w1": alpha, "w2": beta, "w3": gamma,
            "f1": None, "f2": None, "f3": None,
        }

    # robust values (avoid accessing .X if no solution)
    f1_val = max(workload[w].X for w in workers) - min(workload[w].X for w in workers)
    f2_val = max(ergonomic_load[w].X for w in workers) - min(ergonomic_load[w].X for w in workers)
    f3_val = f3_expr.getValue()

    return {
        "instance": instance_name,
        "w1": alpha, "w2": beta, "w3": gamma,
        "f1": f1_val,
        "f2": f2_val,
        "f3": f3_val,
    }


# =======================
# Per-instance pipeline: run 3 times + compute utopia/nadir
# =======================
def process_instance(instance_filename):
    """Run a single instance with the 3 sets of weights"""
    file_path = os.path.join(instances_folder, instance_filename)

    # extract the final digits of the instance name for identification
    m_id = re.search(r"(\d+)\.csv$", instance_filename)
    instance_id = int(m_id.group(1)) if m_id else instance_filename

    rows = []
    for weights in weight_sets:
        print(f"▶️ {instance_filename} | weights={weights}")
        rows.append(run_instance(file_path, weights))

    df_runs = pd.DataFrame(rows)

    # compute utopia/nadir only if we have valid f1,f2,f3
    valid = df_runs[["f1", "f2", "f3"]].notna().all(axis=1)
    if valid.sum() == 0:
        # no feasible solution in any of the 3 runs
        return {
            "instance": instance_id,
            "f1_min": None, "f2_min": None, "f3_min": None,
            "f1_max": None, "f2_max": None, "f3_max": None,
            "n_valid_runs": 0
        }

    dfv = df_runs.loc[valid, ["f1", "f2", "f3"]]

    print("dtype f2:", dfv["f2"].dtype)
    print("esempio valori f2:", dfv["f2"].dropna().head(5).tolist())
    print("type primo valore:", type(dfv["f2"].dropna().iloc[0]))
    print("min f2 raw:", dfv["f2"].min(), " type:", type(dfv["f2"].min()))

    summary_row = {
        "instance": instance_id,
        "f1_min": float(dfv["f1"].min()),
        "f2_min": float(dfv["f2"].min()),
        "f3_min": float(dfv["f3"].min()),
        "f1_max": float(dfv["f1"].max()),
        "f2_max": float(dfv["f2"].max()),
        "f3_max": float(dfv["f3"].max()),
    }
    print(f"✅ Done instance {instance_id}")
    return summary_row


# =======================
# MAIN: parallel over instances, write one summary csv
# =======================
if __name__ == "__main__":
    instance_files = [f for f in os.listdir(instances_folder) if f.endswith(".csv")]
    print(f"🚀 Found {len(instance_files)} instances in {instances_folder}")

    # run in parallel
    with Pool(processes=max(os.cpu_count() - 1, 1)) as pool:
        summary_rows = pool.map(process_instance, instance_files)

    df_summary = pd.DataFrame(summary_rows).sort_values("instance")
    df_summary.to_csv(summary_csv, index=False)
    print(f"\n💾 Summary saved: {summary_csv}")
    print(df_summary.round(6))
