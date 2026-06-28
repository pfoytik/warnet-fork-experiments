# Example: Bitcoin Soft Fork Experiment

This experiment replicates the parameter space from Foytik (2026),
"Quantifying Bitcoin Network Resilience Through Critical Scenario Discovery."

It models Bitcoin v27 (new consensus rules) competing against v26 (legacy rules)
across a grid of economic conditions and miner commitment levels.

## Parameters

| Parameter | Value | Meaning |
|---|---|---|
| `economic_split` | 0.55 – 0.78 | Fraction of BTC custody initially on Fork A |
| `pool_committed_split` | 0.10 – 0.50 | Fraction of hashrate ideologically committed to Fork A |
| `duration` | 13000s | ~3.6 hours sim time (covers 2016-block retarget) |
| `retarget_interval` | 2016 | Standard Bitcoin difficulty adjustment period |

## Running a Single Scenario

```bash
# From the repo root:
warnet run scenarios/fork_experiment.py \
    --fork-a-name "v27-new-rules" \
    --fork-b-name "v26-legacy" \
    --fork-a-version "27." \
    --fork-b-version "26." \
    --fork-a-economic 78 \
    --duration 13000 \
    --enable-difficulty \
    --retarget-interval 2016 \
    --network networks/lite/network.yaml
```

## Running the Full Grid Sweep

```bash
# 1. Generate scenarios
python tools/generate_scenarios.py \
    --economic-splits 0.55 0.65 0.74 0.78 \
    --committed-splits 0.10 0.15 0.25 0.30 0.40 0.50 \
    --output-dir experiments/bitcoin-soft-fork/runs/

# 2. Build network configs
python tools/build_configs.py \
    --input experiments/bitcoin-soft-fork/runs/scenarios.json \
    --base-network lite

# 3. Run sweep (adjust namespace and scenario range for your cluster)
python tools/run_sweep.py \
    --input experiments/bitcoin-soft-fork/runs/build_manifest.json \
    --results-dir experiments/bitcoin-soft-fork/runs/results/ \
    --duration 13000 --retarget-interval 2016 --interval 2 \
    --namespace my-fork-experiment

# 4. Analyze
python tools/analyze_results.py \
    --results-dir experiments/bitcoin-soft-fork/runs/results/ \
    --output-dir experiments/bitcoin-soft-fork/runs/analysis/
```

## Key Findings (from Foytik 2026)

- v27 wins when economic_split > 0.74 regardless of mining behavior
- Below 0.50, v27 fails structurally — no amount of miner support is sufficient
- In the contested zone (0.50–0.74), pool committed hashrate and pool identity determine the winner
- A single large pool (Foundry, 27%) committed to v27 provides a stronger cascade signal than
  equivalent smaller pools combined — because it fires the 2016-block retarget faster

See the [research companion repo](https://github.com/pfoytik/bitcoin-fork-governance-study) for all 21 findings and the full results database.
