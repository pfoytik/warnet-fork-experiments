#!/usr/bin/env python3
"""
Difficulty Oracle - Block Timing & Heaviest Chain for Fork Scenarios

Models realistic block production timing after a fork and determines the winning
fork by cumulative proof-of-work (chainwork) rather than simple block count.

Key dynamics:
- After a fork, both chains inherit the same difficulty
- Minority chain produces blocks dramatically slower until difficulty adjusts
- Each fork retargets independently every N blocks (configurable)
- Chain weight is cumulative chainwork, not block height
- Optional Emergency Difficulty Adjustment (EDA) for minority chain survival

Block Production Model:
    probability_per_tick = tick_interval / expected_block_interval
    expected_block_interval = target_interval * (difficulty / hashrate_fraction)

    A fork with 10% hashrate and pre-fork difficulty:
    expected = 10 * (1.0 / 0.1) = 100s. At 1s ticks, P = 1%.

Usage:
    from difficulty_oracle import DifficultyOracle

    oracle = DifficultyOracle(target_block_interval=10.0)
    oracle.initialize_fork('fork_a', initial_height=101)
    oracle.initialize_fork('fork_b', initial_height=101)

    # Per tick:
    if oracle.should_mine_block('fork_a', hashrate_pct=70.0, tick_interval=1.0):
        # mine a block on fork_a
        event = oracle.record_block('fork_a', sim_time=elapsed, height=new_height)

    winner, winner_cw, loser_cw = oracle.get_winning_fork()
"""

import json
import random
import time
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional, Tuple


@dataclass
class DifficultyState:
    """Per-fork difficulty and chainwork state."""
    fork_id: str
    current_difficulty: float
    blocks_since_retarget: int
    cumulative_chainwork: float
    last_block_sim_time: float
    last_retarget_sim_time: float
    expected_block_interval: float
    initial_height: int
    blocks_mined: int = 0


@dataclass
class DifficultyAdjustmentEvent:
    """Records a difficulty retarget event."""
    fork_id: str
    sim_time: float
    height: int
    old_difficulty: float
    new_difficulty: float
    actual_time: float  # Time taken for last retarget_interval blocks
    target_time: float  # Expected time for last retarget_interval blocks
    adjustment_factor: float
    is_eda: bool = False


