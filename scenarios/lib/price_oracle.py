#!/usr/bin/env python3
"""
Price Oracle - Token Price Tracking for Fork Scenarios

Tracks and manages token prices for each fork (fork_a/fork_b) during partition tests.
Implements price divergence based on chain fundamentals (hashrate, economic weight, block production).

Usage:
    from price_oracle import PriceOracle

    oracle = PriceOracle(base_price=60000)
    oracle.update_price('fork_a', chain_weight=0.5, economic_weight=0.9, hashrate_weight=0.1)
    fork_a_price = oracle.get_price('fork_a')
"""

import json
import time
from math import exp, log
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, asdict
import threading


@dataclass
class PricePoint:
    """Single price observation at a point in time"""
    timestamp: float
    chain_id: str
    price: float
    metadata: Optional[Dict] = None


class PriceOracle:
    """
    Tracks token prices for forked chains with economic-based divergence.

    Price Model:
        new_price = base_price × chain_weight_factor × economic_factor × hashrate_factor

    Where factors are derived from:
        - chain_weight: Relative block production (blocks behind = sell pressure)
        - economic_weight: Custody + volume on chain (buying support)
        - hashrate_weight: Security premium for higher hashrate
    """

    def __init__(
        self,
        base_price: float = 60000,
        max_divergence: float = 0.20,
        chain_weight_coef: float = 0.3,
        economic_weight_coef: float = 0.5,
        hashrate_weight_coef: float = 0.2,
        storage_path: Optional[str] = None,
        min_fork_depth: int = 6,
        debug: bool = False,
        liveness_penalty: bool = False,
        liveness_exponent: float = 1.0,
        # Proposal 1 (Kristoufek 2015): EMA lag on economic weight
        use_economic_ema: bool = False,
        economic_ema_alpha: float = 0.15,
        # Proposal 2 (Biais et al. 2019 / Metcalfe): Sigmoid factor mapping
        use_sigmoid: bool = False,
        sigmoid_steepness_k: float = 6.0,
        # Proposal 3 (Hayes 2019): Asymmetric cost-of-production price floor
        use_cost_floor: bool = False,
        cost_floor_margin_buffer: float = 0.05,
    ):
        """
        Initialize price oracle.

        Args:
            base_price: Starting price for both chains (USD)
            max_divergence: Maximum price divergence allowed (0.20 = ±20%)
            chain_weight_coef: Weight for block production factor (0-1)
            economic_weight_coef: Weight for economic factor (0-1)
            hashrate_weight_coef: Weight for hashrate factor (0-1)
            storage_path: Path to store price history (JSON file)
            min_fork_depth: Minimum fork depth to consider sustained (default: 6)
            liveness_penalty: If True, decay the economic factor by block production
                rate. Chains producing 0 blocks/hour get no economic premium/discount.
                Raises max_divergence to 0.50 to allow larger swings.
            liveness_exponent: Exponent for production ratio decay (1.0 = linear).
                Higher values make the penalty kick in harder at low production.
            use_economic_ema: If True, apply EMA smoothing to economic weight before
                computing the economic factor. Introduces realistic lag — market prices
                respond gradually to custody shifts rather than instantaneously.
                (Kristoufek 2015)
            economic_ema_alpha: EMA smoothing factor (0=no update, 1=no lag).
                Default 0.15 gives half-life of ~4.3 update cycles (~4 min at 60s interval).
            use_sigmoid: If True, replace the linear [0.8, 1.2] factor mapping with a
                logistic sigmoid. Produces sharper minority-chain collapse at extreme
                weights while remaining nearly linear in the central 40-60% range.
                Applied to the economic factor only. (Biais et al. 2019, Metcalfe)
            sigmoid_steepness_k: Steepness of the sigmoid curve. k=4 is gentle,
                k=6 is moderate (default), k=10 is near-step.
            use_cost_floor: If True, replace the symmetric ±max_divergence floor with
                a per-fork cost-of-production floor scaled by hashrate share. A fork
                at 10% hashrate has ~10% of difficulty and a proportionally lower
                breakeven price. (Hayes 2019)
            cost_floor_margin_buffer: Safety buffer below breakeven before floor
                activates (0.05 = 5% below breakeven). Default 0.05.
        """
        self.base_price = base_price
        # Raise divergence cap when liveness penalty is active so that a dead
        # chain (0 blocks) can actually fall below the normal ±20% floor.
        self.max_divergence = 0.50 if liveness_penalty else max_divergence
        self.liveness_penalty = liveness_penalty
        self.liveness_exponent = liveness_exponent

        # Proposal 1: EMA lag
        self.use_economic_ema = use_economic_ema
        self.economic_ema_alpha = economic_ema_alpha
        self._ema_economic: Dict[str, Optional[float]] = {'fork_a': None, 'fork_b': None}

        # Proposal 2: Sigmoid factor mapping
        self.use_sigmoid = use_sigmoid
        self.sigmoid_steepness_k = sigmoid_steepness_k

        # Proposal 3: Asymmetric cost floor
        self.use_cost_floor = use_cost_floor
        self.cost_floor_margin_buffer = cost_floor_margin_buffer

        # Model coefficients (should sum to ~1.0)
        self.chain_weight_coef = chain_weight_coef
        self.economic_weight_coef = economic_weight_coef
        self.hashrate_weight_coef = hashrate_weight_coef

        # Validate coefficients
        total = chain_weight_coef + economic_weight_coef + hashrate_weight_coef
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"Coefficients should sum to 1.0, got {total:.2f}")

        # Current prices
        self.prices: Dict[str, float] = {
            'fork_a': base_price,
            'fork_b': base_price
        }

        # Price history
        self.history: List[PricePoint] = []

        # Sustained fork detection
        self.min_fork_depth = min_fork_depth
        self.fork_sustained = False
        self.fork_start_height = None  # Min height when fork detected
        self.fork_sustained_at = None  # When fork became sustained

        # Storage
        self.storage_path = Path(storage_path) if storage_path else None
        self._lock = threading.Lock()

        # Debug mode
        self.debug = debug

        # Initialize history with starting prices
        start_time = time.time()
        self.history.append(PricePoint(start_time, 'fork_a', base_price, {'initial': True}))
        self.history.append(PricePoint(start_time, 'fork_b', base_price, {'initial': True}))

    def _compute_factor(self, weight: float, use_sigmoid: bool = False) -> float:
        """
        Map a weight (0-1) to a price factor (0.8-1.2).

        Linear (current default):
            factor = 0.8 + (weight × 0.4)
            weight=0.5 → 1.00, weight=1.0 → 1.20, weight=0.0 → 0.80

        Sigmoid (Proposal 2):
            sig = 1 / (1 + exp(-k × (weight - 0.5)))
            factor = 0.8 + (sig × 0.4)
            Identical to linear at weight=0.5, but curves away more steeply
            outside the 40-60% range — producing sharper minority-chain collapse.
        """
        if use_sigmoid:
            sig = 1.0 / (1.0 + exp(-self.sigmoid_steepness_k * (weight - 0.5)))
            return 0.8 + (sig * 0.4)
        else:
            return 0.8 + (weight * 0.4)

    def get_price(self, chain_id: str) -> float:
        """
        Get current price for a chain.

        Args:
            chain_id: 'fork_a' or 'fork_b'

        Returns:
            Current price in USD
        """
        with self._lock:
            return self.prices.get(chain_id, self.base_price)

    def get_price_ratio(self) -> float:
        """
        Get current price ratio (fork_a/fork_b).

        Returns:
            Price ratio, where >1.0 means fork_a is more valuable
        """
        fork_a_price = self.get_price('fork_a')
        fork_b_price = self.get_price('fork_b')
        return fork_a_price / fork_b_price if fork_b_price > 0 else 1.0

    def check_fork_sustained(
        self,
        fork_a_height: int,
        fork_b_height: int,
        common_ancestor_height: int
    ) -> bool:
        """
        Check if fork is sustained (deep enough for separate token valuation).

        Natural chain splits happen frequently and resolve quickly.
        Only sustained protocol forks should create separate token prices.

        Args:
            fork_a_height: Current fork_a block height
            fork_b_height: Current fork_b block height
            common_ancestor_height: Height of last common block

        Returns:
            True if fork is sustained, False otherwise
        """
        with self._lock:
            # Calculate total fork depth (blocks mined on both chains since split)
            fork_depth = (fork_a_height + fork_b_height) - (2 * common_ancestor_height)

            # First detection
            if self.fork_start_height is None:
                self.fork_start_height = common_ancestor_height

            # Check if sustained
            if not self.fork_sustained and fork_depth >= self.min_fork_depth:
                self.fork_sustained = True
                self.fork_sustained_at = time.time()
                if self.debug:
                    print(f"  [PRICE DEBUG] Fork became SUSTAINED! depth={fork_depth} >= min={self.min_fork_depth}")
                return True

            if self.debug and not self.fork_sustained:
                print(f"  [PRICE DEBUG] Fork not yet sustained: depth={fork_depth} < min={self.min_fork_depth}")

            return self.fork_sustained

    def update_price(
        self,
        chain_id: str,
        chain_weight: float,
        economic_weight: float,
        hashrate_weight: float,
        metadata: Optional[Dict] = None,
        block_rate_ratio: Optional[float] = None,
    ) -> float:
        """
        Update price based on chain fundamentals.

        NOTE: Price divergence only occurs for SUSTAINED forks (depth >= min_fork_depth).
        Natural chain splits keep both tokens at base price until fork is sustained.

        Args:
            chain_id: 'fork_a' or 'fork_b'
            chain_weight: Relative chain strength (0-1, based on blocks produced)
                         0.5 = even, >0.5 = ahead, <0.5 = behind
            economic_weight: Economic node concentration (0-1)
                           0.5 = split evenly, >0.5 = majority on this chain
            hashrate_weight: Hashrate concentration (0-1)
                           0.5 = split evenly, >0.5 = majority on this chain
            metadata: Optional additional data to store with price point
            block_rate_ratio: Fraction of target block rate this chain is currently
                producing (0-1, where 1.0 = on-target). Used for liveness penalty
                when self.liveness_penalty=True. None disables the penalty for
                this call even if liveness_penalty is globally enabled.

        Returns:
            New price for the chain
        """
        # Check if fork is sustained
        if not self.fork_sustained:
            # Fork not yet sustained - keep price at base
            new_price = self.base_price
            if self.debug:
                print(f"  [PRICE DEBUG] {chain_id}: fork NOT sustained, price stays at ${new_price:,.0f}")
        else:
            # Fork is sustained - calculate price divergence

            # Proposal 2: Sigmoid vs linear factor mapping (Biais et al. 2019 / Metcalfe)
            # Sigmoid applied to economic factor only — the dominant 50% component.
            # Linear mapping used for chain and hashrate factors.
            chain_factor    = self._compute_factor(chain_weight,    use_sigmoid=False)
            economic_factor = self._compute_factor(economic_weight, use_sigmoid=self.use_sigmoid)
            hashrate_factor = self._compute_factor(hashrate_weight, use_sigmoid=False)

            # Liveness penalty: decay economic factor by block production rate.
            # A chain producing 0 blocks/hr collapses its economic_factor to neutral 1.0.
            # Formula: effective_econ = 1.0 + (raw_econ - 1.0) * production_ratio^exponent
            if self.liveness_penalty and block_rate_ratio is not None:
                effective_econ_factor = 1.0 + (economic_factor - 1.0) * (
                    block_rate_ratio ** self.liveness_exponent
                )
                if self.debug:
                    print(f"  [PRICE DEBUG] {chain_id}: liveness block_rate_ratio={block_rate_ratio:.3f} "
                          f"econ_factor {economic_factor:.3f}->{effective_econ_factor:.3f}")
            else:
                effective_econ_factor = economic_factor

            # Weighted combination
            combined_factor = (
                chain_factor * self.chain_weight_coef +
                effective_econ_factor * self.economic_weight_coef +
                hashrate_factor * self.hashrate_weight_coef
            )

            # Calculate new price
            new_price = self.base_price * combined_factor

            # Proposal 3: Asymmetric cost-of-production floor (Hayes 2019)
            # A fork with x% of hashrate has ~x% of difficulty → x% of mining cost.
            # Its token price floor = that proportional cost × (1 - margin_buffer),
            # well below the symmetric ±20% floor used for the majority chain.
            # This prevents ideology-driven miners from sustaining a near-worthless
            # minority chain indefinitely behind an artificially high price floor.
            if self.use_cost_floor:
                fork_floor = hashrate_weight * self.base_price * (1 - self.cost_floor_margin_buffer)
                if self.debug and fork_floor < self.base_price * (1 - self.max_divergence):
                    print(f"  [PRICE DEBUG] {chain_id}: cost floor ${fork_floor:,.0f} "
                          f"(hashrate_weight={hashrate_weight:.3f}) below symmetric floor "
                          f"${self.base_price * (1 - self.max_divergence):,.0f}")
            else:
                fork_floor = self.base_price * (1 - self.max_divergence)

            max_price = self.base_price * (1 + self.max_divergence)
            new_price = max(fork_floor, min(max_price, new_price))

            if self.debug:
                print(f"  [PRICE DEBUG] {chain_id}: chain={chain_weight:.3f} econ={economic_weight:.3f} "
                      f"hash={hashrate_weight:.3f}")
                print(f"  [PRICE DEBUG] {chain_id}: factors chain={chain_factor:.3f} "
                      f"econ={effective_econ_factor:.3f} hash={hashrate_factor:.3f}")
                print(f"  [PRICE DEBUG] {chain_id}: combined={combined_factor:.4f} -> ${new_price:,.0f} "
                      f"(floor=${fork_floor:,.0f})")

            # Proposal 1: EMA lag on price output (Kristoufek 2015)
            # Market prices respond gradually to fundamental changes — smooth the
            # output price rather than the input weights so the lag is visible in
            # the price signal that pool profitability and node switching reads.
            if self.use_economic_ema:
                prev_ema = self._ema_economic.get(chain_id)
                if prev_ema is None:
                    new_price = new_price  # seed: first call returns raw price
                else:
                    new_price = (
                        prev_ema * (1 - self.economic_ema_alpha)
                        + new_price * self.economic_ema_alpha
                    )
                self._ema_economic[chain_id] = new_price
                if self.debug:
                    print(f"  [PRICE DEBUG] {chain_id}: EMA price smoothed -> ${new_price:,.0f} "
                          f"(alpha={self.economic_ema_alpha})")

        # Update price
        with self._lock:
            self.prices[chain_id] = new_price

            # Record in history
            price_point = PricePoint(
                timestamp=time.time(),
                chain_id=chain_id,
                price=new_price,
                metadata=metadata or {}
            )
            self.history.append(price_point)

        return new_price

    def update_prices_from_state(
        self,
        fork_a_height: int,
        fork_b_height: int,
        fork_a_economic_pct: float,
        fork_b_economic_pct: float,
        fork_a_hashrate_pct: float,
        fork_b_hashrate_pct: float,
        common_ancestor_height: int,
        metadata: Optional[Dict] = None,
        fork_a_chain_weight_override: Optional[float] = None,
        fork_b_chain_weight_override: Optional[float] = None,
        fork_a_block_rate_ratio: Optional[float] = None,
        fork_b_block_rate_ratio: Optional[float] = None,
    ) -> Tuple[float, float]:
        """
        Update both chain prices from current network state.

        Args:
            fork_a_height: Current block height of fork_a chain
            fork_b_height: Current block height of fork_b chain
            fork_a_economic_pct: Percentage of economic weight on fork_a (0-100)
            fork_b_economic_pct: Percentage of economic weight on fork_b (0-100)
            fork_a_hashrate_pct: Percentage of hashrate on fork_a (0-100)
            fork_b_hashrate_pct: Percentage of hashrate on fork_b (0-100)
            common_ancestor_height: Height of last common block before fork
            metadata: Optional metadata to attach to price updates
            fork_a_chain_weight_override: If provided, use this instead of height-based
                chain weight for fork_a (0-1, from difficulty oracle chainwork)
            fork_b_chain_weight_override: If provided, use this instead of height-based
                chain weight for fork_b (0-1, from difficulty oracle chainwork)
            fork_a_block_rate_ratio: Fraction of target block rate fork_a is producing (0-1).
                Passed to update_price() for liveness penalty calculation.
            fork_b_block_rate_ratio: Fraction of target block rate fork_b is producing (0-1).

        Returns:
            Tuple of (fork_a_price, fork_b_price)
        """
        # Check if fork is sustained (only for sustained forks do prices diverge)
        just_became_sustained = self.check_fork_sustained(
            fork_a_height, fork_b_height, common_ancestor_height
        )

        # Log when fork becomes sustained
        if just_became_sustained and self.fork_sustained_at is not None:
            fork_depth = (fork_a_height + fork_b_height) - (2 * common_ancestor_height)
            # Note: Actual logging should be done by caller with proper logger
            # We'll add this info to metadata
            if metadata is None:
                metadata = {}
            metadata['fork_sustained'] = True
            metadata['fork_depth'] = fork_depth

        # Use chainwork-based overrides if provided, otherwise height-based
        if fork_a_chain_weight_override is not None and fork_b_chain_weight_override is not None:
            fork_a_chain_weight = fork_a_chain_weight_override
            fork_b_chain_weight = fork_b_chain_weight_override
        else:
            total_blocks = fork_a_height + fork_b_height
            fork_a_chain_weight = fork_a_height / total_blocks if total_blocks > 0 else 0.5
            fork_b_chain_weight = fork_b_height / total_blocks if total_blocks > 0 else 0.5

        # Economic weights (convert percentage to 0-1)
        fork_a_econ_weight = fork_a_economic_pct / 100.0
        fork_b_econ_weight = fork_b_economic_pct / 100.0

        # Hashrate weights (convert percentage to 0-1)
        fork_a_hash_weight = fork_a_hashrate_pct / 100.0
        fork_b_hash_weight = fork_b_hashrate_pct / 100.0

        # Update prices
        fork_a_price = self.update_price(
            'fork_a',
            fork_a_chain_weight,
            fork_a_econ_weight,
            fork_a_hash_weight,
            metadata,
            block_rate_ratio=fork_a_block_rate_ratio,
        )

        fork_b_price = self.update_price(
            'fork_b',
            fork_b_chain_weight,
            fork_b_econ_weight,
            fork_b_hash_weight,
            metadata,
            block_rate_ratio=fork_b_block_rate_ratio,
        )

        return fork_a_price, fork_b_price

    def get_price_history(
        self,
        chain_id: Optional[str] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None
    ) -> List[PricePoint]:
        """
        Get price history with optional filtering.

        Args:
            chain_id: Filter by chain ('fork_a' or 'fork_b'), None for all
            start_time: Filter prices after this timestamp
            end_time: Filter prices before this timestamp

        Returns:
            List of PricePoint objects
        """
        with self._lock:
            history = self.history.copy()

        # Apply filters
        if chain_id:
            history = [p for p in history if p.chain_id == chain_id]
        if start_time:
            history = [p for p in history if p.timestamp >= start_time]
        if end_time:
            history = [p for p in history if p.timestamp <= end_time]

        return history

    def get_price_timeline(self) -> Dict:
        """
        Get price history formatted as timelines for visualization.

        Returns:
            Dict with timestamps and price arrays:
            {
                'timestamps': [t0, t1, ...],
                'fork_a_prices': [p0, p1, ...],
                'fork_b_prices': [p0, p1, ...]
            }
        """
        fork_a_history = self.get_price_history('fork_a')
        fork_b_history = self.get_price_history('fork_b')

        # Get all unique timestamps
        all_times = sorted(set(
            [p.timestamp for p in fork_a_history] +
            [p.timestamp for p in fork_b_history]
        ))

        # Build price arrays (using last known price for each timestamp)
        fork_a_prices = []
        fork_b_prices = []

        fork_a_idx = 0
        fork_b_idx = 0

        for t in all_times:
            # Advance fork_a index to latest price <= t
            while fork_a_idx < len(fork_a_history) - 1 and fork_a_history[fork_a_idx + 1].timestamp <= t:
                fork_a_idx += 1

            # Advance fork_b index to latest price <= t
            while fork_b_idx < len(fork_b_history) - 1 and fork_b_history[fork_b_idx + 1].timestamp <= t:
                fork_b_idx += 1

            fork_a_prices.append(fork_a_history[fork_a_idx].price if fork_a_idx < len(fork_a_history) else self.base_price)
            fork_b_prices.append(fork_b_history[fork_b_idx].price if fork_b_idx < len(fork_b_history) else self.base_price)

        return {
            'timestamps': all_times,
            'fork_a_prices': fork_a_prices,
            'fork_b_prices': fork_b_prices
        }

    def export_to_json(self, output_path: str):
        """
        Export price history to JSON file.

        Args:
            output_path: Path to output JSON file
        """
        timeline = self.get_price_timeline()

        export_data = {
            'config': {
                'base_price': self.base_price,
                'max_divergence': self.max_divergence,
                'coefficients': {
                    'chain_weight': self.chain_weight_coef,
                    'economic_weight': self.economic_weight_coef,
                    'hashrate_weight': self.hashrate_weight_coef
                }
            },
            'current_prices': self.prices.copy(),
            'timeline': timeline,
            'history': [asdict(p) for p in self.history]
        }

        with open(output_path, 'w') as f:
            json.dump(export_data, f, indent=2)

    def save_to_disk(self):
        """Save price history to configured storage path"""
        if self.storage_path:
            self.export_to_json(str(self.storage_path))

    def print_summary(self):
        """Print current price state summary"""
        fork_a_price = self.get_price('fork_a')
        fork_b_price = self.get_price('fork_b')
        ratio = self.get_price_ratio()

        print("=" * 60)
        print("PRICE ORACLE SUMMARY")
        print("=" * 60)
        print(f"Fork Status: {'SUSTAINED' if self.fork_sustained else 'NOT SUSTAINED'}")
        if self.fork_sustained:
            print(f"  (Sustained fork enables separate token valuation)")
        else:
            print(f"  (Natural chain split - prices remain equal)")
        print(f"fork_a Price: ${fork_a_price:,.2f}")
        print(f"fork_b Price: ${fork_b_price:,.2f}")
        print(f"Price Ratio (fork_a/fork_b): {ratio:.4f}")
        print(f"Divergence: {abs(ratio - 1.0) * 100:.2f}%")
        print(f"Total price updates: {len(self.history)}")
        print("=" * 60)


