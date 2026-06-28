# How the Fork Experiment Works

## Overview

The fork experiment models a contested protocol change in a Bitcoin-like network.
Two versions of bitcoind run simultaneously, partitioned from each other by Kubernetes
network policy. Actors (mining pools, exchanges, merchants, users) make independent
economically-motivated decisions about which fork to support.

## The Two Forks

| | Fork A | Fork B |
|---|---|---|
| Represents | New consensus rules | Old/legacy rules |
| Initial economic weight | Set by `economic_split` parameter | Remainder |
| Node type | New bitcoind version | Old bitcoind version |
| Rule enforcement | Strict (rejects non-compliant blocks) | Permissive (accepts Fork A blocks) |

Fork A and Fork B are network-partitioned from the start: nodes cannot communicate
across the partition boundary. When a pool mines a block on Fork A, that block is
submitted to a bridge node on the Fork B side via `submitblock` RPC — modeling how
blocks propagate across the internet even without direct P2P connections.

## Actor Types

### Mining Pools

Each pool has three behavioral parameters:

| Parameter | Description |
|---|---|
| `fork_preference` | `fork_a`, `fork_b`, or `neutral` — ideology |
| `max_loss_pct` | Maximum tolerated profit loss before switching forks |
| `ideology_strength` | How strongly they stick to their preferred fork |

Pools update their fork choice every `--hashrate-update-interval` seconds by comparing
their expected profitability on each fork. Neutral pools switch purely on profit.
Committed pools accept losses up to `max_loss_pct` before capitulating.

### Economic Nodes (Exchanges, Merchants, Institutions)

Economic nodes hold BTC on one fork and can migrate. Their decisions are based on:
- The current price ratio between forks (from the Price Oracle)
- Their own ideology (preference for Fork A or Fork B)
- An inertia parameter (switching cost)

Economic nodes control `economic_split` — the fraction of total BTC custody on Fork A.
This is the dominant long-run outcome predictor.

### User Nodes

Similar to economic nodes but with smaller economic weight per node and slightly
different ideology + switching parameters. Models retail/individual users.

## The Price Oracle

The price of each fork's BTC is computed from a formula that accounts for:
- Economic weight (custody fraction) on each fork
- Transactional activity (fee-generating merchants and exchanges)
- Block production rate (liveness)
- A configurable maximum divergence cap (default ±10%)

Price feeds back into pool and economic node decisions at each update interval.

## The 2016-Block Retarget Cascade

This is the central mechanism the experiment is designed to study.

Bitcoin adjusts mining difficulty every 2016 blocks based on how long those blocks
took to produce. If Fork A accumulates 2016 blocks faster than Fork B:

1. Fork A fires a difficulty adjustment: `new_difficulty = target_time / actual_time`
2. If Fork A is mining faster than the target rate, difficulty *increases* — but if
   Fork A is attracting more hashrate while Fork B is lagging, Fork B's 2016 blocks
   take longer → Fork B's next retarget will *decrease* its difficulty
3. The side that fires the retarget first gets 2–3× more profitable blocks immediately
4. Committed pools on the losing side face losses exceeding `max_loss_pct` → cascade switch
5. All remaining neutral pools follow the price signal

This "retarget race" is why pool composition identity matters, not just aggregate hashrate:
a single large pool committed to Fork A depletes Fork B's hashrate more aggressively,
causing Fork B's blocks to slow down, causing the retarget differential.

## Outcome Classification

At the end of each scenario, the outcome is classified:

| Outcome | Definition |
|---|---|
| `fork_a_dominant` | Fork A controls ≥80% of hashrate and ≥70% of economic weight |
| `fork_b_dominant` | Fork B controls ≥80% of hashrate and ≥70% of economic weight |
| `contested` | Neither fork achieves dominance thresholds |
| `stalemate` | Negligible movement in either direction |

## Parameters That Matter

From Foytik (2026), tested across 2,694 scenarios:

| Parameter | Impact | Direction |
|---|---|---|
| `economic_split` | High (60% feature importance) | Above 0.74 → Fork A wins; below 0.50 → Fork A fails |
| `pool_committed_split` | Moderate (16.6%) | Above ~0.25–0.30 within contested zone → Fork A wins |
| `pool_max_loss_pct × ideology_strength` | Moderate (23%) | High values favor incumbent (Fork B) |
| `pool_neutral_pct` | None (cascade intensity only) | — |
| `hashrate_split` | None | — |
| User/economic node inertia | None | — |

See [../experiments/bitcoin-soft-fork/README.md](../experiments/bitcoin-soft-fork/README.md)
for how to reproduce the full parameter sweep.
