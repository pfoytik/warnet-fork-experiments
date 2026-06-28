# Parameter Reference

All parameters for `scenarios/fork_experiment.py`.

## Fork Identity

| Argument | Default | Description |
|---|---|---|
| `--fork-a-name` | `fork_a` | Display name for Fork A (e.g., `"taproot"`, `"v2"`) |
| `--fork-b-name` | `fork_b` | Display name for Fork B (e.g., `"legacy"`, `"v1"`) |
| `--fork-a-version` | `27.` | Version prefix in bitcoind subversion string for Fork A nodes |
| `--fork-b-version` | `26.` | Version prefix in bitcoind subversion string for Fork B nodes |

## Economic Conditions

| Argument | Default | Description |
|---|---|---|
| `--fork-a-economic` | `70.0` | Initial economic weight on Fork A (0–100%) |
| `--fork-b-economic` | auto | Derived as `100 - fork_a_economic` if not set |
| `--max-price-divergence` | `0.10` | Maximum price ratio between forks (e.g., 0.10 = ±10%) |
| `--enable-liveness-penalty` | off | Decay economic factor for chains not producing blocks |
| `--use-economic-ema` | off | Apply EMA lag to economic weight updates |
| `--economic-ema-alpha` | `0.15` | EMA smoothing factor (0=no update, 1=no lag) |
| `--use-sigmoid` | off | Use sigmoid (vs linear) mapping for economic factor |
| `--sigmoid-steepness` | `6.0` | Sigmoid steepness `k` (4=gentle, 6=moderate, 10=step) |
| `--use-cost-floor` | off | Per-fork cost-of-production price floor scaled by hashrate |

## Mining / Difficulty

| Argument | Default | Description |
|---|---|---|
| `--enable-difficulty` | off | Enable probability-per-tick mining mode (required for retarget) |
| `--retarget-interval` | `144` | Blocks between difficulty adjustments (use 2016 for realistic) |
| `--tick-interval` | `1.0` | Seconds per tick in difficulty mode |
| `--enable-eda` | off | Enable Emergency Difficulty Adjustment (BCH-style) |
| `--min-difficulty` | `0.0625` | Minimum difficulty floor (1/16) |
| `--initial-v27-hashrate` | auto | Override initial Fork A hashrate (if not using pool config) |

## Update Intervals

| Argument | Default | Description |
|---|---|---|
| `--interval` | `10` | Block mining interval (seconds), non-difficulty mode |
| `--duration` | `7200` | Experiment duration in seconds (default 2 hours) |
| `--hashrate-update-interval` | `600` | How often pools re-evaluate fork choice (10 min) |
| `--price-update-interval` | `60` | How often price oracle recalculates (1 min) |
| `--economic-update-interval` | `300` | How often economic nodes re-evaluate (5 min) |
| `--snapshot-interval` | `60` | Time series data snapshot interval |

## Pool Configuration

| Argument | Default | Description |
|---|---|---|
| `--pool-scenario` | `realistic_current` | Pool scenario name from `config/mining_pools_config.yaml` |
| `--network-yaml` | auto | Path to network.yaml for node metadata |

## Fork Reunion

| Argument | Default | Description |
|---|---|---|
| `--enable-reunion` | off | At end of duration, reconnect partitions and let heavier chain win |
| `--reunion-timeout` | `120` | Seconds to wait for reorg convergence after reconnection |
| `--uasf-duration` | off | UASF active duration (seconds). After this, Fork A nodes stop enforcing strict rules |
| `--uasf-expiry-action` | `reunion` | `reunion`, `accept`, or `continue` when UASF expires |

## Dynamic Switching

| Argument | Default | Description |
|---|---|---|
| `--enable-dynamic-switching` | on | Economic/user nodes can switch forks during the experiment |
| `--enable-reorg-metrics` | off | Track reorg events for impact metrics |

## Results

| Argument | Default | Description |
|---|---|---|
| `--results-id` | auto | Unique identifier for this run (timestamp-based) |

## Pool Config Format (`config/mining_pools_config.yaml`)

Each pool in the config file has:

```yaml
scenarios:
  my_scenario:
    pool_foundryusa:
      hashrate_pct: 26.89          # % of total Bitcoin hashrate
      fork_preference: fork_a      # fork_a, fork_b, or neutral
      max_loss_pct: 0.26           # switch when losing >26% profit
      ideology_strength: 0.51      # how strongly they resist switching
      initial_fork: fork_a         # which fork they start on
```

The `pool_committed_split` parameter in sweep specs controls how many pools are
assigned `fork_a` vs `fork_b` vs `neutral` — see `tools/generate_scenarios.py`.
