#!/usr/bin/env python3

"""
Mining Pool Strategy - Individual Pool Decision Engine

Models realistic mining pool behavior with dual motivations:
1. Rational Economics - maximize profitability
2. Ideology/Preference - support preferred fork even at economic cost

Enables research questions like:
- How long can ideological miners sustain a fork?
- What's the cost of supporting a losing fork?
- When do rational economics override ideology?
"""

import time
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from enum import Enum
import json


class ForkPreference(Enum):
    """Fork preference for mining pools"""
    V27 = "fork_a"
    V26 = "fork_b"
    NEUTRAL = "neutral"  # No preference, purely profit-driven


@dataclass
class PoolProfile:
    """
    Profile for a mining pool with economic and ideological characteristics.

    Attributes:
        pool_id: Unique identifier
        hashrate_pct: Percentage of network hashrate (0-100)
        fork_preference: Which fork they prefer (fork_a, fork_b, or neutral)
        ideology_strength: How much profitability they'll sacrifice (0.0-1.0)
                          0.0 = purely rational (never mine at loss)
                          1.0 = ideology at any cost
        profitability_threshold: Minimum profit advantage to switch (0.0-1.0)
                                0.05 = must be 5% more profitable
        max_loss_usd: Maximum USD loss before forced switch (absolute cap)
        max_loss_pct: Maximum % of potential revenue they'll sacrifice
        initial_fork: Which fork the pool starts mining on. Defaults to fork_preference
                      (or 'fork_a' for neutral). Can differ from fork_preference to model
                      pools that are currently on one chain but ideologically favor another.
        current_fork: Track current fork for reorg detection
    """
    pool_id: str
    hashrate_pct: float
    fork_preference: ForkPreference
    ideology_strength: float = 0.0
    profitability_threshold: float = 0.05
    max_loss_usd: Optional[float] = None
    max_loss_pct: Optional[float] = 0.10  # 10% default
    initial_fork: Optional[str] = None    # If None, derived from fork_preference at init
    current_fork: str = 'fork_a'  # Track current fork for reorg detection


@dataclass
class MiningDecision:
    """
    Record of a mining pool's decision at a point in time.
    """
    timestamp: float
    pool_id: str
    chosen_fork: str
    fork_a_profitability_usd: float
    fork_b_profitability_usd: float
    rational_choice: str  # Which fork is objectively more profitable
    ideology_override: bool  # True if mining against profitability
    opportunity_cost_usd: float  # How much money left on table
    cumulative_cost_usd: float  # Total opportunity cost so far
    reason: str  # Human-readable explanation