class DifficultyOracle:
    """
    Models difficulty adjustment and block production timing for forked chains.

    After a fork, both chains start with the same difficulty. Block production
    rate depends on hashrate fraction: a chain with less hashrate produces blocks
    slower. Difficulty retargets periodically to restore target block interval.

    Chain weight is cumulative chainwork (sum of difficulty of all blocks),
    matching Bitcoin's actual consensus rule for determining the heaviest chain.
    """

    def __init__(
        self,
        target_block_interval: float = 10.0,
        retarget_interval: int = 144,
        pre_fork_difficulty: float = 1.0,
        max_adjustment_factor: float = 4.0,
        min_difficulty: float = 0.0625,
        enable_eda: bool = False,
        eda_threshold: float = 6.0,
        eda_reduction: float = 0.20,
    ):
        """
        Initialize difficulty oracle.

        Args:
            target_block_interval: Target seconds between blocks (matches --interval)
            retarget_interval: Blocks between difficulty adjustments (default 144)
            pre_fork_difficulty: Starting difficulty for both forks (normalized, default 1.0)
            max_adjustment_factor: Maximum difficulty change per retarget (default 4.0)
            min_difficulty: Floor for difficulty (default 0.0625 = 1/16 of pre-fork)
            enable_eda: Enable Emergency Difficulty Adjustment (BCH-style)
            eda_threshold: EDA triggers when block time > threshold * target_interval
            eda_reduction: Fraction to reduce difficulty per EDA (default 0.20 = 20%)
        """
        self.target_block_interval = target_block_interval
        self.retarget_interval = retarget_interval
        self.pre_fork_difficulty = pre_fork_difficulty
        self.max_adjustment_factor = max_adjustment_factor
        self.min_difficulty = min_difficulty
        self.enable_eda = enable_eda
        self.eda_threshold = eda_threshold
        self.eda_reduction = eda_reduction

        # Per-fork state
        self.forks: Dict[str, DifficultyState] = {}

        # Event history
        self.adjustment_history: List[DifficultyAdjustmentEvent] = []

    def initialize_fork(self, fork_id: str, initial_height: int = 0):
        """
        Set up initial state for a fork. Both forks start with the same difficulty.

        Args:
            fork_id: Fork identifier (e.g., 'fork_a', 'fork_b')
            initial_height: Block height at fork point
        """
        expected_interval = self.target_block_interval * (self.pre_fork_difficulty / 1.0)
        self.forks[fork_id] = DifficultyState(
            fork_id=fork_id,
            current_difficulty=self.pre_fork_difficulty,
            blocks_since_retarget=0,
            cumulative_chainwork=0.0,
            last_block_sim_time=0.0,
            last_retarget_sim_time=0.0,
            expected_block_interval=expected_interval,
            initial_height=initial_height,
            blocks_mined=0,
        )

    def should_mine_block(
        self, fork_id: str, hashrate_pct: float, tick_interval: float
    ) -> bool:
        """
        Determine if a block should be mined on this fork during this tick.

        Wrapper for backwards compatibility. Use get_blocks_to_mine() for
        multi-block support.

        Returns:
            True if at least one block should be mined
        """
        return self.get_blocks_to_mine(fork_id, hashrate_pct, tick_interval) > 0

    def get_blocks_to_mine(
        self, fork_id: str, hashrate_pct: float, tick_interval: float
    ) -> int:
        """
        Calculate number of blocks to mine on this fork during this tick.

        Uses probability sampling: each tick, the expected number of blocks is
        tick_interval / expected_block_interval, where expected_block_interval
        accounts for both difficulty and hashrate fraction.

        When expected blocks per tick > 1 (fast block times), this correctly
        returns multiple blocks to maintain accurate block production rates.

        Args:
            fork_id: Fork identifier
            hashrate_pct: Percentage of total hashrate on this fork (0-100)
            tick_interval: Duration of this tick in seconds

        Returns:
            Number of blocks to mine (0 or more)
        """
        if fork_id not in self.forks:
            return 0

        state = self.forks[fork_id]
        hashrate_fraction = max(hashrate_pct / 100.0, 0.001)  # Avoid division by zero

        # expected_block_interval = target * (difficulty / hashrate_fraction)
        expected = self.target_block_interval * (state.current_difficulty / hashrate_fraction)
        state.expected_block_interval = expected

        # Expected blocks per tick (can be > 1.0)
        expected_blocks = tick_interval / expected if expected > 0 else 1.0

        if expected_blocks < 1.0:
            # Normal case: probabilistic single block
            return 1 if random.random() < expected_blocks else 0
        else:
            # Fast block case: guaranteed blocks + probabilistic remainder
            guaranteed = int(expected_blocks)
            remainder = expected_blocks - guaranteed
            extra = 1 if random.random() < remainder else 0
            return guaranteed + extra

    def record_block(
        self, fork_id: str, sim_time: float, height: int
    ) -> Optional[DifficultyAdjustmentEvent]:
        """
        Record a mined block and update chainwork. Check for retarget.

        Args:
            fork_id: Fork identifier
            sim_time: Current simulation time (seconds since start)
            height: New block height

        Returns:
            DifficultyAdjustmentEvent if a retarget occurred, else None
        """
        if fork_id not in self.forks:
            return None

        state = self.forks[fork_id]

        # Add chainwork for this block (work = difficulty)
        state.cumulative_chainwork += state.current_difficulty
        state.blocks_since_retarget += 1
        state.blocks_mined += 1
        state.last_block_sim_time = sim_time

        # Check for difficulty retarget
        retarget_event = None
        if state.blocks_since_retarget >= self.retarget_interval:
            retarget_event = self._perform_retarget(fork_id, sim_time, height)

        # Check for EDA (independent of retarget)
        if self.enable_eda and retarget_event is None:
            eda_event = self._check_eda(fork_id, sim_time)
            if eda_event is not None:
                retarget_event = eda_event

        return retarget_event

    def _perform_retarget(
        self, fork_id: str, sim_time: float, height: int
    ) -> DifficultyAdjustmentEvent:
        """
        Perform Bitcoin-style difficulty adjustment.

        new_difficulty = old_difficulty * (target_time / actual_time)
        Capped at max_adjustment_factor (4x up or down).

        Args:
            fork_id: Fork identifier
            sim_time: Current simulation time
            height: Current block height

        Returns:
            DifficultyAdjustmentEvent recording the retarget
        """
        state = self.forks[fork_id]

        # Calculate actual time for last retarget_interval blocks
        actual_time = sim_time - state.last_retarget_sim_time
        if actual_time <= 0:
            actual_time = 0.01  # Prevent division by zero

        # Target time for retarget_interval blocks
        target_time = self.retarget_interval * self.target_block_interval

        # Bitcoin DAA: new_diff = old_diff * (target_time / actual_time)
        adjustment_factor = target_time / actual_time

        # Cap adjustment to max_adjustment_factor
        adjustment_factor = max(
            1.0 / self.max_adjustment_factor,
            min(self.max_adjustment_factor, adjustment_factor),
        )

        old_difficulty = state.current_difficulty
        new_difficulty = old_difficulty * adjustment_factor

        # Enforce minimum difficulty
        new_difficulty = max(new_difficulty, self.min_difficulty)

        # Update state
        state.current_difficulty = new_difficulty
        state.blocks_since_retarget = 0
        state.last_retarget_sim_time = sim_time

        # Record event
        event = DifficultyAdjustmentEvent(
            fork_id=fork_id,
            sim_time=sim_time,
            height=height,
            old_difficulty=old_difficulty,
            new_difficulty=new_difficulty,
            actual_time=actual_time,
            target_time=target_time,
            adjustment_factor=adjustment_factor,
            is_eda=False,
        )
        self.adjustment_history.append(event)

        return event

    def _check_eda(
        self, fork_id: str, sim_time: float
    ) -> Optional[DifficultyAdjustmentEvent]:
        """
        Check and apply Emergency Difficulty Adjustment (BCH-style).

        Triggers when time since last block exceeds eda_threshold * target_interval.
        Reduces difficulty by eda_reduction fraction.

        Args:
            fork_id: Fork identifier
            sim_time: Current simulation time

        Returns:
            DifficultyAdjustmentEvent if EDA triggered, else None
        """
        state = self.forks[fork_id]

        time_since_last_block = sim_time - state.last_block_sim_time
        threshold = self.eda_threshold * self.target_block_interval

        if time_since_last_block < threshold:
            return None

        old_difficulty = state.current_difficulty
        new_difficulty = old_difficulty * (1.0 - self.eda_reduction)

        # Enforce minimum difficulty
        new_difficulty = max(new_difficulty, self.min_difficulty)

        if new_difficulty >= old_difficulty:
            return None  # Already at minimum, no change

        state.current_difficulty = new_difficulty

        event = DifficultyAdjustmentEvent(
            fork_id=fork_id,
            sim_time=sim_time,
            height=state.initial_height + state.blocks_mined,
            old_difficulty=old_difficulty,
            new_difficulty=new_difficulty,
            actual_time=time_since_last_block,
            target_time=self.target_block_interval,
            adjustment_factor=(1.0 - self.eda_reduction),
            is_eda=True,
        )
        self.adjustment_history.append(event)

        return event

    def get_blocks_per_hour(self, fork_id: str, hashrate_pct: float) -> float:
        """
        Get expected blocks per hour for a fork given current difficulty and hashrate.

        Args:
            fork_id: Fork identifier
            hashrate_pct: Percentage of total hashrate on this fork (0-100)

        Returns:
            Expected blocks per hour
        """
        if fork_id not in self.forks:
            return 6.0  # Default fallback

        state = self.forks[fork_id]
        hashrate_fraction = max(hashrate_pct / 100.0, 0.001)

        expected_interval = self.target_block_interval * (
            state.current_difficulty / hashrate_fraction
        )

        if expected_interval <= 0:
            return 6.0

        return 3600.0 / expected_interval

    def get_cumulative_chainwork(self, fork_id: str) -> float:
        """
        Get cumulative chainwork for a fork.

        Args:
            fork_id: Fork identifier

        Returns:
            Cumulative chainwork (sum of difficulty of all blocks)
        """
        if fork_id not in self.forks:
            return 0.0
        return self.forks[fork_id].cumulative_chainwork

    def get_chain_weight(self, fork_id: str) -> float:
        """
        Get normalized chainwork proportion for a fork (0-1).

        Args:
            fork_id: Fork identifier

        Returns:
            Chainwork as fraction of total chainwork across all forks
        """
        total_chainwork = sum(s.cumulative_chainwork for s in self.forks.values())
        if total_chainwork <= 0:
            return 0.5  # Equal weight before any blocks

        if fork_id not in self.forks:
            return 0.0

        return self.forks[fork_id].cumulative_chainwork / total_chainwork

    def get_winning_fork(self) -> Tuple[str, float, float]:
        """
        Determine the fork with highest cumulative chainwork.

        Returns:
            Tuple of (winning_fork_id, winner_chainwork, loser_chainwork)
        """
        if not self.forks:
            return ('', 0.0, 0.0)

        sorted_forks = sorted(
            self.forks.values(),
            key=lambda s: s.cumulative_chainwork,
            reverse=True,
        )

        winner = sorted_forks[0]
        loser = sorted_forks[1] if len(sorted_forks) > 1 else winner

        return (winner.fork_id, winner.cumulative_chainwork, loser.cumulative_chainwork)

    def export_to_json(self, output_path: str):
        """Export difficulty state and history to JSON file."""
        export_data = {
            'config': {
                'target_block_interval': self.target_block_interval,
                'retarget_interval': self.retarget_interval,
                'pre_fork_difficulty': self.pre_fork_difficulty,
                'max_adjustment_factor': self.max_adjustment_factor,
                'min_difficulty': self.min_difficulty,
                'enable_eda': self.enable_eda,
                'eda_threshold': self.eda_threshold,
                'eda_reduction': self.eda_reduction,
            },
            'forks': {
                fork_id: asdict(state)
                for fork_id, state in self.forks.items()
            },
            'winning_fork': self.get_winning_fork()[0],
            'adjustment_history': [asdict(e) for e in self.adjustment_history],
        }

        with open(output_path, 'w') as f:
            json.dump(export_data, f, indent=2)

    def print_summary(self):
        """Print current difficulty state summary."""
        print("=" * 70)
        print("DIFFICULTY ORACLE SUMMARY")
        print("=" * 70)
        print(f"Target block interval: {self.target_block_interval:.1f}s")
        print(f"Retarget interval: {self.retarget_interval} blocks")
        print(f"EDA enabled: {self.enable_eda}")

        winner_id, winner_cw, loser_cw = self.get_winning_fork()

        for fork_id, state in self.forks.items():
            is_winner = fork_id == winner_id
            marker = " << WINNING" if is_winner else ""
            print(f"\n  {fork_id}{marker}:")
            print(f"    Difficulty: {state.current_difficulty:.6f}")
            print(f"    Blocks mined: {state.blocks_mined}")
            print(f"    Cumulative chainwork: {state.cumulative_chainwork:.4f}")
            print(f"    Chain weight: {self.get_chain_weight(fork_id):.4f}")
            print(f"    Expected block interval: {state.expected_block_interval:.2f}s")
            print(f"    Blocks since retarget: {state.blocks_since_retarget}")

        print(f"\nDifficulty adjustments: {len(self.adjustment_history)}")
        eda_count = sum(1 for e in self.adjustment_history if e.is_eda)
        if eda_count > 0:
            print(f"  EDA activations: {eda_count}")
        print("=" * 70)


