#!/usr/bin/env python3
"""
Fork Partition Scenario — Warnet Example

Models a contested Bitcoin protocol fork: two versions of bitcoind (Fork A
and Fork B) are network-partitioned and compete for mining support.

Mining pools make independent decisions based on ideology and profitability.
The 2016-block difficulty retarget is the key cascade trigger: the fork with
less initial hashrate mines slowly, causing its difficulty to drop when the
retarget fires — suddenly making its blocks far more profitable, which can
cascade all remaining pools.

Self-contained: no external library dependencies beyond Warnet's Commander.

Usage:
    warnet run fork_partition.py [options]

Key parameters:
    --fork-a-economic 74     Initial economic weight on Fork A (%)
    --pool-committed 0.30    Fraction of hashrate committed to Fork A
    --retarget-interval 2016 Blocks between difficulty adjustments
    --duration 7200          Scenario duration in seconds
"""

import argparse
import random
import yaml
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from time import sleep, time
from typing import Dict, List, Optional

from commander import Commander


# ---------------------------------------------------------------------------
# Pool model
# ---------------------------------------------------------------------------

class Ideology(Enum):
    FORK_A  = "fork_a"   # ideologically committed to Fork A (new rules)
    FORK_B  = "fork_b"   # ideologically committed to Fork B (old rules)
    NEUTRAL = "neutral"  # profit-driven only


@dataclass
class Pool:
    pool_id: str
    name: str
    hashrate_pct: float       # fraction of total network hashrate (0.0–1.0)
    ideology: Ideology
    max_loss_pct: float       # maximum tolerated profit loss before switching
    current_fork: str = "fork_b"


# ---------------------------------------------------------------------------
# Default pool distribution (real Bitcoin mining pools, 2024)
# ---------------------------------------------------------------------------

DEFAULT_POOLS = [
    # id, name, hashrate%, ideology, max_loss_pct
    ("foundryusa",  "Foundry USA",   0.2689, Ideology.NEUTRAL, 0.05),
    ("antpool",     "AntPool",       0.1925, Ideology.NEUTRAL, 0.05),
    ("viabtc",      "ViaBTC",        0.1139, Ideology.NEUTRAL, 0.05),
    ("f2pool",      "F2Pool",        0.1125, Ideology.NEUTRAL, 0.05),
    ("binancepool", "Binance Pool",  0.1004, Ideology.NEUTRAL, 0.05),
    ("marapool",    "MARA Pool",     0.0825, Ideology.NEUTRAL, 0.05),
    ("sbicrypto",   "SBI Crypto",    0.0457, Ideology.NEUTRAL, 0.05),
    ("luxor",       "Luxor",         0.0394, Ideology.NEUTRAL, 0.05),
    ("ocean",       "OCEAN",         0.0142, Ideology.NEUTRAL, 0.05),
    ("braiins",     "Braiins Pool",  0.0137, Ideology.NEUTRAL, 0.05),
]


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------