class MiningPoolStrategy:
    """
    Manages individual mining pool decisions with dual motivations.

    Each pool independently decides which fork to mine based on:
    1. Profitability (rational economics)
    2. Fork preference (ideology/values)
    3. Loss tolerance (economic sustainability)
    """

    def __init__(self, pools: List[PoolProfile]):
        """
        Initialize with pool profiles.

        Args:
            pools: List of mining pool profiles
        """
        self.pools = {p.pool_id: p for p in pools}

        # Current mining allocation
        self.current_allocation = {p.pool_id: None for p in pools}

        # Decision history
        self.decision_history: List[MiningDecision] = []

        # Cost tracking per pool
        self.pool_costs = {
            p.pool_id: {
                'actual_revenue_usd': 0.0,
                'optimal_revenue_usd': 0.0,
                'cumulative_opportunity_cost_usd': 0.0,
                'forced_switch_count': 0,
                'ideology_override_count': 0
            }
            for p in pools
        }

        # Last decision time per pool (for cooldown)
        self.last_decision_time = {p.pool_id: 0.0 for p in pools}

        # Decision cooldown (prevent rapid switching)
        self.decision_interval = 600  # 10 minutes

    def calculate_pool_profitability(
        self,
        pool: PoolProfile,
        fork_id: str,
        price_oracle,
        fee_oracle,
        block_subsidy: float = 3.125,
        mining_cost_usd: float = 100000.0,
        difficulty_oracle=None,
        assumed_fork_hashrate: float = None,
    ) -> float:
        """
        Calculate profitability for a pool mining a specific fork.

        Args:
            pool: Pool profile
            fork_id: Which fork ('fork_a' or 'fork_b')
            price_oracle: Price oracle instance
            fee_oracle: Fee oracle instance
            block_subsidy: Block reward in BTC
            mining_cost_usd: Cost to mine one block
            difficulty_oracle: Optional DifficultyOracle instance. When provided,
                uses difficulty-derived blocks_per_hour instead of fixed 6.0.
            assumed_fork_hashrate: Override fork hashrate for "what if" scenarios.
                If None, uses current allocation (which can cause feedback loops).
                For pool decisions, pass 50.0 to assume balanced competition.

        Returns:
            Expected USD profit per hour for this pool on this fork
        """
        # Get current price and fees
        price_usd = price_oracle.get_price(fork_id)
        fee_rate = fee_oracle.get_fee(fork_id)

        # Calculate revenue per block
        fee_btc = (fee_rate * 1_000_000) / 100_000_000  # 1MB block
        revenue_per_block_btc = block_subsidy + fee_btc
        revenue_per_block_usd = revenue_per_block_btc * price_usd

        # Calculate expected blocks per hour for this pool
        if difficulty_oracle is not None:
            # Use assumed hashrate or current allocation
            if assumed_fork_hashrate is not None:
                fork_hashrate = assumed_fork_hashrate
            else:
                fork_hashrate = sum(
                    p.hashrate_pct for p_id, p in self.pools.items()
                    if self.current_allocation.get(p_id) == fork_id
                )
            fork_hashrate = max(fork_hashrate, 0.1)  # Avoid division by zero
            fork_bph = difficulty_oracle.get_blocks_per_hour(fork_id, fork_hashrate)
            # Pool's share of blocks on this fork
            blocks_per_hour = fork_bph * (pool.hashrate_pct / max(fork_hashrate, pool.hashrate_pct))
        else:
            # Assumes 6 blocks/hour total, pool gets (hashrate_pct / 100) of them
            blocks_per_hour = 6.0 * (pool.hashrate_pct / 100.0)

        # Calculate profit per hour
        revenue_per_hour = blocks_per_hour * revenue_per_block_usd
        cost_per_hour = blocks_per_hour * mining_cost_usd
        profit_per_hour = revenue_per_hour - cost_per_hour

        return profit_per_hour

    def make_decision(
        self,
        pool_id: str,
        current_time: float,
        price_oracle,
        fee_oracle,
        force_decision: bool = False,
        difficulty_oracle=None,
    ) -> Tuple[str, MiningDecision]:
        """
        Pool decides which fork to mine based on profitability and ideology.

        Args:
            pool_id: Which pool is deciding
            current_time: Current timestamp
            price_oracle: Price oracle instance
            fee_oracle: Fee oracle instance
            force_decision: Ignore cooldown and make decision now
            difficulty_oracle: Optional DifficultyOracle instance for
                difficulty-aware profitability calculations

        Returns:
            Tuple of (chosen_fork, decision_record)
        """
        pool = self.pools[pool_id]

        # Check cooldown
        if not force_decision:
            time_since_last = current_time - self.last_decision_time[pool_id]
            if time_since_last < self.decision_interval:
                # Keep current choice
                current_choice = self.current_allocation[pool_id]
                if current_choice is None:
                    # First decision: use explicit initial_fork if set,
                    # otherwise fall back to fork_preference
                    if pool.initial_fork is not None:
                        current_choice = pool.initial_fork
                    elif pool.fork_preference == ForkPreference.NEUTRAL:
                        current_choice = 'fork_a'  # Default
                    else:
                        current_choice = pool.fork_preference.value
                    self.current_allocation[pool_id] = current_choice

                return current_choice, None

        # Calculate profitability on each fork
        # IMPORTANT: Use assumed 50% hashrate on each fork for fair comparison
        # This prevents feedback loops where low allocation -> low profitability -> lower allocation
        # The pool is comparing: "If I mine fork_a vs fork_b, which is better FOR ME?"
        # Not: "Which fork currently has more hashrate?"
        fork_a_profit = self.calculate_pool_profitability(
            pool, 'fork_a', price_oracle, fee_oracle,
            difficulty_oracle=difficulty_oracle,
            assumed_fork_hashrate=50.0  # Assume balanced competition for fair comparison
        )
        fork_b_profit = self.calculate_pool_profitability(
            pool, 'fork_b', price_oracle, fee_oracle,
            difficulty_oracle=difficulty_oracle,
            assumed_fork_hashrate=50.0  # Assume balanced competition for fair comparison
        )

        # Determine rational choice (purely profit-driven)
        if fork_a_profit > fork_b_profit:
            rational_choice = 'fork_a'
            profit_advantage = (fork_a_profit - fork_b_profit) / fork_b_profit if fork_b_profit > 0 else 1.0
        else:
            rational_choice = 'fork_b'
            profit_advantage = (fork_b_profit - fork_a_profit) / fork_a_profit if fork_a_profit > 0 else 1.0

        # Apply ideology if pool has preference
        chosen_fork = rational_choice
        ideology_override = False
        reason = "Rational profit maximization"

        if pool.fork_preference != ForkPreference.NEUTRAL:
            preferred_fork = pool.fork_preference.value

            # Check if preferred fork is less profitable
            if preferred_fork != rational_choice:
                # Calculate loss if mining preferred fork
                if preferred_fork == 'fork_a':
                    opportunity_cost = fork_b_profit - fork_a_profit
                    loss_pct = (fork_b_profit - fork_a_profit) / fork_b_profit if fork_b_profit > 0 else 0
                else:
                    opportunity_cost = fork_a_profit - fork_b_profit
                    loss_pct = (fork_a_profit - fork_b_profit) / fork_a_profit if fork_a_profit > 0 else 0

                # Check if ideology is strong enough to accept this loss
                max_acceptable_loss_pct = pool.ideology_strength * (pool.max_loss_pct or 1.0)

                cumulative_cost = self.pool_costs[pool_id]['cumulative_opportunity_cost_usd']

                # Decision: Mine preferred fork despite lower profitability?
                can_afford_ideology = True

                # Check absolute loss limit
                if pool.max_loss_usd is not None:
                    if cumulative_cost + opportunity_cost > pool.max_loss_usd:
                        can_afford_ideology = False
                        reason = f"Forced switch: exceeded max loss ${pool.max_loss_usd:,.0f}"
                        self.pool_costs[pool_id]['forced_switch_count'] += 1

                # Check percentage loss limit
                if can_afford_ideology and loss_pct > max_acceptable_loss_pct:
                    can_afford_ideology = False
                    reason = f"Forced switch: loss {loss_pct*100:.1f}% exceeds tolerance {max_acceptable_loss_pct*100:.1f}%"
                    self.pool_costs[pool_id]['forced_switch_count'] += 1

                # Make ideological choice if affordable
                if can_afford_ideology:
                    chosen_fork = preferred_fork
                    ideology_override = True
                    reason = f"Ideology: supporting {preferred_fork} (loss {loss_pct*100:.1f}% < tolerance {max_acceptable_loss_pct*100:.1f}%)"
                    self.pool_costs[pool_id]['ideology_override_count'] += 1
            else:
                # Preferred fork IS more profitable - ideology and profit aligned!
                reason = "Ideology and profit aligned"

        # Calculate opportunity cost of this decision
        if chosen_fork == 'fork_a':
            opportunity_cost = max(0, fork_b_profit - fork_a_profit)
        else:
            opportunity_cost = max(0, fork_a_profit - fork_b_profit)

        # Update cost tracking
        self.pool_costs[pool_id]['cumulative_opportunity_cost_usd'] += opportunity_cost
        cumulative_cost = self.pool_costs[pool_id]['cumulative_opportunity_cost_usd']

        # Record decision
        decision = MiningDecision(
            timestamp=current_time,
            pool_id=pool_id,
            chosen_fork=chosen_fork,
            fork_a_profitability_usd=fork_a_profit,
            fork_b_profitability_usd=fork_b_profit,
            rational_choice=rational_choice,
            ideology_override=ideology_override,
            opportunity_cost_usd=opportunity_cost,
            cumulative_cost_usd=cumulative_cost,
            reason=reason
        )

        self.decision_history.append(decision)
        self.current_allocation[pool_id] = chosen_fork
        self.last_decision_time[pool_id] = current_time

        return chosen_fork, decision

    def calculate_hashrate_allocation(
        self,
        current_time: float,
        price_oracle,
        fee_oracle,
        difficulty_oracle=None,
    ) -> Tuple[float, float]:
        """
        Calculate total hashrate allocation across all pools.

        Args:
            current_time: Current timestamp
            price_oracle: Price oracle instance
            fee_oracle: Fee oracle instance
            difficulty_oracle: Optional DifficultyOracle instance

        Returns:
            Tuple of (fork_a_hashrate_pct, fork_b_hashrate_pct)
        """
        fork_a_hashrate = 0.0
        fork_b_hashrate = 0.0

        for pool_id, pool in self.pools.items():
            chosen_fork, decision = self.make_decision(
                pool_id, current_time, price_oracle, fee_oracle,
                difficulty_oracle=difficulty_oracle,
            )

            if chosen_fork == 'fork_a':
                fork_a_hashrate += pool.hashrate_pct
            else:
                fork_b_hashrate += pool.hashrate_pct

        return fork_a_hashrate, fork_b_hashrate

    def get_pool_summary(self, pool_id: str) -> Dict:
        """Get summary of pool's decisions and costs"""
        pool = self.pools[pool_id]
        costs = self.pool_costs[pool_id]

        # Get recent decisions
        recent_decisions = [
            d for d in self.decision_history[-10:]
            if d.pool_id == pool_id
        ]

        return {
            'pool_id': pool_id,
            'hashrate_pct': pool.hashrate_pct,
            'fork_preference': pool.fork_preference.value,
            'ideology_strength': pool.ideology_strength,
            'current_allocation': self.current_allocation[pool_id],
            'cumulative_opportunity_cost_usd': costs['cumulative_opportunity_cost_usd'],
            'ideology_override_count': costs['ideology_override_count'],
            'forced_switch_count': costs['forced_switch_count'],
            'recent_decisions': [asdict(d) for d in recent_decisions]
        }

    def print_allocation_summary(self):
        """Print current hashrate allocation and pool costs"""
        print("\n" + "="*70)
        print("MINING POOL ALLOCATION")
        print("="*70)

        fork_a_total = sum(
            p.hashrate_pct for p_id, p in self.pools.items()
            if self.current_allocation[p_id] == 'fork_a'
        )
        fork_b_total = sum(
            p.hashrate_pct for p_id, p in self.pools.items()
            if self.current_allocation[p_id] == 'fork_b'
        )

        print(f"\nTotal Hashrate Allocation:")
        print(f"  fork_a: {fork_a_total:.1f}%")
        print(f"  fork_b: {fork_b_total:.1f}%")

        print(f"\nIndividual Pool Decisions:")
        for pool_id, pool in self.pools.items():
            current = self.current_allocation[pool_id]
            costs = self.pool_costs[pool_id]
            pref_marker = ""
            if pool.fork_preference != ForkPreference.NEUTRAL:
                pref_marker = f" (prefers {pool.fork_preference.value})"

            print(f"\n  {pool_id} ({pool.hashrate_pct:.1f}% hashrate){pref_marker}:")
            print(f"    Mining: {current}")
            print(f"    Opportunity cost: ${costs['cumulative_opportunity_cost_usd']:,.0f}")
            print(f"    Ideology overrides: {costs['ideology_override_count']}")
            print(f"    Forced switches: {costs['forced_switch_count']}")

        print("\n" + "="*70)

    def export_to_json(self, output_path: str):
        """Export decision history and costs to JSON"""
        export_data = {
            'pools': {
                pool_id: {
                    'profile': asdict(pool),
                    'costs': self.pool_costs[pool_id],
                    'current_allocation': self.current_allocation[pool_id]
                }
                for pool_id, pool in self.pools.items()
            },
            'decision_history': [asdict(d) for d in self.decision_history]
        }

        # Convert enums to strings
        for pool_data in export_data['pools'].values():
            if 'fork_preference' in pool_data['profile']:
                pool_data['profile']['fork_preference'] = pool_data['profile']['fork_preference'].value

        with open(output_path, 'w') as f:
            json.dump(export_data, f, indent=2)


