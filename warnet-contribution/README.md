# fork_partition.py — Warnet Contribution

A self-contained Warnet scenario that models a contested Bitcoin protocol fork.
No external libraries required beyond Warnet's `Commander`.

---

## What It Does

Two versions of `bitcoind` are network-partitioned from each other. Mining pools
hold ideology (committed to Fork A, committed to Fork B, or neutral) and make
independent profit-driven decisions about which fork to mine. The 2016-block
difficulty retarget is the key cascade trigger.

**Cascade mechanism:**
1. Fork A starts with a committed minority of hashrate (e.g., 27%)
2. Fork A mines slowly → accumulates 2016 blocks in many ticks → difficulty drops sharply
3. Lower difficulty makes Fork A blocks far more profitable per hashrate unit
4. Neutral pools cascade to Fork A; committed Fork B pools follow if losses exceed threshold
5. Fork A reaches hashrate dominance (or fails if economic support is insufficient)

**Profitability formula** (per unit of pool hashrate):
```
profit[fork] = price[fork] / difficulty[fork]
```
Pool hashrate and block reward cancel when comparing forks — only price and
difficulty drive the decision. Price is set by the economic weight parameter.

---

## Files

| File | Purpose |
|---|---|
| `fork_partition.py` | Self-contained scenario script |
| `pool_config_example.yaml` | Example pool configuration with Foundry committed to Fork A |
| `README.md` | This file |

---

## Quick Start

```bash
# Run with defaults (all pools neutral, 70% economic weight on Fork A)
warnet run fork_partition.py

# Foundry committed to Fork A, contested economic conditions
warnet run fork_partition.py \
    --fork-a-economic 65 \
    --pool-committed 0.27 \
    --retarget-interval 2016 \
    --duration 7200

# Custom pool ideology via config file
warnet run fork_partition.py \
    --fork-a-economic 78 \
    --pool-config pool_config_example.yaml \
    --retarget-interval 2016 \
    --duration 7200

# Fast test with short retarget
warnet run fork_partition.py \
    --fork-a-economic 74 \
    --pool-committed 0.30 \
    --retarget-interval 144 \
    --duration 3600 \
    --interval 0.5
```

---

## Parameters

| Argument | Default | Description |
|---|---|---|
| `--fork-a-version` | `27.` | bitcoind subversion prefix identifying Fork A nodes |
| `--fork-b-version` | `26.` | bitcoind subversion prefix identifying Fork B nodes |
| `--fork-a-economic` | `70` | Initial economic weight on Fork A (%) |
| `--max-price-divergence` | `0.10` | Maximum price ratio between forks (±10%) |
| `--pool-committed` | `0.0` | Fraction of hashrate pre-committed to Fork A |
| `--pool-max-loss` | `0.26` | Loss tolerance for committed pools before switching |
| `--pool-config` | — | Path to YAML file overriding pool assignments |
| `--duration` | `7200` | Scenario duration (seconds) |
| `--interval` | `1.0` | Tick interval (seconds); lower = faster simulation |
| `--retarget-interval` | `2016` | Blocks per difficulty epoch |
| `--pool-update-ticks` | `600` | Ticks between pool fork-choice evaluations |

---

## Network Requirements

Your network needs two groups of nodes running different Bitcoin versions:

- **Fork A nodes:** running e.g. `bitcoin/bitcoin:27.0`
- **Fork B nodes:** running e.g. `bitcoin/bitcoin:26.0`

The scenario auto-detects which nodes belong to which fork by matching the
`--fork-a-version` / `--fork-b-version` string against each node's
`getnetworkinfo()` subversion. Use `--fork-a-version "28."` to model a
different version pair.

---

## Outcomes

| Outcome | Definition |
|---|---|
| `fork_a_dominant` | Fork A holds ≥80% of hashrate |
| `fork_b_dominant` | Fork B holds ≥80% of hashrate |
| `stalemate` | Neither fork moves beyond 40–60% |
| `contested` | One fork leads but hasn't reached 80% threshold |

---

## Research Context

This scenario is extracted from a larger fork experiment framework developed
for the paper "Quantifying Bitcoin Network Resilience Through Critical Scenario
Discovery" (Foytik, UW Bitcoin Research Initiative, July 2026).

Key findings from 2,694 simulations:
- Below ~50% economic weight, Fork A fails regardless of mining support
- Above ~82%, Fork A wins regardless of mining support
- In the contested zone (50–82%), the retarget cascade and pool identity determine the winner
- A single large pool committed to Fork A (e.g., Foundry, 27%) provides a stronger
  cascade signal than multiple smaller pools at the same aggregate hashrate

The full framework with parameter sweeps, oracle library, and results database:
https://github.com/pfoytik/warnet-fork-experiments
