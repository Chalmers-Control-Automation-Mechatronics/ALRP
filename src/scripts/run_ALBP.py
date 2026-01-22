import gurobipy as gp
import numpy as np
from gurobipy import Model, GRB, QuadExpr
import pandas as pd
import math
import ast
import os
import time
import re
from multiprocessing import Pool, cpu_count

'''
ALBP.py — Assembly Line Balancing to generate feasible incumbent instances
=========================================================================

Purpose
-------
This script converts *synthetic* assembly-line instances (which may be infeasible by construction)
into *feasible incumbent* instances. The output instances are later used as the starting point
for the Assembly Line Rebalancing problem solved in the main experiments.

What the script does
--------------------
Given an input CSV instance describing tasks (processing times, precedence constraints, ergonomic
values, and task area), the script solves an Assembly Line Balancing
Problem (ALBP) to compute a feasible allocation consistent with:
  - a fixed cycle time (CT) specified for the instance,
  - a fixed number of stations (S) specified by the user,
  - the minimum number of workers required for feasibility under the given CT (pre-computed).

The balancing model is analogous to the rebalancing model used in the paper, with one key difference:
the Mean Similarity Factor (MSF) objective is NOT included here, since the goal is to generate a
feasible incumbent configuration (not to preserve similarity).

Objectives (Balancing stage)
----------------------------
The model optimizes fairness-related objectives only:
  - workload imbalance across workers,
  - ergonomic load imbalance across workers.

The script can produce either:
  - an optimal incumbent solution (when solved to proven optimality), or
  - a suboptimal incumbent solution (by stopping early using a target MIPGap).

Inputs
------
- A folder containing synthetic instances of the same problem size (same number of tasks).
- Each instance is stored as a CSV file and must include at least:
    * task_id
    * execution_time
    * predecessors
    * ergonomic_value
    * task area
    * previous_station (it will not be used in the balancing)

User-defined parameters (per run)
---------------------------------
- Number of stations S: chosen by the user (fixed for the run).
- Cycle time CT: can be fixed globally or set per instance.
- Minimum number of workers: must be computed before solving (given CT) and provided as input.

Outputs
-------
For each input instance, the script writes a new CSV file representing a feasible incumbent instance.
The output is identical to the input except that the column 'previous_station' is replaced by the
station assignment produced by the ALBP solution. The output files are stored in an output folder
(e.g., 'opt_instances/' or 'optimized_instances/').

Optimal vs. suboptimal incumbents (MIPGap)
------------------------------------------
To generate a feasible incumbent, it is not necessary to reach proven optimality: any feasible
solution is sufficient. This can be enforced by limiting the solve quality, e.g.:
    m.setParam("MIPGap", 0.8)
which stops the solver when a solution within 80% relative MIP gap is found (or when the time limit
is reached), still producing a valid feasible incumbent.

How to run
----------
1) Place all synthetic instances with the same number of tasks in the same folder.
2) Set S (number of stations) and CT (cycle time), and compute the minimum number of workers per
   instance (given CT).
3) Run this script; it will solve one ALBP per instance and export the corresponding feasible
   incumbent CSV.
'''



def normalized_range(values):
    v = np.array(values, dtype=float)
    return (v.max() - v.min()) / (v.mean() + 1e-9)


def coefficient_of_variation(values):
    v = np.array(values, dtype=float)
    if v.mean() == 0:
        return 0
    return v.std() / v.mean()


def run_instance(file):
    instance_name = os.path.splitext(os.path.basename(file))[0]

    try:
        instance_num = int(''.join([c for c in instance_name if c.isdigit()][-3:]))  # extract 3 final digits of the instance name
    except ValueError:
        instance_num = None

    df = pd.read_csv(file)


    # EXTRACT TASK FEATURES

    tasks = df["task_id"].tolist()
    task_times = df["execution_time"].tolist()
    ergonomic_value = df["ergonomic_value"].tolist()

    # parsing of the predecessors
    df["predecessors"] = df["predecessors"].apply(ast.literal_eval)

    # conversion working area to binary
    df["area_binary"] = df["area"].map({"external": 0, "internal": 1})
    task_area = dict(zip(df["task_id"], df["area_binary"]))

    # --- model parameters ---
    num_stations = 3
    cycle_time =  17
    num_workers = 5

    '''
    # assigning different cycle times for different instances
    if instance_num in [1, 7, 10]:
        cycle_time = 21
    elif instance_num in [2, 3, 6]:
        cycle_time = 17
    elif instance_num in [4, 9]:
        cycle_time = 18
    else:
        cycle_time = 22

    # minimum number of workers for each instance
    if instance_num in [2, 3, 6, 7]:
        num_workers = 5
    elif instance_num in [5]:
        num_workers = 3
    else:
        num_workers = 4

    '''

    stations = list(range(num_stations))
    workers = list(range(num_workers))

    min_workers_per_station = num_workers // num_stations
    max_workers_per_station = math.ceil(num_workers / num_stations)