def create_realistic_pool_distribution() -> List[PoolProfile]:
    """
    Create realistic mining pool distribution based on current Bitcoin landscape.

    Represents major pools with different ideological stances.
    """
    return [
        # Large pools - more rational/profit-driven
        PoolProfile(
            pool_id="foundry",
            hashrate_pct=28.0,
            fork_preference=ForkPreference.NEUTRAL,
            ideology_strength=0.0,
            profitability_threshold=0.03  # Will switch for 3% advantage
        ),
        PoolProfile(
            pool_id="antpool",
            hashrate_pct=18.0,
            fork_preference=ForkPreference.NEUTRAL,
            ideology_strength=0.0,
            profitability_threshold=0.05
        ),

        # Medium pools - some ideological preference
        PoolProfile(
            pool_id="f2pool",
            hashrate_pct=15.0,
            fork_preference=ForkPreference.V27,  # Prefers newer version
            ideology_strength=0.3,  # Moderate ideology
            profitability_threshold=0.05,
            max_loss_usd=1_000_000,  # Will sacrifice up to $1M
            max_loss_pct=0.15  # Or 15% of potential revenue
        ),
        PoolProfile(
            pool_id="viabtc",
            hashrate_pct=12.0,
            fork_preference=ForkPreference.V26,  # Prefers older/conservative
            ideology_strength=0.5,  # Strong ideology
            profitability_threshold=0.08,
            max_loss_usd=2_000_000,  # Willing to sacrifice more
            max_loss_pct=0.20
        ),

        # Smaller pools - potentially more ideological
        PoolProfile(
            pool_id="braiins",
            hashrate_pct=10.0,
            fork_preference=ForkPreference.V27,
            ideology_strength=0.4,
            profitability_threshold=0.10,
            max_loss_usd=500_000,
            max_loss_pct=0.10
        ),
        PoolProfile(
            pool_id="binance",
            hashrate_pct=8.0,
            fork_preference=ForkPreference.NEUTRAL,
            ideology_strength=0.0,  # Purely rational (exchange)
            profitability_threshold=0.02  # Very quick to switch
        ),

        # Ideological small pool
        PoolProfile(
            pool_id="community_pool",
            hashrate_pct=5.0,
            fork_preference=ForkPreference.V27,
            ideology_strength=0.8,  # Very strong ideology
            profitability_threshold=0.20,  # Rarely switches
            max_loss_usd=200_000,
            max_loss_pct=0.30  # Will sacrifice a lot
        ),

        # Unknown/other pools
        PoolProfile(
            pool_id="other",
            hashrate_pct=4.0,
            fork_preference=ForkPreference.NEUTRAL,
            ideology_strength=0.1,
            profitability_threshold=0.05
        ),
    ]


