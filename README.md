
<br>
<h1 align="center">A Multi-Worker Assembly Line Rebalancing with Spatial and Ergonomic Considerations</h1>
<br>

<p align="center">
  Martina Vinetti, Sabino Franceso Roselli, and Martin Fabian.
</p>
<br>

This repository contains the code and experimental data supporting two research contributions on multi-worker assembly line rebalancing:

- **A Multi-Worker Assembly Line Rebalancing with Spatial and Ergonomic Considerations** (IFAC 2026)
- **Multi-Worker Assembly Line Rebalancing with Relevance-Guided Configuration Preservation** (IEEE CASE 2026)

It includes the Python implementations of the proposed optimization models, the procedures used to generate and extend the benchmark instances, and the scripts required to reproduce the computational experiments reported in both papers. The material associated with each publication is organized in a dedicated directory.

<br>

### Research Overview

This repository investigates the Assembly Line Rebalancing Problem in manual assembly systems where multiple workers can operate in parallel within the same station. The proposed approaches integrate task reassignment, workload and ergonomic balance, and explicit spatial feasibility through work-area constraints. Particular attention is also devoted to preserving relevant elements of the existing assembly configuration during rebalancing.

### IFAC 2026

The first contribution proposes a multi-objective optimization framework that jointly addresses task reassignment, worker allocation, workload balance, ergonomic conditions, and spatial feasibility. Linear and quadratic formulations of the work-area constraints are introduced and computationally compared. Experiments on synthetic instances show that the proposed models can generate feasible and human-centered configurations under different cycle-time conditions.

### IEEE CASE 2026

The second contribution introduces a relevance-guided approach to configuration preservation. Rather than treating all tasks uniformly, the proposed MSF-p metric prioritizes tasks according to their processing time, ergonomic load, and structural importance in the precedence graph. The resulting optimization model balances workload and ergonomic conditions while preserving the most relevant task groupings. Computational experiments on extended benchmark instances evaluate solution quality, robustness, formulation performance, and the effect of the pruning parameter.

<br>

## Repository Structure

<pre>
ALRBP/
├── README.md                              # Project overview + steps to reproduce experiments
├── requirements.txt                       # Python dependencies (incl. gurobipy)
├── LICENSE                                # License information
│
│
├── IFAC2026/
│   ├── README.md                          # Description of the IFAC 2026 paper
│   │
│   ├── data/
│   │   ├── synthetic_instances/           # Generated synthetic instances
│   │   ├── balanced_instances/            # Feasible ALBP solutions used as existing task allocations
│   │   │   ├── opt/                       # Instances solved to optimality
│   │   │   └── subopt/                    # Instances solved up to the target MIP gap (e.g., 80%)
│   │   └── norm_values/                   # Nadir/utopia values used to normalize the objectives
│   │
│   └── src/
│       ├── models/
│       │   ├── Q_ALRBP_model.py           # Rebalancing model with quadratic work-area constraints
│       │   └── L_ALRBP_model.py           # Rebalancing model with linearized work-area constraints
│       │
│       └── scripts/
│           ├── run_ALBP.py                # Runs ALBP on synthetic instances → balanced_instances/(opt|subopt)
│           ├── compute_norm_values.py     # Computes normalization values → data/norm_values/
│           └── run_ALRBP.py               # Runs both ALRBP formulations on the balanced instances
│
│
└── CASE2026/
    ├── README.md                          # Description of the IEEE CASE 2026 paper
    │
    ├── data/
    │   ├── generated_extended_instances/  # Benchmark instances extended with spatial and ergonomic attributes
    │   ├── balanced_instances/            # Initial balanced configurations used as inputs for rebalancing
    │   └── experimental_parameters/       # Experimental settings, including cycle times, K values, and solver parameters
    │
    ├── src/
    │   └── scripts/
    │       ├── instance_extension.py      # Extends the original benchmark datasets with the required attributes
    │       ├── run_balancing.py           # Generates the initial balanced configurations
    │       └── run_rebalancing.py         # Solves the proposed multi-worker rebalancing problem
    │
    └── results/                           # Results reported in the IEEE CASE 2026 paper                    




</pre>
## Getting Started

### Prerequisites

1. **Python** (version 3.11 or higher)
2. Install required dependencies using:

```bash
pip install -r requirements.txt
```

3. Solver:
    - [Gurobi Optimizer](https://www.gurobi.com)

### Running the Code

run_ALRBP.py (or run_rebalancing.py) – loads feasible instances and solves the Assembly Line Rebalancing Problem.

<br>

## Licence

This project is licensed under the MIT License. See `LICENSE` for details.
