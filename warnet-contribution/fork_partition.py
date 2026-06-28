#!/usr/bin/env python3
"""
Fork Partition Scenario

Models a contested Bitcoin protocol fork: two versions of bitcoind are
network-partitioned and compete for mining support.

Mining pools hold ideology (committed to Fork A, committed to Fork B, or
neutral) and make independent profit-driven decisions about which fork to mine.
The difficulty retarget is the key cascade trigger: a fork mining with low
initial hashrate mines slowly, causing its difficulty to drop when the retarget
fires — suddenly making its blocks far more profitable and cascading remaining
pools.

Requires a pre-partitioned network (see examples/networks/fork_partition/).
Fork A and Fork B nodes must only have addnode connections within their own
partition before the scenario starts.

Profitability per unit of pool hashrate:
    profit[fork] = price[fork] / difficulty[fork]

Pool hashrate cancels when comparing forks, so only the price-to-difficulty
ratio drives pool decisions.

Difficulty retarget (tick-based):
    adj = retarget_interval / ticks_taken_to_mine_N_blocks
    new_difficulty = old_difficulty * adj
A fork with low hashrate takes more ticks per block, so adj < 1 and
difficulty drops, triggering the cascade.
"""

import argparse
import random
import yaml
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from time import sleep, time
from typing import Dict, List, Optional

from commander import Commander


# ---------------------------------------------------------------------------
# Pool model
# ---------------------------------------------------------------------------

class Ideology(Enum):
    FORK_A  = "fork_a"   # committed to Fork A (new rules)
    FORK_B  = "fork_b"   # committed to Fork B (old rules)
    NEUTRAL = "neutral"  # profit-driven only


@dataclass
class Pool:
    pool_id: str
    name: str
    hashrate_pct: float       # fraction of total network hashrate (0.0–1.0)
    ideology: Ideology
    max_loss_pct: float       # maximum tolerated profit loss before switching
    current_fork: str = "fork_b"


