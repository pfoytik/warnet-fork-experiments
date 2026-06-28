# warnet-fork-experiments

A framework for running contested Bitcoin protocol fork experiments using [Warnet](https://github.com/bitcoin-dev-tools/warnet).

Model two competing versions of `bitcoind`, partition them, and watch mining pools, exchanges, merchants, and users make independent economically-motivated decisions about which fork to support.

---

## What It Does

- Runs real `bitcoind` nodes on Kubernetes via Warnet — no simulation shortcuts
- Mining pools switch forks based on profitability + ideology thresholds
- Economic nodes (exchanges, merchants) migrate based on relative BTC price
- The 2016-block difficulty retarget is the key cascade trigger:
  the first fork to accumulate 2016 blocks fires a difficulty adjustment
  that makes its blocks 2–3× more profitable, cascading uncommitted pools
- Supports single runs, parameter grid sweeps, and Latin Hypercube Sampling

## Quick Start

### Prerequisites

- Python 3.10+
- [Warnet](https://github.com/bitcoin-dev-tools/warnet) installed and connected to a Kubernetes cluster
- `pip install numpy scipy scikit-learn pandas pyyaml matplotlib`

### Run a single fork experiment

```bash
# Clone this repo
git clone https://github.com/pfoytik/warnet-fork-experiments
cd warnet-fork-experiments

# Deploy a lite network (25 nodes)
warnet deploy networks/lite/network.yaml --namespace my-fork

# Run the scenario (Fork A = Bitcoin v27 new rules, Fork B = v26 legacy)
warnet run scenarios/fork_experiment.py \
    --fork-a-name "new-rules" \
    --fork-b-name "legacy" \
    --fork-a-version "27." \
    --fork-b-version "26." \
    --fork-a-economic 74 \
    --enable-difficulty \
    --retarget-interval 2016 \
    --duration 13000
```

### Run your own fork experiment

Adapt to any two Bitcoin versions:

```bash
warnet run scenarios/fork_experiment.py \
    --fork-a-name "taproot-plus" \
    --fork-b-name "legacy" \
    --fork-a-version "28." \
    --fork-b-version "27." \
    --fork-a-economic 65 \
    --duration 13000
```

### Run a parameter sweep

```bash
# Generate a grid of scenarios
python tools/generate_scenarios.py \
    --economic-splits 0.55 0.65 0.74 0.78 \
    --committed-splits 0.10 0.25 0.40 \
    --output-dir my_sweep/

# Build Warnet configs
python tools/build_configs.py \
    --input my_sweep/scenarios.json \
    --base-network lite

# Run on Kubernetes
python tools/run_sweep.py \
    --input my_sweep/build_manifest.json \
    --results-dir my_sweep/results/ \
    --duration 13000 --retarget-interval 2016 \
    --namespace my-sweep

# Analyze
python tools/analyze_results.py \
    --results-dir my_sweep/results/ \
    --output-dir my_sweep/analysis/
```

---

## Repository Structure

```
warnet-fork-experiments/
│
├── README.md                    ← You are here
│
├── scenarios/                   ← The fork experiment scenario
│   ├── fork_experiment.py       ← Main scenario script
│   ├── lib/                     ← Price, fee, difficulty, and strategy oracles
│   └── config/                  ← Pool and economic node configuration
│
├── networks/                    ← Bitcoin network topologies
│   ├── lite/                    ← 25-node network (fast, for testing)
│   └── full/                    ← 60-node network (realistic, research-quality)
│
├── experiments/                 ← Ready-to-run experiment examples
│   └── bitcoin-soft-fork/       ← The v26/v27 soft fork from Foytik (2026)
│
├── tools/                       ← Sweep and analysis pipeline
│   ├── generate_scenarios.py    ← Create scenario parameter sets
│   ├── build_configs.py         ← Build Warnet configs from scenarios
│   ├── run_sweep.py             ← Run scenarios on Kubernetes
│   └── analyze_results.py       ← Parse results and compute outcomes
│
└── docs/
    ├── how_it_works.md          ← Conceptual explanation of the model
    ├── parameters.md            ← All CLI parameters for fork_experiment.py
    └── network_design.md        ← How to design custom network topologies
```

---

## Key Experimental Parameters

| Parameter | What It Controls | Research Finding |
|---|---|---|
| `--fork-a-economic` | Fraction of BTC custody on Fork A (0–100%) | **Dominant predictor** — above 74%, Fork A wins; below 50%, Fork A fails |
| Pool `committed_split` | Fraction of hashrate ideologically committed to Fork A | Decisive within the 50–74% contested zone |
| Pool `max_loss_pct` | How much profit loss a committed pool will tolerate | Gates the cascade timing and magnitude |
| `--retarget-interval` | 2016 (standard) or 144 (fast) | Changes which parameter dominates: pool commitment at 2016, economic at 144 |

See [docs/parameters.md](docs/parameters.md) for the full parameter reference.

---

## Included Example

[`experiments/bitcoin-soft-fork/`](experiments/bitcoin-soft-fork/) replicates the
parameter sweep from Foytik (2026). Results and all 21 findings are published in the
[research companion repo](https://github.com/pfoytik/bitcoin-fork-governance-study).

---

## Extending the Framework

**New pool configurations:** Edit `scenarios/config/mining_pools_config.yaml` — add
a new scenario block with your pool distribution and ideology assignments.

**New network topologies:** Copy `networks/lite/network.yaml`, adjust node counts and
roles. See [docs/network_design.md](docs/network_design.md).

**New sweep designs:** Use `tools/generate_scenarios.py --help`. Supports grid,
Latin Hypercube, and targeted sweep types.

---

## Citation

If you use this framework in research, please cite:

```bibtex
@inproceedings{foytik2026bitcoin,
  title     = {Quantifying Bitcoin Network Resilience Through Critical Scenario Discovery},
  author    = {Foytik, Peter},
  booktitle = {Proceedings of the University of Wyoming Bitcoin Research Initiative Workshop},
  year      = {2026},
  note      = {Framework: https://github.com/pfoytik/warnet-fork-experiments}
}
```

---

## License

MIT — see [LICENSE](LICENSE)

## Acknowledgments

Built on [Warnet](https://github.com/bitcoin-dev-tools/warnet) by the Bitcoin Dev Tools team.
