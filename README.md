
<br>
<h1 align="center">A Multi-Worker Assembly Line Rebalancing with Spatial and Ergonomic Considerations</h1>
<br>

<p align="center">
  Martina Vinetti, Sabino Franceso Roselli, and Martin Fabian.
</p>
<br>

This is the official repository for **A Multi-Worker Assembly Line Rebalancing with Spatial and Ergonomic Considerations**. This repository contains
- XXXX.
- YYYY.
- ZZZZ.

<br>

## Abstract
_This work addresses the Assembly Line Rebalancing Problem in manual assembly systems where multiple workers operate in parallel within the same station—an industrially relevant scenario that remains insufficiently explored in the literature. A multi-objective optimization model is proposed that incorporates task reassignment, worker allocation, ergonomic evaluation, and explicit spatial feasibility through work-area constraints. The formulation minimizes deviations from the current configuration while promoting balanced workload and ergonomic conditions among workers._

_Computational experiments on synthetic problem instances demonstrate that the model consistently generates feasible and human-centered reconfigurations across varying cycle-time conditions, highlighting its potential as a decision-support tool for industrial rebalancing in flexible production environments._

<br>

## Repository Structure

<pre>
ALRBP/
├── README.md                        # Project overview + steps to reproduce experiments
├── requirements.txt                 # Python dependencies (incl. gurobipy)
├── LICENSE                          # License information
│
├── data/
│   ├── synthetic_instances/         # Randomly generated (potentially infeasible) instances
│   │
│   ├── feasible_instances/
│   │   ├── opt/                     # Feasible incumbents from ALBP solved to optimality
│   │   └── subopt/                  # Feasible incumbents from ALBP with target MIPGap (e.g., 80%)
│   │
│   └── norm_values/                 # Nadir/utopia (or normalization) values used to scale objectives
│
├── src/
│   ├── models/
│   │   ├── Q_ALRBP_model.py              # Rebalancing model (quadratic area constraint)
│   │   └── L_ALRBP_model.py              # Rebalancing model (linearized area constraint)
│   │
│   └── scripts/
│       ├── run_ALBP.py                   # Runs ALBP on synthetic instances -> feasible_instances/(opt|subopt)
│       ├── norm_values.py                # Computes normalization values -> data/norm_values/
│       └──  run_ALRBP.py                 # Runs ALRBP (both quadratic and linear) on feasible instances   
│
└── results/
    ├── rebalancing/                       # Rebalancing results
    │
    ├── tables/                            # Final LaTeX-ready tables for the paper
    │   ├── table_fairness_main.tex
    │   └── table_fairness_robustness.tex
    │
    └── plots/                             # Quadratic vs Linear plot
        └── cactus_plot/


</pre>
## Getting Started

<br>

## Licence

<br>
