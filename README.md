
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
ALRBP/
│
├── README.md
├── requirements.txt
├── LICENSE
│
├── data/
│   ├── synthetic instances/
│   │   ├── 10_tasks/
│   │   ├── 15_tasks/
│   │   ├── ...
│   │   └── 40_tasks/
│   │
│   └── feasible_instances/
│       ├── 10_tasks/
│       ├── 15_tasks/
│       ├── ...
│       └── 40_tasks/
│
├── src/
│   ├── model/
│   │   ├── alb_model.py
│   │   ├── rebalancing_model.py
│   │   └── fairness_metrics.py
│   │
│   ├── experiments/
│   │   ├── run_balancing.py
│   │   ├── run_rebalancing.py
│   │   └── compute_nadir_utopia.py
│   │
│   └── analysis/
│       ├── aggregate_results.py
│       ├── compute_fairness_tables.py
│       └── plots.py
│
├── results/
│   ├── tables/
│   │   ├── table_fairness.tex
│   │   └── table_robustness.tex
│   └── logs/
│
└── scripts/
    ├── reproduce_main_results.sh
    └── reproduce_tables.sh

<br>

## Getting Started

<br>

## Licence

<br>
