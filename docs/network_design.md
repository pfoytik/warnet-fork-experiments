# Network Design Guide

Two network topologies are provided. Both are Warnet `network.yaml` files defining
the Bitcoin node composition and their roles.

## Lite Network (`networks/lite/network.yaml`)

25 nodes, fast iteration, ~25% economic resolution.

| Role | Count | Purpose |
|---|---|---|
| `mining_pool` | 8 | Represent the major mining pools |
| `economic_aggregate` | 4 | Exchange/merchant aggregates (~25% each) |
| `power_user_aggregate` | 4 | Power user groups |
| `power_user` | 3 | Individual power users |
| `casual_user_aggregate` | 6 | Retail user groups |

**When to use:** Initial parameter exploration, fast sweeps, hypothesis testing.
Each scenario takes ~217 min wall-clock (same as full network — the bottleneck
is the simulation duration, not node count).

**Limitation:** Economic split only has 4 discrete levels of resolution (~25% each).
Pool parameter effects can appear more significant than they actually are at full resolution.

## Full Network (`networks/full/network.yaml`)

60 nodes, production results, ~4% economic resolution.

| Role | Count | Purpose |
|---|---|---|
| `mining_pool` | 8 | Real pool proportions (Foundry 27%, AntPool 19%, etc.) |
| `major_exchange` | 4 | Large exchanges (Coinbase, Binance, Kraken, etc.) |
| `exchange` | 6 | Mid-tier exchanges |
| `payment_processor` | 3 | Payment rail companies |
| `merchant` | 6 | BTC-accepting merchants |
| `institutional` | 5 | ETFs, corporate treasuries |
| `power_user` | 12 | Active BTC users |
| `casual_user` | 16 | Retail holders |

**When to use:** Final parameter sweeps, paper-quality results, full-network findings.
Economic split is the dominant predictor at full resolution (60% RF feature importance).

## Node Metadata

Each node in network.yaml can carry metadata that the scenario uses:

```yaml
nodes:
  - name: node-0000
    image: bitcoin/bitcoin:27.0   # Fork A
    metadata:
      role: mining_pool
      pool_id: foundryusa
      fork: fork_a                # Which partition this node is in
      economic_weight: 0.0        # How much BTC custody this node holds
      transaction_velocity: 0.0   # Fee-generating activity rate

  - name: node-0010
    image: bitcoin/bitcoin:26.0   # Fork B
    metadata:
      role: major_exchange
      fork: fork_b
      economic_weight: 0.08       # Holds 8% of total BTC custody
      transaction_velocity: 0.7   # High fee-generating activity
      accepts_foreign_blocks: true  # Will accept Fork A blocks (bridge node)
```

## Designing a Custom Network

1. Copy `networks/lite/network.yaml` as a starting point
2. Set half of your nodes to `image: bitcoin/bitcoin:27.0` (or your Fork A version)
3. Set the other half to `image: bitcoin/bitcoin:26.0` (or your Fork B version)
4. Assign `economic_weight` values that sum to 1.0 across all nodes
5. Set `fork` tag matching the node version
6. Designate at least one Fork B node as `accepts_foreign_blocks: true`
   (the bridge node for cross-partition block submission)
7. Run with `--fork-a-version` and `--fork-b-version` matching your node image tags

## The Bridge Node

Fork A blocks get submitted to Fork B nodes (and vice versa) via the `submitblock`
RPC to a single designated "bridge" node per partition. P2P propagation within
that partition's island handles the rest. This models how blocks travel across
the real internet even when P2P is partitioned.

The bridge node is the first node found with `accepts_foreign_blocks: true`
in the metadata. If none is set, all Fork B nodes default to accepting Fork A blocks
(standard soft fork model where new-rules blocks are valid under old rules).