if __name__ == '__main__':
    """Standalone test: simulate a 70/30 hashrate split with difficulty adjustment."""
    import sys

    print("=" * 70)
    print("DIFFICULTY ORACLE STANDALONE TEST")
    print("=" * 70)
    print()

    # Configuration
    target_interval = 10.0
    retarget_interval = 20  # Small for fast test iteration
    tick_interval = 1.0
    fork_a_hashrate = 70.0
    fork_b_hashrate = 30.0
    duration = 600  # 10 minutes simulated

    print(f"Target block interval: {target_interval}s")
    print(f"Retarget interval: {retarget_interval} blocks")
    print(f"Tick interval: {tick_interval}s")
    print(f"Hashrate split: fork_a={fork_a_hashrate}% / fork_b={fork_b_hashrate}%")
    print(f"Simulation duration: {duration}s")
    print()

    oracle = DifficultyOracle(
        target_block_interval=target_interval,
        retarget_interval=retarget_interval,
        pre_fork_difficulty=1.0,
        min_difficulty=0.0625,
        enable_eda=False,
    )

    oracle.initialize_fork('fork_a', initial_height=101)
    oracle.initialize_fork('fork_b', initial_height=101)

    fork_a_height = 101
    fork_b_height = 101

    print(f"{'Time':>6s}  {'fork_a blks':>8s}  {'fork_b blks':>8s}  "
          f"{'fork_a diff':>9s}  {'fork_b diff':>9s}  "
          f"{'fork_a CW':>9s}  {'fork_b CW':>9s}  {'Winner':>6s}")
    print("-" * 80)

    last_report = 0
    for tick in range(int(duration / tick_interval)):
        sim_time = tick * tick_interval

        # Check fork_a
        if oracle.should_mine_block('fork_a', fork_a_hashrate, tick_interval):
            fork_a_height += 1
            event = oracle.record_block('fork_a', sim_time, fork_a_height)
            if event:
                eda_str = " (EDA)" if event.is_eda else ""
                print(f"  >> fork_a RETARGET at {sim_time:.0f}s: "
                      f"{event.old_difficulty:.6f} -> {event.new_difficulty:.6f} "
                      f"(factor={event.adjustment_factor:.3f}){eda_str}")

        # Check fork_b
        if oracle.should_mine_block('fork_b', fork_b_hashrate, tick_interval):
            fork_b_height += 1
            event = oracle.record_block('fork_b', sim_time, fork_b_height)
            if event:
                eda_str = " (EDA)" if event.is_eda else ""
                print(f"  >> fork_b RETARGET at {sim_time:.0f}s: "
                      f"{event.old_difficulty:.6f} -> {event.new_difficulty:.6f} "
                      f"(factor={event.adjustment_factor:.3f}){eda_str}")

        # Report every 60 seconds
        if sim_time - last_report >= 60:
            winner = oracle.get_winning_fork()[0]
            fork_a_state = oracle.forks['fork_a']
            fork_b_state = oracle.forks['fork_b']
            print(f"{sim_time:6.0f}s  {fork_a_state.blocks_mined:8d}  {fork_b_state.blocks_mined:8d}  "
                  f"{fork_a_state.current_difficulty:9.6f}  {fork_b_state.current_difficulty:9.6f}  "
                  f"{fork_a_state.cumulative_chainwork:9.4f}  {fork_b_state.cumulative_chainwork:9.4f}  "
                  f"{winner:>6s}")
            last_report = sim_time

    print()
    oracle.print_summary()

    # Verify key properties
    print("\nVerification:")
    fork_a_state = oracle.forks['fork_a']
    fork_b_state = oracle.forks['fork_b']

    expected_fork_a_rate = fork_a_hashrate / 100.0  # blocks per target_interval
    expected_fork_b_rate = fork_b_hashrate / 100.0

    actual_fork_a_rate = fork_a_state.blocks_mined / (duration / target_interval)
    actual_fork_b_rate = fork_b_state.blocks_mined / (duration / target_interval)

    print(f"  fork_a: expected ~{expected_fork_a_rate:.2f} blocks/interval, "
          f"got {actual_fork_a_rate:.2f}")
    print(f"  fork_b: expected ~{expected_fork_b_rate:.2f} blocks/interval, "
          f"got {actual_fork_b_rate:.2f}")

    winner_id, winner_cw, loser_cw = oracle.get_winning_fork()
    print(f"  Winning fork: {winner_id} "
          f"(chainwork {winner_cw:.4f} vs {loser_cw:.4f})")

    # Chainwork test: majority should win even if minority has more blocks post-adjustment
    print(f"  fork_a chain weight: {oracle.get_chain_weight('fork_a'):.4f}")
    print(f"  fork_b chain weight: {oracle.get_chain_weight('fork_b'):.4f}")

    # Export
    output_file = '/tmp/difficulty_oracle_test.json'
    oracle.export_to_json(output_file)
    print(f"\nResults exported to: {output_file}")

    # Test multi-block per tick scenario
    print("\n" + "=" * 70)
    print("MULTI-BLOCK PER TICK TEST")
    print("=" * 70)

    # Create oracle with very low difficulty to trigger multi-block
    multi_oracle = DifficultyOracle(
        target_block_interval=1.0,  # 1 second target
        retarget_interval=100,
        pre_fork_difficulty=0.1,    # Low difficulty = fast blocks
        min_difficulty=0.01,
    )
    multi_oracle.initialize_fork('test', initial_height=0)

    # With 100% hashrate and difficulty 0.1:
    # expected_interval = 1.0 * (0.1 / 1.0) = 0.1 seconds
    # With 1.0s tick: expected_blocks = 1.0 / 0.1 = 10 blocks per tick!

    print(f"Target interval: 1.0s, Difficulty: 0.1, Hashrate: 100%")
    print(f"Expected blocks per tick (1s): {1.0 / (1.0 * (0.1 / 1.0)):.1f}")
    print()

    # Run 10 ticks and count total blocks
    total_blocks = 0
    for tick in range(10):
        blocks = multi_oracle.get_blocks_to_mine('test', 100.0, 1.0)
        total_blocks += blocks
        print(f"  Tick {tick+1}: {blocks} blocks")

    print(f"\nTotal blocks in 10 ticks: {total_blocks}")
    print(f"Expected ~100 blocks (10 per tick × 10 ticks)")
    print(f"Actual rate: {total_blocks / 10:.1f} blocks/tick")