def load_pools_from_config(config_path: str, scenario_name: str) -> List[PoolProfile]:
    """
    Load mining pool configuration from YAML file.

    Args:
        config_path: Path to mining_pools_config.yaml
        scenario_name: Which scenario to load (e.g., 'realistic_current')

    Returns:
        List of PoolProfile objects
    """
    import yaml

    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    if scenario_name not in config:
        raise ValueError(f"Scenario '{scenario_name}' not found in config")

    scenario = config[scenario_name]
    pools = []

    for pool_data in scenario['pools']:
        # Convert fork_preference string to enum
        pref_str = pool_data['fork_preference'].lower()
        if pref_str == 'fork_a':
            pref = ForkPreference.V27
        elif pref_str == 'fork_b':
            pref = ForkPreference.V26
        else:
            pref = ForkPreference.NEUTRAL

        pools.append(PoolProfile(
            pool_id=pool_data['pool_id'],
            hashrate_pct=pool_data['hashrate_pct'],
            fork_preference=pref,
            ideology_strength=pool_data['ideology_strength'],
            profitability_threshold=pool_data['profitability_threshold'],
            max_loss_usd=pool_data.get('max_loss_usd'),
            max_loss_pct=pool_data.get('max_loss_pct', 0.10)
        ))

    return pools


if __name__ == "__main__":
    """Test the mining pool strategy system"""

    print("="*70)
    print("MINING POOL STRATEGY TEST")
    print("="*70)

    # Create realistic pool distribution
    pools = create_realistic_pool_distribution()

    print("\nPool Profiles:")
    total_hashrate = sum(p.hashrate_pct for p in pools)
    print(f"Total hashrate: {total_hashrate:.1f}%\n")

    for pool in pools:
        pref_str = pool.fork_preference.value if pool.fork_preference != ForkPreference.NEUTRAL else "neutral"
        ideology_str = f"{pool.ideology_strength*100:.0f}%" if pool.ideology_strength > 0 else "none"

        print(f"{pool.pool_id:20s} {pool.hashrate_pct:5.1f}% | "
              f"Preference: {pref_str:7s} | Ideology: {ideology_str:5s}")

    print(f"\n✓ Mining pool strategy system ready for integration")
    print("="*70)