# Real Bitcoin mining pool distribution (2024)
DEFAULT_POOLS = [
    # (id, name, hashrate_fraction, ideology, max_loss_pct)
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
    tick-based difficulty retarget simulation.
    """

    def set_test_params(self):
        self.fork_a_nodes: list = []
        self.fork_b_nodes: list = []
        self.fork_a_wallets: list = []
        self.fork_b_wallets: list = []
        self.pools: List[Pool] = []

        self.blocks:     Dict[str, int]   = {"fork_a": 0, "fork_b": 0}
        self.difficulty: Dict[str, float] = {"fork_a": 1.0, "fork_b": 1.0}
        self.hashrate:   Dict[str, float] = {"fork_a": 0.0, "fork_b": 0.0}
        self.price:      Dict[str, float] = {"fork_a": 1.0, "fork_b": 1.0}
        self._retarget_start_tick:  Dict[str, int] = {"fork_a": 0, "fork_b": 0}
        self._econ_a: float = 0.7

    def add_options(self, parser: argparse.ArgumentParser):
        parser.description = (
            "Contested fork simulation: two bitcoind versions compete for "
            "hashrate dominance via pool ideology and the difficulty retarget cascade."
        )
        parser.usage = "warnet run fork_partition.py [options]"

        parser.add_argument("--fork-a-version", default="27.",
            help="bitcoind subversion prefix identifying Fork A nodes (default: '27.')")
        parser.add_argument("--fork-b-version", default="26.",
            help="bitcoind subversion prefix identifying Fork B nodes (default: '26.')")

        parser.add_argument("--fork-a-economic", type=float, default=70.0,
            help="Initial economic weight on Fork A, percent (default: 70)")
        parser.add_argument("--max-price-divergence", type=float, default=0.10,
            help="Maximum BTC price ratio between forks (default: 0.10 = ±10%%)")

        parser.add_argument("--pool-committed", type=float, default=0.0,
            help="Fraction of hashrate pre-committed to Fork A (0.0–1.0). "
                 "Pools are assigned greedily largest-first. (default: 0.0)")
        parser.add_argument("--pool-max-loss", type=float, default=0.26,
            help="Profit loss tolerance for committed pools before switching "
                 "(default: 0.26 = 26%%)")
        parser.add_argument("--pool-config", type=str, default=None,
            help="Optional YAML file overriding pool ideology assignments")

        parser.add_argument("--duration", type=int, default=7200,
            help="Scenario duration in seconds (default: 7200)")
        parser.add_argument("--interval", type=float, default=1.0,
            help="Tick interval in seconds (default: 1.0)")
        parser.add_argument("--retarget-interval", type=int, default=2016,
            help="Blocks per difficulty epoch (default: 2016)")
        parser.add_argument("--pool-update-ticks", type=int, default=600,
            help="Ticks between pool fork-choice evaluations (default: 600)")

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def _partition_nodes(self):
        """Classify nodes into Fork A / Fork B by bitcoind version string."""
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
                self.log.warning(f"  tank-{node.index}: {e}")
        self.log.info(
            f"Fork A: {len(self.fork_a_nodes)} nodes | "
            f"Fork B: {len(self.fork_b_nodes)} nodes"
        )

    def _setup_wallets(self):
        """Initialise a mining wallet on each node."""
        for node in self.fork_a_nodes:
            self.fork_a_wallets.append(self.ensure_miner(node))
        for node in self.fork_b_nodes:
            self.fork_b_wallets.append(self.ensure_miner(node))

    def _load_pools(self):
        """Load pool ideology from YAML or build from defaults + --pool-committed."""
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

        committed_target = self.options.pool_committed
        committed_so_far = 0.0
        for pid, name, hr, _, _ in DEFAULT_POOLS:
            if committed_so_far < committed_target:
                ideo, init_fork = Ideology.FORK_A, "fork_a"
                committed_so_far += hr
            else:
                ideo, init_fork = Ideology.NEUTRAL, "fork_b"
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
    # Price oracle
    # ------------------------------------------------------------------

    def _update_price(self):
        """Linear mapping: economic weight → price premium, capped at max_price_divergence."""
        cap = self.options.max_price_divergence
        premium = (self._econ_a - 0.5) * 2.0 * cap
        self.price["fork_a"] = 1.0 + premium
        self.price["fork_b"] = 1.0 - premium

    # ------------------------------------------------------------------
    # Difficulty retarget
    # ------------------------------------------------------------------

    def _fire_retarget(self, fork: str, tick: int):
        """
        Adjust difficulty based on ticks taken vs. expected.
        adj < 1 → difficulty drops  (fork was mining slowly → blocks easier)
        adj > 1 → difficulty rises  (fork was mining quickly → blocks harder)
        """
        ticks_taken = max(1, tick - self._retarget_start_tick[fork])
        adj = max(0.25, min(4.0, self.options.retarget_interval / ticks_taken))
        old = self.difficulty[fork]
        self.difficulty[fork] = old * adj
        self._retarget_start_tick[fork] = tick
        self.log.info(
            f"  RETARGET [{fork}] block {self.blocks[fork]:,}: "
            f"ticks={ticks_taken} adj={adj:.3f}x "
            f"difficulty {old:.4f} → {self.difficulty[fork]:.4f}"
        )

    # ------------------------------------------------------------------
    # Pool switching
    # ------------------------------------------------------------------

    def _profit(self, fork: str) -> float:
        """Profitability per unit of pool hashrate = price / difficulty."""
        return self.price[fork] / max(self.difficulty[fork], 1e-6)

    def _maybe_switch(self, pool: Pool) -> bool:
        cur   = pool.current_fork
        other = "fork_b" if cur == "fork_a" else "fork_a"
        p_cur, p_other = self._profit(cur), self._profit(other)

        if p_cur <= 0:
            pool.current_fork = other
            return True

        advantage = (p_other - p_cur) / p_cur   # positive = other fork is better

        if pool.ideology == Ideology.NEUTRAL:
            if advantage > 0:
                pool.current_fork = other
                return True

        elif pool.ideology == Ideology.FORK_A:
            if cur == "fork_a":
                if advantage > pool.max_loss_pct:     # losing too much staying on Fork A
                    pool.current_fork = "fork_b"
                    return True
            else:
                if self._profit("fork_a") >= self._profit("fork_b"):  # Fork A profitable again
                    pool.current_fork = "fork_a"
                    return True

        elif pool.ideology == Ideology.FORK_B:
            if cur == "fork_b":
                if advantage > pool.max_loss_pct:
                    pool.current_fork = "fork_a"
                    return True
            else:
                if self._profit("fork_b") >= self._profit("fork_a"):
                    pool.current_fork = "fork_b"
                    return True

        return False

    # ------------------------------------------------------------------
    # Outcome
    # ------------------------------------------------------------------

    def _classify(self) -> str:
        total = self.hashrate["fork_a"] + self.hashrate["fork_b"]
        if total == 0:
            return "unknown"
        share_a = self.hashrate["fork_a"] / total
        if share_a >= 0.80:   return "fork_a_dominant"
        if share_a <= 0.20:   return "fork_b_dominant"
        if 0.40 <= share_a <= 0.60: return "stalemate"
        return "contested"

    # ------------------------------------------------------------------
    # Main
    # ------------------------------------------------------------------

    def run_test(self):
        self.log.info("=" * 60)
        self.log.info("Fork Partition Scenario")
        self.log.info("=" * 60)

        self._econ_a = self.options.fork_a_economic / 100.0

        self._partition_nodes()
        if not self.fork_a_nodes or not self.fork_b_nodes:
            self.log.error(
                "Need at least one node on each fork. "
                "Check --fork-a-version / --fork-b-version and verify the "
                "network has nodes on both forks."
            )
            return

        self._setup_wallets()
        self._load_pools()
        self._update_price()

        self.log.info(
            f"Fork A: hashrate={self.hashrate['fork_a']:.1%}  "
            f"economic={self._econ_a:.1%}  price={self.price['fork_a']:.3f}"
        )
        self.log.info(
            f"Fork B: hashrate={self.hashrate['fork_b']:.1%}  "
            f"price={self.price['fork_b']:.3f}"
        )
        self.log.info(
            f"Committed to Fork A: "
            f"{[p.name for p in self.pools if p.ideology == Ideology.FORK_A] or ['none']}"
        )

        start_time = time()
        tick = 0

        while time() - start_time < self.options.duration:
            tick += 1

            # Block mining: probability per tick = hashrate / difficulty
            for fork, wallets in [("fork_a", self.fork_a_wallets),
                                   ("fork_b", self.fork_b_wallets)]:
                p_block = self.hashrate[fork] / max(self.difficulty[fork], 1e-6)
                if wallets and random.random() < p_block:
                    self.blocks[fork] += 1
                    wallet = wallets[self.blocks[fork] % len(wallets)]
                    try:
                        wallet.generatetoaddress(1, wallet.getnewaddress())
                    except Exception:
                        pass
                    if self.blocks[fork] % self.options.retarget_interval == 0:
                        self._fire_retarget(fork, tick)

            # Periodic pool decisions
            if tick % self.options.pool_update_ticks == 0:
                self._update_price()
                switched = sum(1 for p in self.pools if self._maybe_switch(p))
                self._recompute_hashrate()
                self.log.info(
                    f"t={time()-start_time:.0f}s | "
                    f"blocks a={self.blocks['fork_a']:,} b={self.blocks['fork_b']:,} | "
                    f"hashrate a={self.hashrate['fork_a']:.1%} b={self.hashrate['fork_b']:.1%} | "
                    f"price a={self.price['fork_a']:.3f} b={self.price['fork_b']:.3f} | "
                    f"diff a={self.difficulty['fork_a']:.3f} b={self.difficulty['fork_b']:.3f} | "
                    f"switched={switched}"
                )

            sleep(self.options.interval)

        # Final report
        self._recompute_hashrate()
        outcome = self._classify()
        self.log.info("=" * 60)
        self.log.info(f"OUTCOME: {outcome}")
        self.log.info(
            f"Fork A: hashrate={self.hashrate['fork_a']:.1%}  "
            f"blocks={self.blocks['fork_a']:,}  "
            f"price={self.price['fork_a']:.3f}  "
            f"difficulty={self.difficulty['fork_a']:.4f}"
        )
        self.log.info(
            f"Fork B: hashrate={self.hashrate['fork_b']:.1%}  "
            f"blocks={self.blocks['fork_b']:,}  "
            f"price={self.price['fork_b']:.3f}  "
            f"difficulty={self.difficulty['fork_b']:.4f}"
        )
        self.log.info(
            f"Fork A pools: "
            f"{[p.name for p in self.pools if p.current_fork == 'fork_a'] or ['none']}"
        )
        self.log.info(
            f"Fork B pools: "
            f"{[p.name for p in self.pools if p.current_fork == 'fork_b'] or ['none']}"
        )
        self.log.info("=" * 60)


def main():
    ForkPartition("").main()


if __name__ == "__main__":
    main()
