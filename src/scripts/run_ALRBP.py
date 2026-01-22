# src/scripts/run_q_alrbp.py
import os, ast, time
import pandas as pd
import numpy as np
from multiprocessing import Pool, cpu_count
from gurobipy import GRB

from Q_ALRBP_model import build_q_alrbp_model
from L_ALRBP_model import build_l_alrbp_model


"""
run_ALRBP.py — Assembly Line Rebalancing (ALRBP) with fairness and similarity objectives.

This script solves the Assembly Line Rebalancing Problem (ALRBP) for multi-worker stations,
starting from a *feasible incumbent configuration*. The incumbent assignment is provided
in the input CSV via the column `previous_station`.

The script supports two alternative model formulations:
  - a linear ALRBP formulation, and
  - a quadratic ALRBP formulation,

which differ only in the mathematical expression of the workstation–area compatibility constraint.
In the quadratic formulation, this constraint is enforced through bilinear relations between
task-to-station assignment variables, whereas in the linear formulation an equivalent linearized
constraint is adopted.

Given a feasible incumbent solution, the rebalancing problem computes a new assignment of tasks
to stations and workers such that:
  (i) workload imbalance across workers is minimized,
 (ii) ergonomic imbalance across workers is minimized,
(iii) similarity with the incumbent station assignment is maximized, measured through the
      Mean Similarity Factor (MSF).

Objective function
------------------
The problem is formulated as a multi-objective optimization using a weighted sum of three
*objective terms*:
  - workload imbalance (minimized),
  - ergonomic imbalance (minimized),
  - MSF (maximized and converted to a minimization term after normalization).

Since the objectives have different scales and units, nadir–utopia normalization is applied
on a per-instance basis. Normalization bounds are precomputed offline and loaded at runtime.

Execution flow
--------------
For each input instance:
  - the selected model formulation (linear or quadratic) is built,
  - the optimization problem is solved using Gurobi,
  - objective values, solver statistics, and fairness indicators are extracted,
  - results are saved to a CSV file in the results directory.

The script supports parallel execution over multiple instances via Python multiprocessing.

Model selection
---------------
The model formulation (linear or quadratic) is selected through a configuration flag, allowing
the same execution pipeline to be used for both formulations.
"""

MODEL_TYPE = "quadratic"   # or "linear"

def normalized_range(values):
    v = np.array(values, dtype=float)
    return (v.max() - v.min()) / (v.mean() + 1e-9)

def coefficient_of_variation(values):
    v = np.array(values, dtype=float)
    mu = v.mean()
    return 0.0 if mu == 0 else v.std() / mu

def get_normalization_values(instance_id: int, norm_file: str):
    norm_df = pd.read_csv(norm_file)
    row = norm_df.loc[norm_df["instance"] == int(instance_id)]
    if row.empty:
        raise ValueError(f"No normalization values found for instance {instance_id}")
    r = row.iloc[0]
    return dict(
        f1_min=float(r["f1_min"]), f1_max=float(r["f1_max"]),
        f2_min=float(r["f2_min"]), f2_max=float(r["f2_max"]),
        f3_min=float(r["f3_min"]), f3_max=float(r["f3_max"]),
    )