class ForkPartition(Commander):
    """
    Partitioned fork scenario with pool ideology, price oracle, and
    difficulty retarget simulation.

    Profitability per unit of pool hashrate on a fork:
        profit = price[fork] / difficulty[fork]

    Pool hashrate and block reward cancel out when comparing forks, so
    only the price-to-difficulty ratio drives pool decisions. This is
    the correct formula: a pool's share of fork earnings equals its
    share of fork hashrate regardless of how much other hashrate is
    present (at fixed difficulty).

    Difficulty retarget:
        adj_factor = retarget_interval / ticks_taken_to_mine_N_blocks
        new_difficulty = old_difficulty * adj_factor
    A fork mining with low hashrate takes more ticks per block, so
    adj_factor < 1 and difficulty drops — making its blocks more
    profitable and triggering the cascade.
    """

    def set_test_params(self):
        self.fork_a_nodes: list = []
        self.fork_b_nodes: list = []
        self.pools: List[Pool] = []

        # Simulation state
        self.blocks:     Dict[str, int]   = {"fork_a": 0, "fork_b": 0}
        self.difficulty: Dict[str, float] = {"fork_a": 1.0, "fork_b": 1.0}
        self.hashrate:   Dict[str, float] = {"fork_a": 0.0, "fork_b": 0.0}
        self.price:      Dict[str, float] = {"fork_a": 1.0, "fork_b": 1.0}

        # Retarget tracking (tick-based)
        self._retarget_start_tick: Dict[str, int] = {"fork_a": 0, "fork_b": 0}

    def add_options(self, parser: argparse.ArgumentParser):
        parser.description = (
            "Contested fork simulation: two bitcoind versions compete for "
            "hashrate via pool ideology and the difficulty retarget cascade."
        )
        parser.usage = "warnet run fork_partition.py [options]"

        # Fork identity
        parser.add_argument("--fork-a-version", default="27.",
            help="bitcoind subversion prefix for Fork A nodes (default: '27.')")
        parser.add_argument("--fork-b-version", default="26.",
            help="bitcoind subversion prefix for Fork B nodes (default: '26.')")

        # Economic conditions
        parser.add_argument("--fork-a-economic", type=float, default=70.0,
            help="Initial economic weight on Fork A, percent (default: 70)")
        parser.add_argument("--max-price-divergence", type=float, default=0.10,
            help="Maximum price ratio between forks (default: 0.10 = ±10%%)")

        # Pool commitment
        parser.add_argument("--pool-committed", type=float, default=0.0,
            help="Fraction of hashrate pre-committed to Fork A (0.0–1.0). "
                 "Pools are assigned greedily largest-first. Remaining pools "
                 "start neutral on Fork B. (default: 0.0)")
        parser.add_argument("--pool-max-loss", type=float, default=0.26,
            help="Max loss fraction for committed pools before switching "
                 "(default: 0.26 = 26%%)")
        parser.add_argument("--pool-config", type=str, default=None,
            help="Path to YAML file overriding pool assignments "
                 "(see warnet-contribution/pool_config_example.yaml)")

        # Timing
        parser.add_argument("--duration", type=int, default=7200,
            help="Scenario duration in seconds (default: 7200)")
        parser.add_argument("--interval", type=float, default=1.0,
            help="Tick interval in seconds; smaller = faster sim (default: 1.0)")
        parser.add_argument("--retarget-interval", type=int, default=2016,
            help="Blocks per difficulty epoch (default: 2016)")
        parser.add_argument("--pool-update-ticks", type=int, default=600,
            help="Ticks between pool fork-choice evaluations (default: 600)")

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _partition_nodes(self):
        ver_a = self.options.fork_a_version
        ver_b = self.options.fork_b_version
        for node in self.nodes:
            try:
                ver = node.getnetworkinfo().get("subversion", "")
                if ver_a in ver:
                    self.fork_a_nodes.append(node)
                elif ver_b in ver:
                    self.fork_b_nodes.append(node)
            except Exception as e:
                self.log.warning(f"  node-{node.index}: {e}")
        self.log.info(
            f"Fork A: {len(self.fork_a_nodes)} nodes | "
            f"Fork B: {len(self.fork_b_nodes)} nodes"
        )

    def _load_pools(self):
        if self.options.pool_config:
            path = Path(self.options.pool_config)
            if path.exists():
                with open(path) as f:
                    cfg = yaml.safe_load(f)
                for p in cfg.get("pools", []):
                    ideo = {
                        "fork_a": Ideology.FORK_A,
                        "fork_b": Ideology.FORK_B,
                    }.get(p.get("ideology", "neutral"), Ideology.NEUTRAL)
                    init = "fork_a" if ideo == Ideology.FORK_A else "fork_b"
                    self.pools.append(Pool(
                        pool_id=p["id"], name=p["name"],
                        hashrate_pct=p["hashrate_pct"] / 100.0,
                        ideology=ideo,
                        max_loss_pct=p.get("max_loss_pct", self.options.pool_max_loss),
                        current_fork=p.get("initial_fork", init),
                    ))
                self._recompute_hashrate()
                return

        # Build from defaults + --pool-committed assignment
        committed_target = self.options.pool_committed
        committed_so_far = 0.0
        for pid, name, hr, _, _ in DEFAULT_POOLS:
            if committed_so_far < committed_target:
                ideo = Ideology.FORK_A
                init_fork = "fork_a"
                committed_so_far += hr
            else:
                ideo = Ideology.NEUTRAL
                init_fork = "fork_b"
            self.pools.append(Pool(
                pool_id=pid, name=name, hashrate_pct=hr,
                ideology=ideo,
                max_loss_pct=self.options.pool_max_loss,
                current_fork=init_fork,
            ))
        self._recompute_hashrate()

    def _recompute_hashrate(self):
        self.hashrate = {"fork_a": 0.0, "fork_b": 0.0}
        for p in self.pools:
            self.hashrate[p.current_fork] += p.hashrate_pct

    # ------------------------------------------------------------------
    # Price oracle (linear economic weight → price ratio)
    # ------------------------------------------------------------------

    def _update_price(self):
        ea = self.hashrate.get("fork_a", 0.5)   # economic weight tracks hashrate in simple model
        # Use configured economic weight for price, not mining hashrate
        ea = self._econ_a
        cap = self.options.max_price_divergence
        # Map economic weight [0,1] to price premium [-cap, +cap]
        premium = (ea - 0.5) * 2.0 * cap
        self.price["fork_a"] = 1.0 + premium
        self.price["fork_b"] = 1.0 - premium

    # ------------------------------------------------------------------
    # Difficulty retarget
    # ------------------------------------------------------------------

    def _fire_retarget(self, fork: str, current_tick: int):
        ticks_taken = max(1, current_tick - self._retarget_start_tick[fork])
        target_ticks = self.options.retarget_interval  # expected ticks at hashrate=1.0
        # Clamp to Bitcoin's 4× bound
        adj = max(0.25, min(4.0, target_ticks / ticks_taken))
        old = self.difficulty[fork]
        self.difficulty[fork] = old * adj
        self._retarget_start_tick[fork] = current_tick
        self.log.info(
            f"  RETARGET [{fork}] block {self.blocks[fork]:,}: "
            f"ticks={ticks_taken} adj={adj:.3f}x "
            f"difficulty {old:.4f} → {self.difficulty[fork]:.4f}"
        )

    # ------------------------------------------------------------------
    # Pool switching
    # ------------------------------------------------------------------

    def _pool_profit(self, fork: str) -> float:
        """
        Profitability per unit of pool hashrate on a fork.
        = price[fork] / difficulty[fork]
        Fork hashrate cancels when computing a pool's block share,
        so only price and difficulty determine relative attractiveness.
        """
        return self.price[fork] / max(self.difficulty[fork], 1e-6)

    def _maybe_switch(self, pool: Pool) -> bool:
        cur   = pool.current_fork
        other = "fork_b" if cur == "fork_a" else "fork_a"

        p_cur   = self._pool_profit(cur)
        p_other = self._pool_profit(other)

        if p_cur <= 0:
            pool.current_fork = other
            return True

        # How much better is the other fork? (positive = other is better)
        advantage = (p_other - p_cur) / p_cur

        if pool.ideology == Ideology.NEUTRAL:
            if advantage > 0:
                pool.current_fork = other
                return True

        elif pool.ideology == Ideology.FORK_A:
            if cur == "fork_a":
                # On preferred fork: only leave if loss exceeds tolerance
                if advantage > pool.max_loss_pct:
                    pool.current_fork = "fork_b"
                    return True
            else:
                # Off preferred fork: return if fork_a is profitable again
                if self._pool_profit("fork_a") >= self._pool_profit("fork_b"):
                    pool.current_fork = "fork_a"
                    return True

        elif pool.ideology == Ideology.FORK_B:
            if cur == "fork_b":
                if advantage > pool.max_loss_pct:
                    pool.current_fork = "fork_a"
                    return True
            else:
                if self._pool_profit("fork_b") >= self._pool_profit("fork_a"):
                    pool.current_fork = "fork_b"
                    return True

        return False

    # ------------------------------------------------------------------
    # Outcome classification
    # ------------------------------------------------------------------

    def _classify(self) -> str:
        ha = self.hashrate["fork_a"]
        hb = self.hashrate["fork_b"]
        total = ha + hb
        if total == 0:
            return "unknown"
        share_a = ha / total
        if share_a >= 0.80:
            return "fork_a_dominant"
        if share_a <= 0.20:
            return "fork_b_dominant"
        if 0.40 <= share_a <= 0.60:
            return "stalemate"
        return "contested"

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run_test(self):
        self.log.info("=" * 60)
        self.log.info("Fork Partition Scenario")
        self.log.info("=" * 60)

        # Store initial economic weight
        self._econ_a = self.options.fork_a_economic / 100.0

        self._partition_nodes()
        if not self.fork_a_nodes or not self.fork_b_nodes:
            self.log.error(
                "Need at least one node on each fork. "
                "Check --fork-a-version and --fork-b-version match your network."
            )
            return

        self._load_pools()
        self._update_price()

        # Network-partition: disconnect Fork A nodes from Fork B nodes
        for a_node in self.fork_a_nodes:
            for b_node in self.fork_b_nodes:
                try:
                    a_node.disconnectnode(address=f"node-{b_node.index:04d}")
                except Exception:
                    pass

        self.log.info(
            f"Initial state: "
            f"Fork A hashrate={self.hashrate['fork_a']:.1%} "
            f"economic={self._econ_a:.1%} "
            f"price={self.price['fork_a']:.3f} | "
            f"Fork B hashrate={self.hashrate['fork_b']:.1%} "
            f"price={self.price['fork_b']:.3f}"
        )
        self.log.info(
            f"Committed pools on Fork A: "
            f"{[p.name for p in self.pools if p.ideology == Ideology.FORK_A]}"
        )

        start_time  = time()
        duration    = self.options.duration
        interval    = self.options.interval
        retarget_n  = self.options.retarget_interval
        update_n    = self.options.pool_update_ticks
        tick        = 0

        while time() - start_time < duration:
            tick += 1

            # --- Block mining (probability per tick = hashrate / difficulty) ---
            for fork, nodes in [("fork_a", self.fork_a_nodes),
                                 ("fork_b", self.fork_b_nodes)]:
                p_block = self.hashrate[fork] / max(self.difficulty[fork], 1e-6)
                if nodes and random.random() < p_block:
                    self.blocks[fork] += 1
                    miner = nodes[self.blocks[fork] % len(nodes)]
                    try:
                        addr = miner.getnewaddress()
                        miner.generatetoaddress(1, addr)
                    except Exception:
                        pass
                    if self.blocks[fork] % retarget_n == 0:
                        self._fire_retarget(fork, tick)

            # --- Periodic pool decisions ---
            if tick % update_n == 0:
                self._update_price()
                switched = sum(1 for p in self.pools if self._maybe_switch(p))
                self._recompute_hashrate()
                elapsed = time() - start_time
                self.log.info(
                    f"t={elapsed:.0f}s | "
                    f"blocks a={self.blocks['fork_a']:,} b={self.blocks['fork_b']:,} | "
                    f"hashrate a={self.hashrate['fork_a']:.1%} b={self.hashrate['fork_b']:.1%} | "
                    f"price a={self.price['fork_a']:.3f} b={self.price['fork_b']:.3f} | "
                    f"difficulty a={self.difficulty['fork_a']:.3f} b={self.difficulty['fork_b']:.3f} | "
                    f"switched={switched}"
                )

            sleep(interval)

        # --- Final report ---
        self._recompute_hashrate()
        outcome = self._classify()

        self.log.info("=" * 60)
        self.log.info(f"OUTCOME: {outcome}")
        self.log.info(
            f"Fork A: hashrate={self.hashrate['fork_a']:.1%} "
            f"blocks={self.blocks['fork_a']:,} "
            f"price={self.price['fork_a']:.3f} "
            f"difficulty={self.difficulty['fork_a']:.4f}"
        )
        self.log.info(
            f"Fork B: hashrate={self.hashrate['fork_b']:.1%} "
            f"blocks={self.blocks['fork_b']:,} "
            f"price={self.price['fork_b']:.3f} "
            f"difficulty={self.difficulty['fork_b']:.4f}"
        )
        a_pools = [p.name for p in self.pools if p.current_fork == "fork_a"]
        b_pools = [p.name for p in self.pools if p.current_fork == "fork_b"]
        self.log.info(f"Fork A pools ({len(a_pools)}): {', '.join(a_pools) or 'none'}")
        self.log.info(f"Fork B pools ({len(b_pools)}): {', '.join(b_pools) or 'none'}")
        self.log.info("=" * 60)


def main():
    ForkPartition("").main()


if __name__ == "__main__":
    main()