def load_price_oracle_from_config(config: Dict) -> PriceOracle:
    """
    Create PriceOracle from configuration dictionary.

    Args:
        config: Configuration dict with keys:
            - base_price (optional, default 60000)
            - max_divergence (optional, default 0.20)
            - coefficients (optional, dict with chain_weight, economic_weight, hashrate_weight)
            - storage_path (optional)
            - min_fork_depth (optional, default 6)

    Returns:
        Configured PriceOracle instance
    """
    coeffs = config.get('coefficients', {})

    return PriceOracle(
        base_price=config.get('base_price', 60000),
        max_divergence=config.get('max_divergence', 0.20),
        chain_weight_coef=coeffs.get('chain_weight', 0.3),
        economic_weight_coef=coeffs.get('economic_weight', 0.5),
        hashrate_weight_coef=coeffs.get('hashrate_weight', 0.2),
        storage_path=config.get('storage_path'),
        min_fork_depth=config.get('min_fork_depth', 6)
    )


# Example usage and testing
if __name__ == '__main__':
    import sys

    # Create oracle
    oracle = PriceOracle(base_price=60000)

    print("Testing Price Oracle")
    print()

    # Simulate scenario: fork_a has economic advantage, fork_b has hashrate advantage
    print("Scenario: fork_a = 90% economic, 10% hashrate | fork_b = 10% economic, 90% hashrate")
    print()

    # Simulate over 2 hours (120 minutes)
    for minute in range(0, 121, 10):  # Every 10 minutes
        # Simulate block production
        # fork_b mines faster due to more hashrate
        fork_a_height = 101 + int(minute * 0.1)  # Slow mining
        fork_b_height = 101 + int(minute * 0.9)  # Fast mining

        # Update prices
        fork_a_price, fork_b_price = oracle.update_prices_from_state(
            fork_a_height=fork_a_height,
            fork_b_height=fork_b_height,
            fork_a_economic_pct=90.0,
            fork_b_economic_pct=10.0,
            fork_a_hashrate_pct=10.0,
            fork_b_hashrate_pct=90.0,
            metadata={'minute': minute}
        )

        ratio = fork_a_price / fork_b_price
        print(f"[{minute:3d}min] Heights: fork_a={fork_a_height} fork_b={fork_b_height} | "
              f"Prices: fork_a=${fork_a_price:,.0f} fork_b=${fork_b_price:,.0f} | "
              f"Ratio: {ratio:.4f}")

    print()
    oracle.print_summary()

    # Export data
    output_file = '/tmp/price_oracle_test.json'
    oracle.export_to_json(output_file)
    print(f"\n✓ Price history exported to: {output_file}")