def run_instance(file_path, results_folder, norm_file, num_stations=4, cycle_time=20, weights=(1/3,1/3,1/3)):
    instance_name = os.path.splitext(os.path.basename(file_path))[0]
    instance_num = int(''.join([c for c in instance_name if c.isdigit()][-3:]))

    df = pd.read_csv(file_path)
    df["predecessors"] = df["predecessors"].apply(ast.literal_eval)
    df["area_binary"] = df["area"].map({"external": 0, "internal": 1})

    tasks = df["task_id"].tolist()
    stations = list(range(num_stations))

    # workers: tua logica
    num_workers = 6 if instance_num in [1, 8, 9] else 5
    workers = list(range(num_workers))

    task_times = dict(zip(df["task_id"], df["execution_time"]))
    ergonomic_value = dict(zip(df["task_id"], df["ergonomic_value"]))
    task_area = dict(zip(df["task_id"], df["area_binary"]))

    predecessors = {int(r.task_id): r.predecessors for r in df.itertuples(index=False)}

    # TIB from incumbent previous_station
    TIB = {}
    for i in df["task_id"]:
        si = int(df.loc[df["task_id"] == i, "previous_station"].values[0])
        TIB[i] = [j for j in df["task_id"] if j != i and int(df.loc[df["task_id"] == j, "previous_station"].values[0]) == si]

    min_wps = num_workers // num_stations
    max_wps = int(np.ceil(num_workers / num_stations))

    norm_values = get_normalization_values(instance_num, norm_file)

    if MODEL_TYPE == "quadratic":
        m, ctx = build_q_alrbp_model(
            tasks=tasks, stations=stations, workers=workers,
            task_times=task_times, ergonomic_value=ergonomic_value,
            task_area=task_area, predecessors=predecessors, TIB=TIB,
            cycle_time=cycle_time,
            min_workers_per_station=min_wps, max_workers_per_station=max_wps,
            norm_values=norm_values,
            weights=weights,
        )
        m.Params.NonConvex = 2  # only for the QUADRATIC formulation

    elif MODEL_TYPE == "linear":
        m, ctx = build_l_alrbp_model(
            tasks=tasks, stations=stations, workers=workers,
            task_times=task_times, ergonomic_value=ergonomic_value,
            task_area=task_area, predecessors=predecessors, TIB=TIB,
            cycle_time=cycle_time,
            min_workers_per_station=min_wps, max_workers_per_station=max_wps,
            norm_values=norm_values,
            weights=weights,
        )


    else:
        raise ValueError(f"Unknown MODEL_TYPE: {MODEL_TYPE}")

    # params
    m.Params.TimeLimit = 10800
    m.Params.Threads = 1

    start = time.time()
    m.optimize()
    end = time.time()

    if m.SolCount == 0:
        # salva comunque un record “failed” (utile per robustezza)
        return {"instance": instance_name, "status": int(m.Status), "solcount": 0}

    workload = ctx["workload"]
    ergonomic_load = ctx["ergonomic_load"]
    MSF_expr = ctx["MSF_expr"]

    workloads = [workload[w].X for w in workers]
    ergloads = [ergonomic_load[w].X for w in workers]

    fairness = {
        "norm_range_workload": normalized_range(workloads),
        "cv_workload": coefficient_of_variation(workloads),
        "norm_range_ergonomic": normalized_range(ergloads),
        "cv_ergonomic": coefficient_of_variation(ergloads),
    }

    total_work = sum(workloads)
    eff_givenCT = total_work / (num_workers * cycle_time)
    eff_makespan = total_work / (num_workers * max(workloads))

    result = {
        "instance": instance_name,
        "runtime": m.Runtime,
        "wallclock": end - start,
        "nodecount": m.NodeCount,
        "solcount": m.SolCount,
        "mipgap": m.MIPGap,
        "objval": m.ObjVal,
        "objbound": m.ObjBound,
        "MSF": MSF_expr.getValue(),
        "workload_balance": max(workloads) - min(workloads),
        "ergonomic_balance": max(ergloads) - min(ergloads),
        **fairness,
        "eff_givenCT": eff_givenCT,
        "eff_makespan": eff_makespan,
        "status": int(m.Status),
    }

    out = os.path.join(results_folder, f"{instance_name}_Q_results.csv")
    pd.DataFrame([result]).to_csv(out, index=False)
    return result

def main():
    base_folder = os.path.dirname(__file__)

    instances_folder = os.path.join(base_folder, "..", "..", "data", "feasible_instances", "opt", "25_tasks")
    results_folder = os.path.join(base_folder, "..", "..", "results", "Q_25_results_opt")
    os.makedirs(results_folder, exist_ok=True)

    norm_file = os.path.join(base_folder, "..", "..", "data", "norm_values", "25tasks_utopia_nadir_summary.csv")

    all_instances = [os.path.join(instances_folder, f) for f in os.listdir(instances_folder) if f.endswith(".csv")]

    with Pool(processes=cpu_count()) as pool:
        args = [(fp, results_folder, norm_file) for fp in all_instances]
        results = pool.starmap(run_instance, args)

    print(f"Completed: {len(results)}")

if __name__ == "__main__":
    main()
