import gurobipy as gp
from gurobipy import GRB

"""
Linear ALRBP model.

This module implements a linear formulation of the Assembly Line Rebalancing Problem (ALRBP)
with workload fairness, ergonomic fairness, and similarity preservation objectives.
The workstation–area compatibility constraint is expressed using a linearized formulation,
resulting in a mixed-integer linear program.
"""


def build_l_alrbp_model(
    tasks, stations, workers,
    task_times, ergonomic_value, task_area, predecessors, TIB,
    cycle_time,
    min_workers_per_station, max_workers_per_station,
    norm_values,
    weights=(1/3, 1/3, 1/3),
):
    """
    Builds the *linear* ALRBP model.
    Difference vs quadratic: the workstation-area compatibility constraint is enforced
    with a linear formulation (no bilinear products), using an 'area_choice' variable
    per worker and a conditional relaxation when the worker is assigned to a shared station.
    Returns (model, ctx) where ctx contains vars and expressions needed post-solve.
    """

    m = gp.Model("assembly_line_rebalancing_linear")

    # ---------------- VARIABLES ----------------
    x = m.addVars(tasks, workers, vtype=GRB.BINARY, name="x")      # task->worker
    y = m.addVars(stations, workers, vtype=GRB.BINARY, name="y")   # worker->station
    z = m.addVars(tasks, stations, vtype=GRB.BINARY, name="z")     # task->station

    workload = m.addVars(workers, vtype=GRB.CONTINUOUS, name="workload")
    ergonomic_load = m.addVars(workers, vtype=GRB.CONTINUOUS, name="ergonomic_load")

    max_time = m.addVar(vtype=GRB.CONTINUOUS, name="max_time")
    min_time = m.addVar(vtype=GRB.CONTINUOUS, name="min_time")
    min_erg = m.addVar(vtype=GRB.CONTINUOUS, name="min_ergonomic_load")
    max_erg = m.addVar(vtype=GRB.CONTINUOUS, name="max_ergonomic_load")

    w_balance = m.addVar(name="workload_balance")
    erg_balance = m.addVar(name="ergonomic_balance")

    # area selector per worker (0=external, 1=internal)
    area_choice = m.addVars(workers, vtype=GRB.BINARY, name="area_choice")

    # ---------------- CONSTRAINTS ----------------
    for i in tasks:
        m.addConstr(gp.quicksum(x[i, w] for w in workers) == 1, name=f"assign_once_t{i}")
        m.addConstr(gp.quicksum(z[i, s] for s in stations) == 1, name=f"assign_station_once_t{i}")

    for w in workers:
        m.addConstr(gp.quicksum(y[s, w] for s in stations) <= 1, name=f"worker_one_station_w{w}")

    for w in workers:
        m.addConstr(workload[w] == gp.quicksum(task_times[i] * x[i, w] for i in tasks), name=f"workload_def_w{w}")
        m.addConstr(ergonomic_load[w] == gp.quicksum(ergonomic_value[i] * x[i, w] for i in tasks),
                    name=f"ergonomic_def_w{w}")

        m.addConstr(workload[w] <= max_time, name=f"max_workload_w{w}")
        m.addConstr(workload[w] >= min_time, name=f"min_workload_w{w}")
        m.addConstr(ergonomic_load[w] <= max_erg, name=f"max_erg_w{w}")
        m.addConstr(ergonomic_load[w] >= min_erg, name=f"min_erg_w{w}")

        m.addConstr(workload[w] <= cycle_time, name=f"workload_limit_w{w}")

    # consistency task-worker-station
    for i in tasks:
        for s in stations:
            for w in workers:
                m.addConstr(x[i, w] <= y[s, w] + (1 - z[i, s]), name=f"consistency_{i}_{s}_{w}")

    # workers per station bounds
    for s in stations:
        m.addConstr(gp.quicksum(y[s, w] for w in workers) >= min_workers_per_station, name=f"min_workers_station_{s}")
        m.addConstr(gp.quicksum(y[s, w] for w in workers) <= max_workers_per_station, name=f"max_workers_station_{s}")

    # precedence constraints
    for j in tasks:
        for i in predecessors.get(j, []):
            for s1 in stations:
                for s2 in stations:
                    if s1 > s2:
                        m.addConstr(z[i, s1] + z[j, s2] <= 1, name=f"precedence_illegal_{i}_{j}_s{s1}_s{s2}")

    # shared station detection
    shared = m.addVars(stations, vtype=GRB.BINARY, name="shared")
    for s in stations:
        m.addConstr(gp.quicksum(y[s, w] for w in workers) >= 2 * shared[s])
        m.addConstr(gp.quicksum(y[s, w] for w in workers) <= 1 + (len(workers) - 1) * shared[s])

    worker_shared = m.addVars(workers, vtype=GRB.BINARY, name="worker_shared")
    for w in workers:
        # worker_shared[w]=1 if worker w assigned to any shared station
        m.addConstr(worker_shared[w] >= gp.quicksum(shared[s] * y[s, w] for s in stations) / len(stations))

    # ---------------- LINEAR area compatibility constraint ----------------
    # If worker_shared[w] == 0 => worker must take tasks of a single area (all 0 or all 1).
    # If worker_shared[w] == 1 => constraint relaxed.
    #
    # Using big-M: x[i,w] <= allowed + M*worker_shared[w]
    # For area=1 tasks: allowed = area_choice[w]
    # For area=0 tasks: allowed = 1 - area_choice[w]
    #
    # Choose M = 1 (tight), because x is binary.
    M = 1.0
    for w in workers:
        for i in tasks:
            ai = task_area[i]  # 0 or 1
            if ai == 1:
                m.addConstr(x[i, w] <= area_choice[w] + M * worker_shared[w],
                            name=f"area_int_{i}_{w}")
            else:
                m.addConstr(x[i, w] <= (1 - area_choice[w]) + M * worker_shared[w],
                            name=f"area_ext_{i}_{w}")

    # objective terms
    m.addConstr(w_balance == max_time - min_time, name="workload_balance_def")
    m.addConstr(erg_balance == max_erg - min_erg, name="ergonomic_balance_def")

    # ---- MSF (linearized with auxiliary w[i,j,s]) ----
    w_aux = {}
    SF_exprs = []
    for i in tasks:
        TIB_i = TIB.get(i, [])
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

    MSF_expr = gp.quicksum(SF_exprs) / len(tasks)

    # Normalize (nadir-utopia)
    f1_expr = gp.LinExpr(w_balance)        # min
    f2_expr = gp.LinExpr(erg_balance)      # min
    f3_expr = gp.LinExpr(MSF_expr)         # max

    f1_min, f1_max = norm_values["f1_min"], norm_values["f1_max"]
    f2_min, f2_max = norm_values["f2_min"], norm_values["f2_max"]
    f3_min, f3_max = norm_values["f3_min"], norm_values["f3_max"]

    eps = 1e-6
    f1_norm = (f1_expr - f1_min) / max(f1_max - f1_min, eps)
    f2_norm = (f2_expr - f2_min) / max(f2_max - f2_min, eps)
    f3_norm = (f3_max - f3_expr) / max(f3_max - f3_min, eps)  # max -> min

    alpha, beta, gamma = weights
    m.setObjective(alpha * f1_norm + beta * f2_norm + gamma * f3_norm, GRB.MINIMIZE)

    ctx = {
        "x": x, "y": y, "z": z,
        "workload": workload, "ergonomic_load": ergonomic_load,
        "w_balance": w_balance, "erg_balance": erg_balance,
        "MSF_expr": MSF_expr,
        "f_expr": (f1_expr, f2_expr, f3_expr),
        "f_norm": (f1_norm, f2_norm, f3_norm),
        "area_choice": area_choice,
        "worker_shared": worker_shared,
    }
    return m, ctx
