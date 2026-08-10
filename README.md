
<br>
<h1 align="center">A Multi-Worker Assembly Line Rebalancing with Spatial and Ergonomic Considerations</h1>
<br>

<p align="center">
  Martina Vinetti, Sabino Franceso Roselli, and Martin Fabian.
</p>
<br>

This is the official repository for **A Multi-Worker Assembly Line Rebalancing with Spatial and Ergonomic Considerations**.  
It contains the Python implementations of the proposed linear and quadratic ALRBP models, the procedures to generate feasible incumbent solutions, and the scripts required to reproduce all experimental results reported in the paper.

<br>

## Abstract
_This work addresses the Assembly Line Rebalancing Problem in manual assembly systems where multiple workers operate in parallel within the same station—an industrially relevant scenario that remains insufficiently explored in the literature. A multi-objective optimization model is proposed that incorporates task reassignment, worker allocation, ergonomic evaluation, and explicit spatial feasibility through work-area constraints. The formulation minimizes deviations from the current configuration while promoting balanced workload and ergonomic conditions among workers._

_Computational experiments on synthetic problem instances demonstrate that the model consistently generates feasible and human-centered reconfigurations across varying cycle-time conditions, highlighting its potential as a decision-support tool for industrial rebalancing in flexible production environments._

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

run_ALRBP.py – loads feasible instances and solves the Assembly Line Rebalancing Problem using either the linear or quadratic formulation.

<br>

## Licence

This project is licensed under the MIT License. See `LICENSE` for details.