# ----------------------------------------GUROBI MODEL-------------------------------------------------
    m = gp.Model("assembly_line_rebalancing")

    # VARIABLES

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
        m.addConstr(gp.quicksum(y[s, w] for w in workers) >= min_workers_per_station, name=f"min_workers_station_{s}")
        m.addConstr(gp.quicksum(y[s, w] for w in workers) <= max_workers_per_station, name=f"max_workers_station_{s}")

    # Constraint 12 : Precedence relation between task i and j (i before j)
    for j in tasks:
        preds = df.loc[j, "predecessors"]
        for i in preds:
            for s1 in stations:
                for s2 in stations:
                    if s1 > s2:
                        m.addConstr(z[i, s1] + z[j, s2] <= 1, name=f"precedence_illegal_{i}_{j}_s{s1}_s{s2}")

    # Variable : shared station
    shared = m.addVars(stations, vtype=GRB.BINARY, name="shared")
    for s in stations:
        m.addConstr(gp.quicksum(y[s, w] for w in workers) >= 2 * shared[s])
        m.addConstr(gp.quicksum(y[s, w] for w in workers) <= 1 + (len(workers) - 1) * shared[s])

    # Variable : worker in shared station
    worker_shared = m.addVars(workers, vtype=GRB.BINARY, name="worker_shared")
    for w in workers:
        m.addConstr(worker_shared[w] >= gp.quicksum(shared[s] * y[s, w] for s in stations) / len(stations))

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

    # f2 = ergonomic balance (min), f3 = MSF (max)
    f2_expr = gp.LinExpr(w_balance)
    f3_expr = gp.LinExpr(erg_balance)


    # OBJECTIVE
    # obj1: minimize the difference between maximum and minimum workload
    # obj2: minimize the difference between maximum and minimum ergonomic load
    alpha = 0.5  # obj1 weight
    beta = 0.5  # obj2 weight

    m.setObjective(alpha * f2_expr + beta * f3_expr, GRB.MINIMIZE)

    m.setParam("NonConvex", 2)
    # m.setParam("MIPGap", 0.8) # MIPGap of 80% (to obtain suboptimal instances)
    # m.Params.TimeLimit = 7200
    m.setParam("Seed", 1)
    m.setParam("Threads", 1)  # Fix number of threads for consistency

    m.optimize()

    m.update()

    workloads = [workload[w].X for w in workers]
    ergloads = [ergonomic_load[w].X for w in workers]


    fairness_metrics = {}

    for label, vec in [("workload", workloads), ("ergonomic", ergloads)]:
        fairness_metrics[f"norm_range_{label}"] = normalized_range(vec)
        fairness_metrics[f"cv_{label}"] = coefficient_of_variation(vec)

    # Output in DataFrame
    if m.status == GRB.OPTIMAL:
        results = []

        for i in tasks:
            for w in workers:
                if x[i, w].X > 0.5:
                    # Find the station s assigned to task i
                    assigned_station = next(s for s in stations if z[i, s].X > 0.5)
                    results.append([i, assigned_station, w, task_times[i], ergonomic_value[i]])

        results_df = pd.DataFrame(results,
                                  columns=["task_id", "station", "worker", "execution_time", "ergonomic_value"])
        results_df["predecessors"] = results_df["task_id"].apply(
            lambda i: df.loc[df["task_id"] == i, "predecessors"].values[0])

        results_df.to_csv("task_assignment_results.csv", index=False)
        print(results_df)


    elif m.status == GRB.INFEASIBLE:
        print("⚠️ The model is infeasible. No solution found.")
        m.computeIIS()
        m.write("infeasible.ilp")  # For the debug

    else:
        print(f"⚠️ Optimization completed with status: {m.status}")

    if m.status == GRB.OPTIMAL or m.SolCount > 0:

        df_new = df.copy()

        # Update the column "previous_station"
        new_stations = {}
        for i in tasks:
            for s in stations:
                if z[i, s].X > 0.5:  # task i assegnato a station s
                    new_stations[i] = s
                    break

        # Apply the new updated column
        df_new["previous_station"] = df_new["task_id"].map(new_stations)

        output_folder = os.path.join(os.path.dirname(file), "opt_instances")
        os.makedirs(output_folder, exist_ok=True)

        base_name = os.path.basename(file)
        match = re.search(r"data_(\d+tasks_\d+)\.csv", base_name)
        if match:
            new_name = f"opt_{match.group(1)}.csv"
        else:
            new_name = f"opt_{base_name}"  # fallback se non matcha

        output_path = os.path.join(output_folder, new_name)

        # Save the updated file
        df_new.to_csv(output_path, index=False)
        print(f" Instance save us: {output_path}")

    else:
        print("⚠️ No optimal solution found — no files saved.")


'''
if __name__ == "__main__":

    """
        Main entry point of the script.

        This script:
        - Collects all instance files from the 'instances34' directory
        - Runs each instance in parallel using all available CPU cores
        - Handles and reports errors from parallel processes
    """

    # Get the absolute path of the directory containing this script
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Path to the directory containing the instance files
    instances_dir = os.path.join(script_dir, "instances20")

    # Collect all files in the instances directory (ignore subdirectories)
    instances = [os.path.join(instances_dir, f) for f in os.listdir(instances_dir) if os.path.isfile(os.path.join(instances_dir, f))]

    # Detect the number of available CPU cores
    num_cores = cpu_count()

    # Run instances in parallel using a multiprocessing pool
    with Pool(processes=num_cores) as pool:
        # Launch each instance asynchronously
        results = [pool.apply_async(run_instance, (file,)) for file in instances]

        outputs = []
        for r in results:
            try:
                outputs.append(r.get())
            except Exception as e:
                print(f"⚠️ Error in a process: {e}")

    print("\nAll instances have finished.")
'''

if __name__ == "__main__":

    script_dir = os.path.dirname(os.path.abspath(__file__))
    instances_dir = os.path.join(script_dir, "instances20")
    print("Sto leggendo da:", instances_dir)

    # instances = [os.path.join(instances_dir, f) for f in os.listdir(instances_dir) if os.path.isfile(os.path.join(instances_dir, f))]

    # subset = instances[0:9]

    target_instance_name = "data_20tasks_002.csv"
    target_instance = os.path.join(instances_dir, target_instance_name)
    instances = [target_instance]

    print("Running the instance:")
    for i in instances:
        print(" -", os.path.basename(i))

    run_instance(target_instance)