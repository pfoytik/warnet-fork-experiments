#!/usr/bin/env python3

"""
Bitcoin Fork Experiment — Warnet Community Framework

Simulates a contested protocol fork where two versions of bitcoind (Fork A and Fork B)
compete for hashrate, economic adoption, and network dominance.

How it works:
- Hashrate allocation is DYNAMIC (pools switch based on profitability + ideology)
- Economic weight is DYNAMIC (economic/user nodes switch based on price + ideology)
- Network topology stays STATIC (Fork A nodes isolated from Fork B nodes)

Feedback loop:
  Price Oracle → Pool decisions (hashrate changes)
       ↑          Economic/User decisions (economic weight changes)
       └──────────────────────┘

Network stays partitioned:
  Fork A nodes ✗✗✗ Fork B nodes (can't communicate)

But actors can switch which fork they support:
  Pools: choose which partition to mine on
  Economic nodes: choose which fork's economy to participate in
  User nodes: choose which fork to use

Configure fork identity with:
  --fork-a-name      Display name for Fork A (e.g., "taproot", "segwit", "v2")
  --fork-b-name      Display name for Fork B (e.g., "legacy", "v1")
  --fork-a-version   Version prefix in bitcoind subversion string (default: "27.")
  --fork-b-version   Version prefix in bitcoind subversion string (default: "26.")
"""

from time import sleep, time
from random import random, choices
import argparse
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from commander import Commander

# Import from lib modules (bundled with scenario)
import os
from lib.price_oracle import PriceOracle
from lib.fee_oracle import FeeOracle
from lib.mining_pool_strategy import (
    ForkPreference,
    PoolProfile,
    MiningPoolStrategy,
    load_pools_from_config,
)
from lib.economic_node_strategy import (
    EconomicNodeStrategy,
    EconomicNodeProfile,
    load_economic_nodes_from_network,
)
from lib.difficulty_oracle import DifficultyOracle
from lib.reorg_oracle import ReorgOracle


class ForkExperiment(Commander):
    """
    Partition mining with dynamic pool + economic strategy.

    All actors make independent fork decisions:
    - Mining pools: choose which fork to mine (hashrate allocation)
    - Economic nodes: choose which fork's economy to support (economic weight)
    - User nodes: choose which fork to use (economic weight, small contribution)

    Decisions are based on:
    1. Profitability / Price advantage (rational economics)
    2. Ideology (fork preference)
    3. Loss tolerance / Inertia (switching costs)
    """

    def set_test_params(self):
        """Initialize test parameters"""
        self.num_nodes = 0
        self.fork_a_nodes = []
        self.fork_b_nodes = []
        self.blocks_mined = {'fork_a': 0, 'fork_b': 0}

        # Oracles
        self.price_oracle = None
        self.fee_oracle = None
        self.pool_strategy = None
        self.economic_strategy = None
        self.difficulty_oracle = None
        self.reorg_oracle = None

        # Difficulty mode tick interval
        self.tick_interval = 1.0

        # Current hashrate allocation (dynamic - from pool decisions)
        self.current_fork_a_hashrate = 0.0
        self.current_fork_b_hashrate = 0.0

        # Current economic allocation (dynamic - from economic/user node decisions)
        self.current_fork_a_economic = 0.0
        self.current_fork_b_economic = 0.0

        # Transactional economic allocation (fee-generating activity only)
        # Distinguishes exchanges/merchants (high velocity) from HODLers (low velocity)
        self.current_fork_a_transactional = 0.0
        self.current_fork_b_transactional = 0.0

        # Solo miner hashrate (user/economic nodes that mine)
        self.current_fork_a_solo_hashrate = 0.0
        self.current_fork_b_solo_hashrate = 0.0

        # Pool-to-node mappings (per partition)
        self.pool_nodes_fork_a = {}  # pool_id -> list of fork_a nodes
        self.pool_nodes_fork_b = {}  # pool_id -> list of fork_b nodes

        # Node metadata from network.yaml
        self.node_metadata = {}  # node_name -> metadata dict

        # Asymmetric fork: nodes that accept blocks from the foreign fork
        # fork_b nodes have accepts_foreign_blocks=True and receive fork_a blocks via submitblock
        self.foreign_accepting_nodes = []

        # Dynamic partition switching: track peer lists and node partition assignments
        self.fork_a_peer_addresses = []  # List of fork_a node addresses for reconnection
        self.fork_b_peer_addresses = []  # List of fork_b node addresses for reconnection
        self.node_current_partition = {}  # node_name -> current partition ('fork_a' or 'fork_b')
        self.partition_switch_history = []  # Track all partition switches for analysis

        # Time-limited UASF: fork_a nodes enforce strict rules for a limited time
        self.uasf_active = True  # Whether UASF is currently active
        self.uasf_start_time = None  # When UASF started (simulation time)
        self.uasf_expiry_time = None  # When UASF expires (simulation time)
        self.uasf_expired = False  # Whether UASF has expired
        self.uasf_expiry_data = None  # Snapshot of state at expiry

        # Reunion state: after reunion, all hashrate goes to winning fork
        self.reunion_completed = False  # Whether reunion has successfully completed
        self.reunion_winner = None  # Winning fork after reunion ('fork_a' or 'fork_b')

        # Time series data for charting
        self.time_series = {
            'timestamps': [],           # elapsed seconds
            'fork_a_price': [],
            'fork_b_price': [],
            'fork_a_hashrate': [],
            'fork_b_hashrate': [],
            'fork_a_economic': [],
            'fork_b_economic': [],
            'fork_a_blocks': [],
            'fork_b_blocks': [],
            'fork_a_difficulty': [],
            'fork_b_difficulty': [],
            'fork_a_chainwork': [],
            'fork_b_chainwork': [],
            # Fee market metrics
            'fork_a_fee_rate': [],         # sats/vbyte
            'fork_b_fee_rate': [],
            'fork_a_fee_revenue_btc': [],  # fee revenue per block
            'fork_b_fee_revenue_btc': [],
            'fork_a_congestion': [],       # tx_volume / throughput ratio
            'fork_b_congestion': [],
            'fork_a_mempool_mb': [],       # estimated mempool size
            'fork_b_mempool_mb': [],
            # Transactional vs custodial activity
            'fork_a_transactional': [],    # fee-generating economic activity %
            'fork_b_transactional': [],
            # Solo miner hashrate (user nodes that mine)
            'fork_a_solo_hashrate': [],
            'fork_b_solo_hashrate': [],
            # Measured block time (rolling window between snapshots, sim-seconds per block)
            # Scale to real-world: multiply by (600 / target_block_interval)
            'fork_a_block_time_s': [],
            'fork_b_block_time_s': [],
        }

    def add_options(self, parser: argparse.ArgumentParser):
        """Add command-line arguments"""
        # Fork identity — customize these to match your node version strings
        parser.add_argument('--fork-a-name', type=str, default='fork_a',
                          help='Display name for Fork A (the new rules), e.g. "taproot", "segwit"')
        parser.add_argument('--fork-b-name', type=str, default='fork_b',
                          help='Display name for Fork B (the old rules), e.g. "legacy"')
        parser.add_argument('--fork-a-version', type=str, default='27.',
                          help='Version string prefix identifying Fork A nodes (default: "27.")')
        parser.add_argument('--fork-b-version', type=str, default='26.',
                          help='Version string prefix identifying Fork B nodes (default: "26.")')

        parser.add_argument('--fork-a-economic', type=float, default=70.0,
                          help='Economic weight on Fork A (0-100)')
        parser.add_argument('--fork-b-economic', type=float, default=None)
        parser.add_argument('--interval', type=int, default=10,
                          help='Block mining interval (seconds)')
        parser.add_argument('--duration', type=int, default=7200,
                          help='Test duration (seconds), default 2 hours')
        parser.add_argument('--start-height', type=int, default=101)

        # Pool configuration
        parser.add_argument('--pool-scenario', type=str, default='realistic_current',
                          help='Pool scenario from mining_pools_config.yaml')
        parser.add_argument('--initial-fork_a-hashrate', type=float, default=None,
                          help='Initial fork_a hashrate (if not using pools)')

        # Network configuration
        parser.add_argument('--network-yaml', type=str, default=None,
                          help='Path to network.yaml file with node metadata')

        # Economic node configuration
        parser.add_argument('--economic-scenario', type=str, default='realistic_current',
                          help='Economic node scenario from economic_nodes_config.yaml')
        parser.add_argument('--economic-update-interval', type=int, default=300,
                          help='Economic node decision interval (seconds), default 5min')

        # Update intervals
        parser.add_argument('--hashrate-update-interval', type=int, default=600,
                          help='Pool decision interval (seconds), default 10min')
        parser.add_argument('--price-update-interval', type=int, default=60,
                          help='Price update interval (seconds)')

        # Difficulty oracle configuration
        parser.add_argument('--enable-difficulty', action='store_true', default=False,
                          help='Enable difficulty simulation (probability-per-tick mining)')
        parser.add_argument('--retarget-interval', type=int, default=144,
                          help='Blocks between difficulty adjustments (default 144)')
        parser.add_argument('--tick-interval', type=float, default=1.0,
                          help='Tick interval in seconds for difficulty mode (default 1.0)')
        parser.add_argument('--enable-eda', action='store_true', default=False,
                          help='Enable Emergency Difficulty Adjustment (BCH-style)')
        parser.add_argument('--min-difficulty', type=float, default=0.0625,
                          help='Minimum difficulty floor (default 0.0625 = 1/16)')

        # Reorg metrics
        parser.add_argument('--enable-reorg-metrics', action='store_true', default=False,
                          help='Enable reorg tracking oracle for fork impact metrics')
        parser.add_argument('--enable-dynamic-switching', action='store_true', default=True,
                          help='Enable dynamic partition switching for economic/user nodes (default: True)')

        # Fork reunion
        parser.add_argument('--enable-reunion', action='store_true', default=False,
                          help='At end of duration, reconnect partitions and let the heavier chain reorg the loser')
        parser.add_argument('--reunion-timeout', type=int, default=120,
                          help='Seconds to wait for reorg convergence after reconnection (default 120)')

        # Time-limited UASF (User Activated Soft Fork)
        parser.add_argument('--uasf-duration', type=int, default=None,
                          help='UASF duration in seconds. After this time, fork_a nodes stop enforcing strict rules '
                               'and accept fork_b blocks. If not set, UASF runs indefinitely.')
        parser.add_argument('--uasf-expiry-action', type=str, default='reunion',
                          choices=['reunion', 'accept', 'continue'],
                          help='Action when UASF expires: '
                               'reunion=reconnect partitions and reorg to heavier chain, '
                               'accept=fork_a nodes accept fork_b blocks but stay partitioned, '
                               'continue=just log expiry and continue (default: reunion)')

        # Results management
        parser.add_argument('--results-id', type=str, default=None,
                          help='Unique identifier for this run (default: auto-generated timestamp)')
        parser.add_argument('--snapshot-interval', type=int, default=60,
                          help='Interval in seconds for time series snapshots (default: 60)')

        # Price model options
        parser.add_argument('--enable-liveness-penalty', action='store_true', default=False,
                          help='Enable Option B liveness penalty: decay economic factor by block '
                               'production rate. Dead chains (0 blocks/hr) lose their economic '
                               'premium/discount. Raises max_divergence cap to 50%%.')
        parser.add_argument('--use-economic-ema', action='store_true', default=False,
                          help='Proposal 1 (Kristoufek 2015): Apply EMA lag to economic weight. '
                               'Price responds gradually to custody shifts rather than instantly.')
        parser.add_argument('--economic-ema-alpha', type=float, default=0.15,
                          help='EMA smoothing factor for economic weight (default: 0.15). '
                               '0=no update, 1=no lag. Half-life ~4.3 cycles at 0.15.')
        parser.add_argument('--use-sigmoid', action='store_true', default=False,
                          help='Proposal 2 (Biais et al. 2019): Replace linear factor mapping '
                               'with logistic sigmoid. Produces sharper minority-chain collapse '
                               'at extreme economic weights. Applied to economic factor only.')
        parser.add_argument('--sigmoid-steepness', type=float, default=6.0,
                          help='Sigmoid steepness k (default: 6.0). k=4 gentle, k=6 moderate, k=10 near-step.')
        parser.add_argument('--use-cost-floor', action='store_true', default=False,
                          help='Proposal 3 (Hayes 2019): Replace symmetric price floor with '
                               'per-fork cost-of-production floor scaled by hashrate share. '
                               'Minority chains at low hashrate face proportionally lower price floors.')
        parser.add_argument('--cost-floor-margin-buffer', type=float, default=0.05,
                          help='Safety buffer below breakeven for cost floor (default: 0.05 = 5%%).')
        parser.add_argument('--max-price-divergence', type=float, default=None,
                          help='Max price divergence cap (e.g., 0.20 for ±20%%). Overrides default oracle cap.')

        # Debug options
        parser.add_argument('--debug-prices', action='store_true', default=False,
                          help='Enable verbose price calculation debugging')

    def load_network_metadata(self):
        """Load node metadata from network.yaml file or bundled config"""
        network_config = None

        # Try loading from specified path first
        if self.options.network_yaml:
            network_yaml_path = Path(self.options.network_yaml)
            if network_yaml_path.exists():
                self.log.info(f"Loading network metadata from {network_yaml_path}")
                try:
                    with open(network_yaml_path, 'r') as f:
                        network_config = yaml.safe_load(f)
                except Exception as e:
                    self.log.warning(f"Error loading network YAML from path: {e}")

        # Fallback: try loading from bundled config
        if network_config is None:
            try:
                import pkgutil
                bundled_data = pkgutil.get_data('config', 'network_metadata.yaml')
                if bundled_data:
                    network_config = yaml.safe_load(bundled_data.decode('utf-8'))
                    self.log.info("Loading network metadata from bundled config")
            except Exception as e:
                self.log.debug(f"No bundled network metadata: {e}")

        # If we loaded config, check for accepts_foreign_blocks and infer from version if missing
        if network_config:
            nodes = network_config.get('nodes', [])
            foreign_blocks_set = any(
                n.get('metadata', {}).get('accepts_foreign_blocks') is not None
                for n in nodes
            )
            if not foreign_blocks_set:
                self.log.info("Inferring accepts_foreign_blocks from node versions (fork_b nodes accept fork_a blocks)")
                for node_config in nodes:
                    image_tag = node_config.get('image', {}).get('tag', '')
                    metadata = node_config.get('metadata', {})
                    # fork_b nodes accept fork_a blocks (non-contentious soft fork model)
                    if '26' in str(image_tag):
                        metadata['accepts_foreign_blocks'] = True
                    else:
                        metadata['accepts_foreign_blocks'] = False
                    node_config['metadata'] = metadata

        if network_config is None:
            self.log.warning("No network metadata available, pool/economic mapping will be limited")
            return

        try:
            # Build node name -> metadata mapping
            for node_config in network_config.get('nodes', []):
                node_name = node_config.get('name')
                metadata = node_config.get('metadata', {})
                # Preserve image tag so economic_node_strategy can determine
                # initial fork from version rather than a hardcoded index threshold
                metadata['image_tag'] = node_config.get('image', {}).get('tag', '')

                if node_name:
                    self.node_metadata[node_name] = metadata

            self.log.info(f"✓ Loaded metadata for {len(self.node_metadata)} nodes")

        except Exception as e:
            self.log.error(f"Error loading network YAML: {e}")

    def partition_nodes_by_version(self):
        """Separate nodes into fork_a and fork_b partitions"""
        self.log.info("Partitioning nodes by version...")
        ver_a = getattr(self.options, 'fork_a_version', '27.')
        ver_b = getattr(self.options, 'fork_b_version', '26.')

        for node in self.nodes:
            try:
                network_info = node.getnetworkinfo()
                version_string = network_info.get('subversion', '')

                is_fork_a = ver_a in version_string
                is_fork_b = ver_b in version_string

                if is_fork_a:
                    self.fork_a_nodes.append(node)
                elif is_fork_b:
                    self.fork_b_nodes.append(node)

            except Exception as e:
                self.log.error(f"  Error querying node {node.index}: {e}")

        self.log.info(f"\nPartition summary:")
        self.log.info(f"  fork_a nodes: {len(self.fork_a_nodes)}")
        self.log.info(f"  fork_b nodes: {len(self.fork_b_nodes)}")

    def build_foreign_accepting_nodes(self):
        """
        Identify nodes that accept blocks from the foreign fork (accepts_foreign_blocks=True).

        In the asymmetric fork model:
          - fork_a (strict): rejects fork_b blocks — accepts_foreign_blocks=False
          - fork_b (permissive): accepts fork_a blocks — accepts_foreign_blocks=True

        When a fork_a block is mined, it is submitted to the first foreign-accepting node
        via submitblock. P2P propagation within the fork_b island handles the rest.

        Note: fork_b nodes automatically accept fork_a blocks in non-contentious soft fork model,
        so we treat all fork_b nodes as foreign-accepting by default.
        """
        self.foreign_accepting_nodes = []

        for node in self.fork_b_nodes:
            node_name = f"node-{node.index:04d}"
            metadata = self.node_metadata.get(node_name, {})

            # Check explicit flag first
            accepts_foreign = metadata.get('accepts_foreign_blocks', None)

            # If not explicitly set, fork_b nodes accept fork_a blocks by default
            # (non-contentious soft fork model)
            if accepts_foreign is None:
                # fork_b nodes accept fork_a blocks (stricter rules are valid under looser rules)
                accepts_foreign = True
                self.log.debug(f"  {node_name}: accepts_foreign_blocks not set, defaulting to True (fork_b node)")

            if accepts_foreign:
                self.foreign_accepting_nodes.append(node)

        if self.foreign_accepting_nodes:
            self.log.info(
                f"\nAsymmetric fork mode: {len(self.foreign_accepting_nodes)} foreign-accepting node(s). "
                f"fork_a blocks will be submitted to node-{self.foreign_accepting_nodes[0].index:04d} "
                f"for fork_b-island propagation."
            )
        else:
            self.log.info(
                "\nAsymmetric fork mode: no foreign-accepting nodes found "
                "(accepts_foreign_blocks not set in metadata). "
                "Running as standard symmetric partition."
            )

    def propagate_to_foreign_accepting(self, miner, fork_id: str):
        """
        After mining a fork_a block, push it to the fork_b island via submitblock.

        Only acts when fork_id=='fork_a' and foreign-accepting nodes exist.
        Submits to one bridge node only — P2P handles intra-island propagation.
        """
        if fork_id != 'fork_a' or not self.foreign_accepting_nodes:
            return

        try:
            block_hash = miner.getbestblockhash()
            raw_block = miner.getblock(block_hash, 0)
            bridge_node = self.foreign_accepting_nodes[0]
            result = bridge_node.submitblock(raw_block)
            if result is not None:
                # submitblock returns None on success; any other value is a rejection reason
                self.log.warning(
                    f"  [asymmetric] submitblock to node-{bridge_node.index:04d} returned: {result}"
                )
        except Exception as e:
            self.log.error(f"  [asymmetric] Failed to propagate fork_a block to fork_b island: {e}")

    def build_partition_peer_lists(self):
        """
        Build lists of peer addresses for each partition.
        Used for dynamic partition switching - nodes can reconnect to a different partition.
        """
        self.log.info("\nBuilding partition peer lists for dynamic switching...")

        # Get addresses from nodes in each partition
        for node in self.fork_a_nodes:
            try:
                # Get the node's address that other nodes can connect to
                node_name = f"node-{node.index:04d}"
                # In warnet, nodes are addressable by their service name
                self.fork_a_peer_addresses.append(node_name)
                self.node_current_partition[node_name] = 'fork_a'
            except Exception as e:
                self.log.warning(f"  Could not get address for fork_a node {node.index}: {e}")

        for node in self.fork_b_nodes:
            try:
                node_name = f"node-{node.index:04d}"
                self.fork_b_peer_addresses.append(node_name)
                self.node_current_partition[node_name] = 'fork_b'
            except Exception as e:
                self.log.warning(f"  Could not get address for fork_b node {node.index}: {e}")

        self.log.info(f"  fork_a peers: {len(self.fork_a_peer_addresses)} nodes")
        self.log.info(f"  fork_b peers: {len(self.fork_b_peer_addresses)} nodes")

    def get_node_peers(self, node) -> list:
        """Get list of currently connected peer addresses for a node."""
        try:
            peer_info = node.getpeerinfo()
            return [p.get('addr', '').split(':')[0] for p in peer_info]
        except Exception as e:
            self.log.warning(f"  Could not get peers for node {node.index}: {e}")
            return []

    def switch_node_partition(self, node, old_partition: str, new_partition: str, reason: str = "") -> bool:
        """
        Switch a node from one partition to another by changing P2P connections.

        Steps:
        1. Disconnect from old partition peers
        2. Connect to new partition peers
        3. Wait for sync to new chain
        4. Update tracking

        Args:
            node: The node to switch
            old_partition: Current partition ('fork_a' or 'fork_b')
            new_partition: Target partition ('fork_a' or 'fork_b')
            reason: Why the switch is happening (for logging)

        Returns:
            True if switch was successful
        """
        if old_partition == new_partition:
            return True  # No switch needed

        node_name = f"node-{node.index:04d}"
        self.log.info(f"\n  PARTITION SWITCH: {node_name} {old_partition} -> {new_partition}")
        if reason:
            self.log.info(f"    Reason: {reason}")

        old_peers = self.fork_a_peer_addresses if old_partition == 'fork_a' else self.fork_b_peer_addresses
        new_peers = self.fork_b_peer_addresses if new_partition == 'fork_b' else self.fork_a_peer_addresses

        try:
            # Step 1: Get current height before switch
            old_height = node.getblockcount()
            old_hash = node.getbestblockhash()

            # Step 2: Disconnect from old partition peers
            current_peers = self.get_node_peers(node)
            disconnected = 0
            for peer_addr in current_peers:
                # Check if this peer is in the old partition
                for old_peer in old_peers:
                    if old_peer in peer_addr:
                        try:
                            node.disconnectnode(peer_addr)
                            disconnected += 1
                        except Exception:
                            pass  # May already be disconnected
                        break

            self.log.info(f"    Disconnected from {disconnected} {old_partition} peers")

            # Step 3: Connect to new partition peers
            connected = 0
            # Connect to a subset of new peers (not all, to avoid overwhelming)
            peers_to_add = new_peers[:min(8, len(new_peers))]
            for peer_name in peers_to_add:
                if peer_name != node_name:  # Don't connect to self
                    try:
                        node.addnode(peer_name, "onetry")
                        connected += 1
                    except Exception as e:
                        self.log.warning(f"    Could not connect to {peer_name}: {e}")

            self.log.info(f"    Connected to {connected} {new_partition} peers")

            # Step 4: Wait briefly for connection establishment
            import time
            time.sleep(2)

            # Step 5: Check if we synced to new chain
            new_height = node.getblockcount()
            new_hash = node.getbestblockhash()

            if new_hash != old_hash:
                self.log.info(f"    Chain changed: height {old_height} -> {new_height}")
                self.log.info(f"    Old tip: {old_hash[:16]}...")
                self.log.info(f"    New tip: {new_hash[:16]}...")

            # Step 6: Update tracking
            self.node_current_partition[node_name] = new_partition

            # Move node between partition lists
            if old_partition == 'fork_a' and node in self.fork_a_nodes:
                self.fork_a_nodes.remove(node)
                self.fork_b_nodes.append(node)
            elif old_partition == 'fork_b' and node in self.fork_b_nodes:
                self.fork_b_nodes.remove(node)
                self.fork_a_nodes.append(node)

            # Record the switch
            self.partition_switch_history.append({
                'node': node_name,
                'from': old_partition,
                'to': new_partition,
                'reason': reason,
                'old_height': old_height,
                'new_height': new_height,
                'timestamp': time.time()
            })

            self.log.info(f"    Switch complete: {node_name} is now on {new_partition}")
            return True

        except Exception as e:
            self.log.error(f"    Switch failed for {node_name}: {e}")
            return False

    def reunite_forks(self, force: bool = False) -> dict:
        """
        Reconnect the two fork partitions and wait for the losing fork to reorg
        to the heavier chain.

        The winning fork is determined by cumulative chainwork (difficulty oracle)
        or by block count if difficulty tracking is disabled.

        Steps:
        1. Determine the winning fork by chainwork / block count
        2. Connect each losing-fork node to several winning-fork peers via addnode
        3. Poll until all losing-fork nodes converge to the winning tip (or timeout)
        4. Report reorg depth, orphaned blocks, and convergence metrics

        Args:
            force: If True, run reunion even if --enable-reunion flag is not set.
                   Used by UASF expiry to trigger reunion regardless of flag.

        Returns:
            dict with reunion metrics (included in the results export)
        """
        if not force and not self.options.enable_reunion:
            return {'enabled': False}

        self.log.info(f"\n{'='*70}")
        self.log.info("FORK REUNION")
        self.log.info(f"{'='*70}")

        # Determine winning and losing forks
        if self.difficulty_oracle:
            winner_id, winner_cw, loser_cw = self.difficulty_oracle.get_winning_fork()
        else:
            winner_id = 'fork_a' if self.blocks_mined['fork_a'] >= self.blocks_mined['fork_b'] else 'fork_b'
            winner_cw = float(self.blocks_mined[winner_id])
            loser_cw = float(self.blocks_mined['fork_b' if winner_id == 'fork_a' else 'fork_a'])

        loser_id = 'fork_b' if winner_id == 'fork_a' else 'fork_a'
        winning_nodes = self.fork_a_nodes if winner_id == 'fork_a' else self.fork_b_nodes
        losing_nodes = self.fork_b_nodes if winner_id == 'fork_a' else self.fork_a_nodes
        winning_peers = self.fork_a_peer_addresses if winner_id == 'fork_a' else self.fork_b_peer_addresses

        if not winning_nodes or not losing_nodes:
            self.log.warning("  Cannot reunite: one partition has no nodes")
            return {'enabled': True, 'skipped': True, 'reason': 'empty_partition'}

        # Snapshot pre-reorg state
        try:
            winning_tip_hash = winning_nodes[0].getbestblockhash()
            winning_tip_height = winning_nodes[0].getblockcount()
        except Exception as e:
            self.log.error(f"  Cannot read winning fork tip: {e}")
            return {'enabled': True, 'skipped': True, 'reason': 'rpc_error'}

        pre_reorg_heights = {}
        for node in losing_nodes:
            node_name = f"node-{node.index:04d}"
            try:
                pre_reorg_heights[node_name] = node.getblockcount()
            except Exception as e:
                self.log.warning(f"  Could not get height for {node_name}: {e}")
                pre_reorg_heights[node_name] = 0

        losing_tip_height = max(pre_reorg_heights.values(), default=0)
        self.log.info(f"  Winning fork: {winner_id}  height={winning_tip_height}  chainwork={winner_cw:.4f}")
        self.log.info(f"  Losing fork:  {loser_id}  height={losing_tip_height}  chainwork={loser_cw:.4f}")
        self.log.info(f"  Connecting {len(losing_nodes)} {loser_id} nodes to {winner_id} peers...")

        # Connect each losing-fork node to up to 4 winning-fork peers
        peers_to_add = winning_peers[:min(4, len(winning_peers))]
        connected_count = 0
        for node in losing_nodes:
            node_name = f"node-{node.index:04d}"
            for peer_name in peers_to_add:
                if peer_name != node_name:
                    try:
                        node.addnode(peer_name, "onetry")
                        connected_count += 1
                    except Exception as e:
                        self.log.debug(f"  Could not connect {node_name} -> {peer_name}: {e}")

        self.log.info(f"  Added {connected_count} cross-partition connections")
        self.log.info(f"  Waiting up to {self.options.reunion_timeout}s for reorg convergence...")

        # Poll for convergence
        poll_interval = 2.0
        reunion_start = time()
        converged_nodes = set()
        wait_elapsed = 0.0

        while True:
            wait_elapsed = time() - reunion_start
            if wait_elapsed >= self.options.reunion_timeout:
                break

            # Refresh winning tip in case mining is still active
            try:
                winning_tip_hash = winning_nodes[0].getbestblockhash()
                winning_tip_height = winning_nodes[0].getblockcount()
            except Exception:
                pass

            for node in losing_nodes:
                node_name = f"node-{node.index:04d}"
                if node_name in converged_nodes:
                    continue
                try:
                    best_hash = node.getbestblockhash()
                    if best_hash == winning_tip_hash:
                        post_h = node.getblockcount()
                        self.log.info(f"  {node_name} converged at height {post_h} ({wait_elapsed:.1f}s)")
                        converged_nodes.add(node_name)
                except Exception as e:
                    self.log.debug(f"  Polling {node_name}: {e}")

            if len(converged_nodes) >= len(losing_nodes):
                break

            sleep(poll_interval)

        # Final heights after waiting
        post_reorg_heights = {}
        for node in losing_nodes:
            node_name = f"node-{node.index:04d}"
            try:
                post_reorg_heights[node_name] = node.getblockcount()
            except Exception:
                post_reorg_heights[node_name] = pre_reorg_heights.get(node_name, 0)

        # Metrics
        fork_point_height = self.options.start_height
        losing_fork_depth = losing_tip_height - fork_point_height
        orphaned_blocks = self.blocks_mined[loser_id]
        nodes_converged = len(converged_nodes)
        nodes_total = len(losing_nodes)
        timed_out = nodes_converged < nodes_total

        self.log.info(f"\n  REUNION RESULTS:")
        self.log.info(f"  Winner:          {winner_id} (height {winning_tip_height})")
        self.log.info(f"  Reorg depth:     {losing_fork_depth} blocks ({loser_id} fork above fork point)")
        self.log.info(f"  Orphaned blocks: {orphaned_blocks}")
        self.log.info(f"  Nodes converged: {nodes_converged}/{nodes_total} in {wait_elapsed:.1f}s")
        if timed_out:
            self.log.warning(f"  WARNING: {nodes_total - nodes_converged} nodes did not converge within timeout")

        return {
            'enabled': True,
            'winner': winner_id,
            'loser': loser_id,
            'winner_chainwork': winner_cw,
            'loser_chainwork': loser_cw,
            'winning_tip_height': winning_tip_height,
            'winning_tip_hash': winning_tip_hash,
            'fork_point_height': fork_point_height,
            'losing_fork_depth': losing_fork_depth,
            'orphaned_blocks': orphaned_blocks,
            'nodes_converged': nodes_converged,
            'nodes_total': nodes_total,
            'timed_out': timed_out,
            'wait_elapsed_seconds': round(wait_elapsed, 1),
            'pre_reorg_heights': pre_reorg_heights,
            'post_reorg_heights': post_reorg_heights,
        }

    def handle_uasf_expiry(self, elapsed: float, current_time: float):
        """
        Handle UASF expiration.

        When the time-limited UASF expires:
        1. Log the expiry event with full state snapshot
        2. Change fork_a nodes to accept fork_b blocks (accepts_foreign_blocks=True)
        3. Based on --uasf-expiry-action:
           - 'reunion': Reconnect partitions and let heavier chain win
           - 'accept': fork_a accepts fork_b blocks but partitions stay separate
           - 'continue': Just log and continue (no behavior change)

        This simulates miners/nodes who committed to running UASF for a limited
        period and then "give up" if the soft fork hasn't activated.
        """
        self.log.info(f"\n{'='*70}")
        self.log.info(f"UASF EXPIRED at {elapsed}s ({elapsed/3600:.2f} hours)")
        self.log.info(f"{'='*70}")

        # Snapshot state at expiry
        fork_a_blocks = self.blocks_mined['fork_a']
        fork_b_blocks = self.blocks_mined['fork_b']
        fork_a_price = self.price_oracle.get_price('fork_a') if self.price_oracle else 0
        fork_b_price = self.price_oracle.get_price('fork_b') if self.price_oracle else 0

        if self.difficulty_oracle:
            fork_a_chainwork = self.difficulty_oracle.get_cumulative_chainwork('fork_a')
            fork_b_chainwork = self.difficulty_oracle.get_cumulative_chainwork('fork_b')
            fork_a_difficulty = self.difficulty_oracle.forks['fork_a'].current_difficulty
            fork_b_difficulty = self.difficulty_oracle.forks['fork_b'].current_difficulty
        else:
            fork_a_chainwork = float(fork_a_blocks)
            fork_b_chainwork = float(fork_b_blocks)
            fork_a_difficulty = 1.0
            fork_b_difficulty = 1.0

        # Determine winning fork at expiry
        winning_fork = 'fork_a' if fork_a_chainwork >= fork_b_chainwork else 'fork_b'
        losing_fork = 'fork_b' if winning_fork == 'fork_a' else 'fork_a'

        self.uasf_expiry_data = {
            'expiry_elapsed_seconds': elapsed,
            'expiry_timestamp': current_time,
            'uasf_duration_seconds': self.options.uasf_duration,
            'expiry_action': self.options.uasf_expiry_action,
            'state_at_expiry': {
                'fork_a_blocks': fork_a_blocks,
                'fork_b_blocks': fork_b_blocks,
                'fork_a_chainwork': fork_a_chainwork,
                'fork_b_chainwork': fork_b_chainwork,
                'fork_a_difficulty': fork_a_difficulty,
                'fork_b_difficulty': fork_b_difficulty,
                'fork_a_price': fork_a_price,
                'fork_b_price': fork_b_price,
                'fork_a_hashrate': self.current_fork_a_hashrate,
                'fork_b_hashrate': self.current_fork_b_hashrate,
                'fork_a_economic': self.current_fork_a_economic,
                'fork_b_economic': self.current_fork_b_economic,
                'winning_fork_at_expiry': winning_fork,
            },
            'reunion_triggered': False,
            'reunion_results': None,
        }

        self.log.info(f"  State at expiry:")
        self.log.info(f"    fork_a: {fork_a_blocks} blocks, chainwork={fork_a_chainwork:.2f}, price=${fork_a_price:,.0f}")
        self.log.info(f"    fork_b: {fork_b_blocks} blocks, chainwork={fork_b_chainwork:.2f}, price=${fork_b_price:,.0f}")
        self.log.info(f"    Hashrate: fork_a={self.current_fork_a_hashrate:.1f}%, fork_b={self.current_fork_b_hashrate:.1f}%")
        self.log.info(f"    Winning fork at expiry: {winning_fork}")

        # Mark UASF as expired
        self.uasf_expired = True
        self.uasf_active = False

        # Handle expiry action
        if self.options.uasf_expiry_action == 'reunion':
            self.log.info(f"\n  Expiry action: REUNION - reconnecting partitions...")

            # Change fork_a nodes to accept fork_b blocks
            self._enable_fork_a_foreign_acceptance()

            # Trigger reunion (force=True to bypass --enable-reunion check)
            reunion_results = self.reunite_forks(force=True)
            self.uasf_expiry_data['reunion_triggered'] = True
            self.uasf_expiry_data['reunion_results'] = reunion_results

            if reunion_results.get('timed_out'):
                self.log.warning(f"  Reunion timed out - some nodes may not have converged")
            elif reunion_results.get('enabled'):
                # Successful reunion - all hashrate now goes to winning fork
                self.reunion_completed = True
                self.reunion_winner = reunion_results.get('winner')

                if self.reunion_winner == 'fork_a':
                    self.current_fork_a_hashrate = 100.0
                    self.current_fork_b_hashrate = 0.0
                else:
                    self.current_fork_a_hashrate = 0.0
                    self.current_fork_b_hashrate = 100.0

                # Update all pool allocations to winning fork
                if self.pool_strategy:
                    for pool_id in self.pool_strategy.current_allocation:
                        self.pool_strategy.current_allocation[pool_id] = self.reunion_winner
                        # Also update pool's current_fork tracking
                        if pool_id in self.pool_strategy.pools:
                            self.pool_strategy.pools[pool_id].current_fork = self.reunion_winner

                self.log.info(f"  Post-reunion hashrate: {self.reunion_winner}=100%, other=0%")
                self.log.info(f"  All pools now mining on {self.reunion_winner}")

        elif self.options.uasf_expiry_action == 'accept':
            self.log.info(f"\n  Expiry action: ACCEPT - fork_a nodes now accept fork_b blocks")
            self.log.info(f"  (Partitions remain separate, but cross-chain blocks are now valid)")

            # Change fork_a nodes to accept fork_b blocks
            self._enable_fork_a_foreign_acceptance()

        else:  # 'continue'
            self.log.info(f"\n  Expiry action: CONTINUE - logging expiry only, no behavior change")
            self.log.info(f"  (fork_a nodes continue to reject fork_b blocks)")

        # Calculate opportunity cost for UASF supporters
        if winning_fork == 'fork_b':
            # fork_a lost - calculate cost of supporting losing fork
            orphaned_blocks = fork_a_blocks
            wasted_chainwork = fork_a_chainwork
            self.log.info(f"\n  UASF FAILED - fork_a chain will be orphaned")
            self.log.info(f"    Orphaned blocks: {orphaned_blocks}")
            self.log.info(f"    Wasted chainwork: {wasted_chainwork:.2f}")

            self.uasf_expiry_data['uasf_outcome'] = 'failed'
            self.uasf_expiry_data['orphaned_blocks'] = orphaned_blocks
            self.uasf_expiry_data['wasted_chainwork'] = wasted_chainwork
        else:
            # fork_a won - UASF was successful (though expired, the chain is still dominant)
            self.log.info(f"\n  UASF SUCCEEDED - fork_a chain is dominant")
            self.log.info(f"    fork_a chainwork lead: {fork_a_chainwork - fork_b_chainwork:.2f}")

            self.uasf_expiry_data['uasf_outcome'] = 'succeeded'
            self.uasf_expiry_data['chainwork_lead'] = fork_a_chainwork - fork_b_chainwork

        self.log.info(f"{'='*70}\n")

    def _enable_fork_a_foreign_acceptance(self):
        """
        Change fork_a nodes to accept fork_b blocks (accepts_foreign_blocks=True).

        This simulates UASF nodes "giving up" and accepting the old rules again.
        After this, fork_a nodes will accept blocks from fork_b and can reorg to
        the longer chain if fork_b has more chainwork.
        """
        self.log.info(f"  Changing fork_a nodes to accept fork_b blocks...")

        changed_count = 0
        for node in self.fork_a_nodes:
            node_name = f"node-{node.index:04d}"
            metadata = self.node_metadata.get(node_name, {})

            # Update metadata to accept foreign blocks
            old_value = metadata.get('accepts_foreign_blocks', False)
            metadata['accepts_foreign_blocks'] = True
            self.node_metadata[node_name] = metadata

            if not old_value:
                changed_count += 1
                self.log.debug(f"    {node_name}: accepts_foreign_blocks = True")

        # Add fork_a nodes to foreign_accepting_nodes list
        for node in self.fork_a_nodes:
            if node not in self.foreign_accepting_nodes:
                self.foreign_accepting_nodes.append(node)

        self.log.info(f"  Changed {changed_count} fork_a nodes to accept foreign blocks")

    def evaluate_economic_node_switches(self, elapsed: float):
        """
        Evaluate whether economic/user nodes should switch partitions based on
        economic conditions (price, fees, ideology).

        Called periodically during simulation to allow dynamic partition changes.
        """
        if not self.economic_strategy:
            return

        switches = []

        # Get current prices and conditions
        fork_a_price = self.price_oracle.get_price('fork_a') if self.price_oracle else 60000
        fork_b_price = self.price_oracle.get_price('fork_b') if self.price_oracle else 60000
        price_ratio = fork_a_price / fork_b_price if fork_b_price > 0 else 1.0

        # Evaluate each economic node
        for node in list(self.fork_a_nodes) + list(self.fork_b_nodes):
            node_name = f"node-{node.index:04d}"
            metadata = self.node_metadata.get(node_name, {})

            # Skip pool nodes - they have their own switching logic
            if metadata.get('node_type') == 'mining_pool':
                continue

            # Only evaluate economic and user nodes
            node_type = metadata.get('node_type', '')
            if node_type not in ['economic', 'user']:
                continue

            current_partition = self.node_current_partition.get(node_name, 'fork_a')
            fork_preference = metadata.get('fork_preference', 'neutral')
            ideology_strength = metadata.get('ideology_strength', 0.0)
            switching_threshold = metadata.get('switching_threshold', 0.10)
            inertia = metadata.get('inertia', 0.05)

            # Determine if node should switch
            should_switch = False
            reason = ""

            # Ideology scales the effective switching threshold.
            # Low ideology (0.0) → switches at base threshold (purely rational).
            # High ideology (1.0) → needs 3× the base threshold to switch.
            # This replaces the old hard cutoffs (< 0.3 / > 0.7) which created
            # a dead zone where nodes with ideology 0.3–0.7 could never switch.
            effective_threshold = switching_threshold * (1 + ideology_strength * 2.0)

            if current_partition == 'fork_a':
                # Ideological pull: strong preference for fork_b overrides price
                if fork_preference == 'fork_b' and ideology_strength > 0.5:
                    should_switch = True
                    reason = f"ideological preference for fork_b (strength={ideology_strength:.2f})"
                # Rational: fork_b price advantage exceeds ideology-adjusted threshold
                elif price_ratio < (1 - effective_threshold):
                    should_switch = True
                    reason = (f"economic: fork_a/fork_b ratio {price_ratio:.3f} below "
                              f"threshold {1 - effective_threshold:.3f} "
                              f"(ideology={ideology_strength:.2f})")

            else:  # current_partition == 'fork_b'
                # Ideological pull: strong preference for fork_a overrides price
                if fork_preference == 'fork_a' and ideology_strength > 0.5:
                    should_switch = True
                    reason = f"ideological preference for fork_a (strength={ideology_strength:.2f})"
                # Rational: fork_a price advantage exceeds ideology-adjusted threshold
                elif price_ratio > (1 + effective_threshold):
                    should_switch = True
                    reason = (f"economic: fork_a/fork_b ratio {price_ratio:.3f} above "
                              f"threshold {1 + effective_threshold:.3f} "
                              f"(ideology={ideology_strength:.2f})")

            # Apply inertia - random chance to delay switch
            if should_switch:
                import random
                if random.random() < inertia:
                    should_switch = False
                    self.log.debug(f"  {node_name} switch delayed by inertia")

            if should_switch:
                new_partition = 'fork_b' if current_partition == 'fork_a' else 'fork_a'
                switches.append((node, current_partition, new_partition, reason))

        # Execute switches
        for node, old_part, new_part, reason in switches:
            self.switch_node_partition(node, old_part, new_part, reason)

    def build_pool_node_mapping(self):
        """
        Build mapping from pool IDs to nodes in each partition.

        Architecture:
        - Each pool has ONE node per partition (if present)
        - Same entity_id appears in both partitions (paired nodes)
        - Pool decides which fork to mine → uses corresponding node
        """
        self.log.info("\nBuilding pool-to-node mappings...")

        # Track unmapped nodes
        fork_a_unmapped = 0
        fork_b_unmapped = 0

        # Read node metadata to extract pool assignments
        for node in self.fork_a_nodes:
            pool_id = self.get_node_pool_id(node)
            if pool_id:
                if pool_id not in self.pool_nodes_fork_a:
                    self.pool_nodes_fork_a[pool_id] = []
                self.pool_nodes_fork_a[pool_id].append(node)
            else:
                fork_a_unmapped += 1

        for node in self.fork_b_nodes:
            pool_id = self.get_node_pool_id(node)
            if pool_id:
                if pool_id not in self.pool_nodes_fork_b:
                    self.pool_nodes_fork_b[pool_id] = []
                self.pool_nodes_fork_b[pool_id].append(node)
            else:
                fork_b_unmapped += 1

        # Log pool distribution
        self.log.info("\nPool node distribution (1 node per partition per pool):")

        # Get all unique pool IDs from both partitions
        all_pools = set(self.pool_nodes_fork_a.keys()) | set(self.pool_nodes_fork_b.keys())

        for pool_id in sorted(all_pools):
            fork_a_nodes = self.pool_nodes_fork_a.get(pool_id, [])
            fork_b_nodes = self.pool_nodes_fork_b.get(pool_id, [])

            fork_a_str = f"node-{fork_a_nodes[0].index:04d}" if fork_a_nodes else "none"
            fork_b_str = f"node-{fork_b_nodes[0].index:04d}" if fork_b_nodes else "none"

            # Warn if pool has multiple nodes in one partition (unexpected)
            if len(fork_a_nodes) > 1:
                self.log.warning(f"  {pool_id:15s}: MULTIPLE fork_a nodes ({len(fork_a_nodes)}) - expected 1")
            if len(fork_b_nodes) > 1:
                self.log.warning(f"  {pool_id:15s}: MULTIPLE fork_b nodes ({len(fork_b_nodes)}) - expected 1")

            self.log.info(f"  {pool_id:15s}: fork_a={fork_a_str:12s} fork_b={fork_b_str:12s}")

        if fork_a_unmapped > 0:
            self.log.info(f"\n  {fork_a_unmapped} fork_a nodes without pool assignment (economic/user nodes)")

        if fork_b_unmapped > 0:
            self.log.info(f"  {fork_b_unmapped} fork_b nodes without pool assignment (economic/user nodes)")

    def get_node_pool_id(self, node) -> Optional[str]:
        """Extract pool ID from node metadata (read from network.yaml)"""
        try:
            # Get node name from tank_index
            # Warnet nodes are typically named "node-XXXX"
            node_name = f"node-{node.index:04d}"

            # Look up metadata from network.yaml
            if node_name in self.node_metadata:
                metadata = self.node_metadata[node_name]
                entity_id = metadata.get('entity_id', None)

                if entity_id and entity_id.startswith('pool-'):
                    # Convert pool-antpool -> antpool
                    pool_id = entity_id.replace('pool-', '')
                    return pool_id

            return None

        except Exception as e:
            self.log.debug(f"Could not extract pool ID from node {node.index}: {e}")
            return None

    def select_mining_node(self) -> Tuple[Optional[object], Optional[str]]:
        """
        Select a mining node based on pool decisions and hashrate weights.

        Architecture (Simple Binary Allocation):
        1. Each pool chooses ONE fork to mine ('fork_a' or 'fork_b')
        2. Pool's full hashrate goes to chosen fork
        3. Weighted random selection based on hashrate
        4. Selected pool's node on chosen partition mines the block
        """
        if not self.pool_strategy:
            # Fallback: random selection from aggregate hashrate
            rand_val = random() * 100.0
            if rand_val < self.current_fork_a_hashrate:
                partition = 'fork_a'
                nodes = self.fork_a_nodes
            else:
                partition = 'fork_b'
                nodes = self.fork_b_nodes

            if not nodes:
                return None, None
            return choices(nodes, k=1)[0], partition

        # Pool-based selection with binary allocation
        # Build weighted list of (pool_id, partition) based on pool decisions
        pool_choices = []
        pool_weights = []

        for pool_id, allocation in self.pool_strategy.current_allocation.items():
            pool = self.pool_strategy.pools[pool_id]
            hashrate_weight = pool.hashrate_pct

            # Binary allocation: pool chooses one fork
            chosen_fork = allocation  # 'fork_a' or 'fork_b'

            # Check if pool has a node in the chosen partition
            if chosen_fork == 'fork_a':
                if pool_id in self.pool_nodes_fork_a and self.pool_nodes_fork_a[pool_id]:
                    pool_choices.append((pool_id, 'fork_a'))
                    pool_weights.append(hashrate_weight)
                else:
                    self.log.debug(f"Pool {pool_id} chose fork_a but has no fork_a node")
            elif chosen_fork == 'fork_b':
                if pool_id in self.pool_nodes_fork_b and self.pool_nodes_fork_b[pool_id]:
                    pool_choices.append((pool_id, 'fork_b'))
                    pool_weights.append(hashrate_weight)
                else:
                    self.log.debug(f"Pool {pool_id} chose fork_b but has no fork_b node")

        if not pool_choices:
            # No pool nodes mapped, fallback to random selection based on hashrate
            rand_val = random() * 100.0
            if rand_val < self.current_fork_a_hashrate:
                partition = 'fork_a'
                nodes = self.fork_a_nodes
            else:
                partition = 'fork_b'
                nodes = self.fork_b_nodes

            if not nodes:
                return None, None
            return choices(nodes, k=1)[0], partition

        # Weighted random selection by hashrate
        selected_pool_id, partition = choices(pool_choices, weights=pool_weights, k=1)[0]

        # Get the pool's node on the chosen partition (should be exactly 1 node)
        if partition == 'fork_a':
            nodes = self.pool_nodes_fork_a.get(selected_pool_id, [])
        else:
            nodes = self.pool_nodes_fork_b.get(selected_pool_id, [])

        if not nodes:
            self.log.warning(f"No nodes for pool {selected_pool_id} in {partition}")
            return None, None

        # Return the pool's node (first/only node in list)
        selected_node = nodes[0]
        return selected_node, partition

    def _select_miner_for_fork(self, fork_id: str) -> Optional[object]:
        """
        Select a mining node for a specific fork (used in difficulty mode).

        In difficulty mode, we already know WHICH fork gets a block. This method
        selects which pool/node mines it, weighted by hashrate of pools AND solo
        miners allocated to that fork.

        Args:
            fork_id: 'fork_a' or 'fork_b'

        Returns:
            A node object to mine on, or None if no node available
        """
        nodes = self.fork_a_nodes if fork_id == 'fork_a' else self.fork_b_nodes
        if not nodes:
            return None

        if not self.pool_strategy:
            # No pool strategy: random node from partition
            return choices(nodes, k=1)[0]

        # Build weighted list of pools allocated to this fork
        miner_choices = []
        miner_weights = []

        # Add pools
        for pool_id, allocation in self.pool_strategy.current_allocation.items():
            if allocation != fork_id:
                continue

            pool = self.pool_strategy.pools[pool_id]
            pool_node_map = self.pool_nodes_fork_a if fork_id == 'fork_a' else self.pool_nodes_fork_b

            if pool_id in pool_node_map and pool_node_map[pool_id]:
                miner_choices.append(pool_node_map[pool_id][0])
                miner_weights.append(pool.hashrate_pct)

        # Add solo miners (user/economic nodes with hashrate)
        if self.economic_strategy:
            solo_miners = self.economic_strategy.get_solo_miners()
            for node_id, miner_fork, hashrate in solo_miners:
                if miner_fork != fork_id or hashrate <= 0:
                    continue

                # Find the actual node object by node_id
                node_obj = self._get_node_by_id(node_id, fork_id)
                if node_obj:
                    miner_choices.append(node_obj)
                    miner_weights.append(hashrate)

        if not miner_choices:
            # Fallback: random node from partition
            return choices(nodes, k=1)[0]

        return choices(miner_choices, weights=miner_weights, k=1)[0]

    def _get_node_by_id(self, node_id: str, fork_id: str) -> Optional[object]:
        """
        Get a node object by its node_id string.

        Args:
            node_id: Node identifier (e.g., "node-0008")
            fork_id: Which fork partition to look in ('fork_a' or 'fork_b')

        Returns:
            Node object or None if not found
        """
        nodes = self.fork_a_nodes if fork_id == 'fork_a' else self.fork_b_nodes

        for node in nodes:
            # Node names are typically "node-XXXX" format
            node_name = f"node-{node.index:04d}"
            if node_name == node_id:
                return node

        return None

    def capture_time_series_snapshot(self, elapsed: int):
        """
        Capture a snapshot of current state for time series charting.

        Args:
            elapsed: Seconds since simulation start
        """
        # Compute rolling block time before appending current values
        if len(self.time_series['timestamps']) > 0:
            delta_t = elapsed - self.time_series['timestamps'][-1]
            delta_fork_a = self.blocks_mined['fork_a'] - self.time_series['fork_a_blocks'][-1]
            delta_fork_b = self.blocks_mined['fork_b'] - self.time_series['fork_b_blocks'][-1]
            fork_a_block_time = delta_t / delta_fork_a if delta_fork_a > 0 else None
            fork_b_block_time = delta_t / delta_fork_b if delta_fork_b > 0 else None
        else:
            fork_a_block_time = None  # no previous snapshot to compare against
            fork_b_block_time = None

        self.time_series['timestamps'].append(elapsed)
        self.time_series['fork_a_price'].append(self.price_oracle.get_price('fork_a'))
        self.time_series['fork_b_price'].append(self.price_oracle.get_price('fork_b'))
        self.time_series['fork_a_hashrate'].append(self.current_fork_a_hashrate)
        self.time_series['fork_b_hashrate'].append(self.current_fork_b_hashrate)
        self.time_series['fork_a_economic'].append(self.current_fork_a_economic)
        self.time_series['fork_b_economic'].append(self.current_fork_b_economic)
        self.time_series['fork_a_blocks'].append(self.blocks_mined['fork_a'])
        self.time_series['fork_b_blocks'].append(self.blocks_mined['fork_b'])

        if self.difficulty_oracle:
            fork_a_state = self.difficulty_oracle.forks.get('fork_a')
            fork_b_state = self.difficulty_oracle.forks.get('fork_b')
            self.time_series['fork_a_difficulty'].append(
                fork_a_state.current_difficulty if fork_a_state else 1.0)
            self.time_series['fork_b_difficulty'].append(
                fork_b_state.current_difficulty if fork_b_state else 1.0)
            self.time_series['fork_a_chainwork'].append(
                fork_a_state.cumulative_chainwork if fork_a_state else 0.0)
            self.time_series['fork_b_chainwork'].append(
                fork_b_state.cumulative_chainwork if fork_b_state else 0.0)
        else:
            self.time_series['fork_a_difficulty'].append(1.0)
            self.time_series['fork_b_difficulty'].append(1.0)
            self.time_series['fork_a_chainwork'].append(float(self.blocks_mined['fork_a']))
            self.time_series['fork_b_chainwork'].append(float(self.blocks_mined['fork_b']))

        # Fee market metrics
        if self.fee_oracle:
            # Fee rates
            self.time_series['fork_a_fee_rate'].append(self.fee_oracle.get_fee('fork_a'))
            self.time_series['fork_b_fee_rate'].append(self.fee_oracle.get_fee('fork_b'))

            # Fee revenue per block (miner incentive from fees)
            self.time_series['fork_a_fee_revenue_btc'].append(
                self.fee_oracle.get_fee_revenue_per_block('fork_a'))
            self.time_series['fork_b_fee_revenue_btc'].append(
                self.fee_oracle.get_fee_revenue_per_block('fork_b'))

            # Mempool/congestion estimates
            # Get blocks per hour for congestion calculation
            if self.difficulty_oracle:
                fork_a_bph = self.difficulty_oracle.get_blocks_per_hour('fork_a', self.current_fork_a_hashrate)
                fork_b_bph = self.difficulty_oracle.get_blocks_per_hour('fork_b', self.current_fork_b_hashrate)
            else:
                # Estimate from hashrate (normal rate = 6 blocks/hour)
                fork_a_bph = 6.0 * (self.current_fork_a_hashrate / 100.0) if self.current_fork_a_hashrate > 0 else 0.1
                fork_b_bph = 6.0 * (self.current_fork_b_hashrate / 100.0) if self.current_fork_b_hashrate > 0 else 0.1

            fork_a_mempool = self.fee_oracle.estimate_mempool_size(
                'fork_a', fork_a_bph, self.current_fork_a_economic)
            fork_b_mempool = self.fee_oracle.estimate_mempool_size(
                'fork_b', fork_b_bph, self.current_fork_b_economic)

            self.time_series['fork_a_congestion'].append(fork_a_mempool['congestion_ratio'])
            self.time_series['fork_b_congestion'].append(fork_b_mempool['congestion_ratio'])
            self.time_series['fork_a_mempool_mb'].append(fork_a_mempool['estimated_mempool_mb'])
            self.time_series['fork_b_mempool_mb'].append(fork_b_mempool['estimated_mempool_mb'])
        else:
            # Default values if no fee oracle
            self.time_series['fork_a_fee_rate'].append(1.0)
            self.time_series['fork_b_fee_rate'].append(1.0)
            self.time_series['fork_a_fee_revenue_btc'].append(0.01)
            self.time_series['fork_b_fee_revenue_btc'].append(0.01)
            self.time_series['fork_a_congestion'].append(1.0)
            self.time_series['fork_b_congestion'].append(1.0)
            self.time_series['fork_a_mempool_mb'].append(0.0)
            self.time_series['fork_b_mempool_mb'].append(0.0)

        # Transactional weight (fee-generating economic activity)
        self.time_series['fork_a_transactional'].append(self.current_fork_a_transactional)
        self.time_series['fork_b_transactional'].append(self.current_fork_b_transactional)

        # Solo miner hashrate
        self.time_series['fork_a_solo_hashrate'].append(self.current_fork_a_solo_hashrate)
        self.time_series['fork_b_solo_hashrate'].append(self.current_fork_b_solo_hashrate)

        # Measured block time (rolling window)
        self.time_series['fork_a_block_time_s'].append(fork_a_block_time)
        self.time_series['fork_b_block_time_s'].append(fork_b_block_time)

    def run_test(self):
        """Main mining loop with dynamic pool strategy"""

        # Calculate complementary economic weight
        if self.options.fork_b_economic is None:
            self.options.fork_b_economic = 100.0 - self.options.fork_a_economic

        self.log.info(f"\n{'='*70}")
        self.log.info(f"Partition Mining with Dynamic Pool Strategy")
        self.log.info(f"{'='*70}")
        self.log.info(f"Initial economic weights: fork_a={self.options.fork_a_economic}%, fork_b={self.options.fork_b_economic}%")
        self.log.info(f"Duration: {self.options.duration}s ({self.options.duration/60:.0f} minutes)")
        self.log.info(f"Pool scenario: {self.options.pool_scenario}")
        self.log.info(f"Economic scenario: {self.options.economic_scenario}")
        self.log.info(f"{'='*70}\n")

        # Initialize oracles
        price_oracle_kwargs = {
            'base_price': 60000,
            'min_fork_depth': 6,
            'debug': self.options.debug_prices,
            'liveness_penalty': self.options.enable_liveness_penalty,
            'use_economic_ema': self.options.use_economic_ema,
            'economic_ema_alpha': self.options.economic_ema_alpha,
            'use_sigmoid': self.options.use_sigmoid,
            'sigmoid_steepness_k': self.options.sigmoid_steepness,
            'use_cost_floor': self.options.use_cost_floor,
            'cost_floor_margin_buffer': self.options.cost_floor_margin_buffer,
        }
        # Override max divergence if specified via command line
        if self.options.max_price_divergence is not None:
            price_oracle_kwargs['max_divergence'] = self.options.max_price_divergence
        self.price_oracle = PriceOracle(**price_oracle_kwargs)
        divergence_msg = f", max_divergence=±{self.options.max_price_divergence*100:.0f}%" if self.options.max_price_divergence else ""
        self.log.info(f"✓ Price oracle initialized (debug={self.options.debug_prices}{divergence_msg})")

        self.fee_oracle = FeeOracle()
        self.log.info("✓ Fee oracle initialized")

        # Initialize pool strategy
        try:
            # Load config from bundled package (works inside .pyz archive)
            import pkgutil
            import yaml
            config_data = pkgutil.get_data('config', 'mining_pools_config.yaml')
            config = yaml.safe_load(config_data.decode('utf-8'))

            scenario_name = self.options.pool_scenario
            if scenario_name not in config:
                raise ValueError(f"Scenario '{scenario_name}' not found in config")

            scenario = config[scenario_name]
            pools = []
            for pool_data in scenario.get('pools', []):
                pools.append(PoolProfile(
                    pool_id=pool_data['pool_id'],
                    hashrate_pct=pool_data['hashrate_pct'],
                    fork_preference=ForkPreference(pool_data.get('fork_preference', 'neutral')),
                    ideology_strength=pool_data.get('ideology_strength', pool_data.get('ideology_score', 0.5)),
                    profitability_threshold=pool_data.get('profitability_threshold', pool_data.get('switch_threshold_pct', 5.0) / 100.0),
                    max_loss_usd=pool_data.get('max_loss_usd'),
                    max_loss_pct=pool_data.get('max_loss_pct', 0.10),
                    initial_fork=pool_data.get('initial_fork'),
                ))
            self.pool_strategy = MiningPoolStrategy(pools)
            self.log.info(f"✓ Pool strategy initialized ({len(pools)} pools)")

            # Set initial hashrate from pool scenario.
            # If a pool has an explicit initial_fork, use that; otherwise fall
            # back to fork_preference (neutral pools split evenly).
            initial_fork_a = 0.0
            initial_fork_b = 0.0
            for pool in pools:
                if pool.initial_fork is not None:
                    start = pool.initial_fork
                elif pool.fork_preference == ForkPreference.V27:
                    start = 'fork_a'
                elif pool.fork_preference == ForkPreference.V26:
                    start = 'fork_b'
                else:
                    start = None  # Neutral — split below

                if start == 'fork_a':
                    initial_fork_a += pool.hashrate_pct
                elif start == 'fork_b':
                    initial_fork_b += pool.hashrate_pct
                else:
                    initial_fork_a += pool.hashrate_pct / 2
                    initial_fork_b += pool.hashrate_pct / 2

            self.current_fork_a_hashrate = initial_fork_a
            self.current_fork_b_hashrate = initial_fork_b

            # Set initial allocation in pool strategy.
            # Neutral pools with no initial_fork alternate to approximate 50/50.
            neutral_toggle = True
            for pool in pools:
                if pool.initial_fork is not None:
                    self.pool_strategy.current_allocation[pool.pool_id] = pool.initial_fork
                elif pool.fork_preference == ForkPreference.V27:
                    self.pool_strategy.current_allocation[pool.pool_id] = 'fork_a'
                elif pool.fork_preference == ForkPreference.V26:
                    self.pool_strategy.current_allocation[pool.pool_id] = 'fork_b'
                else:
                    # Neutral pools alternate between forks
                    self.pool_strategy.current_allocation[pool.pool_id] = 'fork_a' if neutral_toggle else 'fork_b'
                    neutral_toggle = not neutral_toggle

        except Exception as e:
            self.log.warning(f"Could not load pool config: {e}")
            self.log.info("Using static hashrate allocation")

            if self.options.initial_fork_a_hashrate is not None:
                self.current_fork_a_hashrate = self.options.initial_fork_a_hashrate
                self.current_fork_b_hashrate = 100.0 - self.options.initial_fork_a_hashrate
            else:
                self.current_fork_a_hashrate = 50.0
                self.current_fork_b_hashrate = 50.0

            self.pool_strategy = None

        self.log.info(f"\nInitial hashrate: fork_a={self.current_fork_a_hashrate:.1f}%, fork_b={self.current_fork_b_hashrate:.1f}%\n")

        # Load node metadata from network.yaml
        self.load_network_metadata()

        # Initialize economic node strategy (dynamic economic weight)
        try:
            import pkgutil
            econ_config_data = pkgutil.get_data('config', 'economic_nodes_config.yaml')
            econ_config = yaml.safe_load(econ_config_data.decode('utf-8'))

            economic_profiles = load_economic_nodes_from_network(
                self.node_metadata,
                econ_config,
                self.options.economic_scenario
            )

            # Read user_custody_fraction from per-scenario config entry if present.
            # Omitting it (None) leaves user node weights at their calibrated values,
            # reproducing all prior sweep results exactly.
            scenario_cfg = econ_config.get(self.options.economic_scenario, {})
            user_custody_fraction = scenario_cfg.get('user_custody_fraction', None)

            if economic_profiles:
                self.economic_strategy = EconomicNodeStrategy(
                    economic_profiles,
                    user_custody_fraction=user_custody_fraction,
                )

                # Calculate initial economic allocation (nodes start on their partition's fork)
                self.current_fork_a_economic, self.current_fork_b_economic = \
                    self.economic_strategy.calculate_economic_allocation(
                        time(), self.price_oracle
                    )

                # Calculate transactional weights (fee-generating activity)
                self.current_fork_a_transactional, self.current_fork_b_transactional = \
                    self.economic_strategy.get_fee_generation_weight()

                # Calculate solo miner hashrate
                self.current_fork_a_solo_hashrate, self.current_fork_b_solo_hashrate, solo_miners = \
                    self.economic_strategy.get_mining_allocation()

                econ_count = len(economic_profiles)
                econ_types = {}
                solo_miner_count = 0
                for p in economic_profiles:
                    t = p.node_type.value
                    econ_types[t] = econ_types.get(t, 0) + 1
                    if p.hashrate_pct > 0:
                        solo_miner_count += 1

                self.log.info(f"✓ Economic strategy initialized ({econ_count} nodes: {econ_types})")
                self.log.info(f"  Initial economic weight: fork_a={self.current_fork_a_economic:.1f}%, fork_b={self.current_fork_b_economic:.1f}%")
                self.log.info(f"  Initial transactional weight: fork_a={self.current_fork_a_transactional:.1f}%, fork_b={self.current_fork_b_transactional:.1f}%")
                if solo_miner_count > 0:
                    total_solo = self.current_fork_a_solo_hashrate + self.current_fork_b_solo_hashrate
                    self.log.info(f"  Solo miners: {solo_miner_count} nodes with {total_solo:.2f}% hashrate")
                    self.log.info(f"    fork_a: {self.current_fork_a_solo_hashrate:.2f}%, fork_b: {self.current_fork_b_solo_hashrate:.2f}%")
                self.log.info(f"  Update interval: {self.options.economic_update_interval}s")
            else:
                self.log.info("No economic/user nodes found in metadata, using static economic weight")
                self.current_fork_a_economic = self.options.fork_a_economic
                self.current_fork_b_economic = self.options.fork_b_economic
                # Without economic strategy, assume 50/50 transactional split
                self.current_fork_a_transactional = self.options.fork_a_economic
                self.current_fork_b_transactional = self.options.fork_b_economic
                self.economic_strategy = None

        except Exception as e:
            self.log.warning(f"Could not load economic config: {e}")
            self.log.info("Using static economic allocation from --fork-a-economic")
            self.current_fork_a_economic = self.options.fork_a_economic
            self.current_fork_b_economic = self.options.fork_b_economic
            self.current_fork_a_transactional = self.options.fork_a_economic
            self.current_fork_b_transactional = self.options.fork_b_economic
            self.economic_strategy = None

        # Initialize difficulty oracle (if enabled)
        if self.options.enable_difficulty:
            self.tick_interval = self.options.tick_interval
            self.difficulty_oracle = DifficultyOracle(
                target_block_interval=float(self.options.interval),
                retarget_interval=self.options.retarget_interval,
                pre_fork_difficulty=1.0,
                max_adjustment_factor=4.0,
                min_difficulty=self.options.min_difficulty,
                enable_eda=self.options.enable_eda,
            )
            self.difficulty_oracle.initialize_fork('fork_a', initial_height=self.options.start_height)
            self.difficulty_oracle.initialize_fork('fork_b', initial_height=self.options.start_height)
            self.log.info(f"Difficulty oracle initialized:")
            self.log.info(f"  Target interval: {self.options.interval}s, Retarget every {self.options.retarget_interval} blocks")
            self.log.info(f"  Tick interval: {self.tick_interval}s, Min difficulty: {self.options.min_difficulty}")
            self.log.info(f"  EDA: {'enabled' if self.options.enable_eda else 'disabled'}")

        # Partition nodes
        self.partition_nodes_by_version()

        # Identify foreign-accepting nodes for asymmetric fork propagation
        self.build_foreign_accepting_nodes()

        # Build partition peer lists for dynamic switching
        self.build_partition_peer_lists()

        # Build pool-to-node mapping
        if self.pool_strategy:
            self.build_pool_node_mapping()

        # Auto-detect starting height BEFORE reorg oracle initialization
        # This ensures lca_height matches the actual fork point
        if self.options.start_height == 101:  # Default value, auto-detect instead
            detected_heights = []
            for node in (self.fork_a_nodes + self.fork_b_nodes)[:3]:  # Sample first few nodes
                try:
                    h = node.getblockcount()
                    detected_heights.append(h)
                except:
                    pass
            if detected_heights:
                # Use minimum as common ancestor (conservative)
                self.options.start_height = min(detected_heights)
                self.log.info(f"Auto-detected start_height={self.options.start_height} from nodes")

        # Initialize reorg oracle (if enabled) - AFTER auto-detection so lca_height is correct
        if self.options.enable_reorg_metrics:
            # Get initial fork height (LCA = starting height before fork diverges)
            lca_height = self.options.start_height

            # Determine total nodes from pool strategy if available
            total_pool_nodes = len(self.pool_strategy.pools) if self.pool_strategy else 8

            self.reorg_oracle = ReorgOracle(
                lca_height=lca_height,
                lca_hash="fork-point",
                propagation_window=30.0,
                total_nodes=total_pool_nodes
            )
            # Initialize both forks from the LCA
            self.reorg_oracle.initialize_fork('fork_a', lca_height)
            self.reorg_oracle.initialize_fork('fork_b', lca_height)

            self.log.info(f"✓ Reorg oracle initialized (LCA height={lca_height})")

            # Register pool nodes with reorg oracle
            if self.pool_strategy:
                for pool_id, pool in self.pool_strategy.pools.items():
                    initial_fork = self.pool_strategy.current_allocation.get(pool_id, 'fork_a')
                    self.reorg_oracle.register_node(pool_id, initial_fork)
                    # Also set current_fork on pool profile for tracking
                    pool.current_fork = initial_fork
                self.log.info(f"  Registered {len(self.pool_strategy.pools)} pools with reorg oracle")

        # Main mining loop
        start_time = time()
        last_hashrate_update = start_time
        last_price_update = start_time
        last_economic_update = start_time
        last_snapshot = start_time

        # Initialize time-limited UASF tracking
        if self.options.uasf_duration is not None:
            self.uasf_start_time = start_time
            self.uasf_expiry_time = start_time + self.options.uasf_duration
            self.uasf_active = True
            self.uasf_expired = False
            uasf_hours = self.options.uasf_duration / 3600
            self.log.info(f"\n{'='*70}")
            self.log.info(f"TIME-LIMITED UASF ENABLED")
            self.log.info(f"{'='*70}")
            self.log.info(f"  Duration: {self.options.uasf_duration}s ({uasf_hours:.1f} hours)")
            self.log.info(f"  Expiry action: {self.options.uasf_expiry_action}")
            self.log.info(f"  fork_a nodes will enforce strict rules until UASF expires")
            self.log.info(f"{'='*70}\n")

        # Capture initial snapshot
        self.capture_time_series_snapshot(0)

        self.log.info(f"\n{'='*70}")
        if self.difficulty_oracle:
            self.log.info(f"Starting partition mining (DIFFICULTY MODE)...")
        else:
            self.log.info(f"Starting partition mining (LEGACY MODE)...")
        self.log.info(f"{'='*70}\n")

        while time() - start_time < self.options.duration:
            current_time = time()
            elapsed = int(current_time - start_time)

            # Check for UASF expiry
            if (self.uasf_active and not self.uasf_expired and
                self.uasf_expiry_time is not None and
                current_time >= self.uasf_expiry_time):
                self.handle_uasf_expiry(elapsed, current_time)

            # Update economic node allocation (every 5 minutes by default)
            if self.economic_strategy and (current_time - last_economic_update >= self.options.economic_update_interval):
                old_fork_a_econ = self.current_fork_a_economic
                old_fork_b_econ = self.current_fork_b_economic

                self.current_fork_a_economic, self.current_fork_b_economic = \
                    self.economic_strategy.calculate_economic_allocation(
                        current_time, self.price_oracle
                    )

                # Update transactional weights (for fee calculations)
                self.current_fork_a_transactional, self.current_fork_b_transactional = \
                    self.economic_strategy.get_fee_generation_weight()

                # Update solo miner hashrate allocation
                old_fork_a_solo = self.current_fork_a_solo_hashrate
                old_fork_b_solo = self.current_fork_b_solo_hashrate
                self.current_fork_a_solo_hashrate, self.current_fork_b_solo_hashrate, _ = \
                    self.economic_strategy.get_mining_allocation()

                econ_change = abs(self.current_fork_a_economic - old_fork_a_econ)
                solo_change = abs(self.current_fork_a_solo_hashrate - old_fork_a_solo)

                if econ_change > 0.5:  # More than 0.5% change
                    self.log.info(f"\n ECONOMIC REALLOCATION at {elapsed}s:")
                    self.log.info(f"   fork_a: {old_fork_a_econ:.1f}% -> {self.current_fork_a_economic:.1f}%")
                    self.log.info(f"   fork_b: {old_fork_b_econ:.1f}% -> {self.current_fork_b_economic:.1f}%")
                    self.log.info(f"   Transactional: fork_a={self.current_fork_a_transactional:.1f}%, fork_b={self.current_fork_b_transactional:.1f}%")

                    # Log solo miner changes if any
                    if solo_change > 0.01:
                        self.log.info(f"   Solo miners: fork_a={old_fork_a_solo:.2f}%->{self.current_fork_a_solo_hashrate:.2f}%, "
                                      f"fork_b={old_fork_b_solo:.2f}%->{self.current_fork_b_solo_hashrate:.2f}%")

                    # Log notable node decisions
                    recent = [d for d in self.economic_strategy.decision_history[-30:]
                              if d.timestamp >= last_economic_update]
                    for decision in recent:
                        if decision.ideology_override:
                            self.log.info(f"   {decision.node_id}: staying on {decision.chosen_fork} (ideology)")
                        elif decision.inertia_held:
                            self.log.info(f"   {decision.node_id}: staying on {decision.chosen_fork} (inertia)")

                last_economic_update = current_time

            # Capture time series snapshot at regular intervals
            if current_time - last_snapshot >= self.options.snapshot_interval:
                self.capture_time_series_snapshot(elapsed)
                last_snapshot = current_time

            # Update prices (every minute by default)
            if current_time - last_price_update >= self.options.price_update_interval:
                # Use blocks_mined counters for reliable fork depth calculation
                # (node getblockcount() can be unreliable in partitioned networks)
                fork_depth = self.blocks_mined['fork_a'] + self.blocks_mined['fork_b']

                # Still get heights for price oracle (but fork_depth is what matters for sustained check)
                fork_a_height = self.options.start_height + self.blocks_mined['fork_a']
                fork_b_height = self.options.start_height + self.blocks_mined['fork_b']

                # Chainwork-based chain weights if difficulty oracle available
                fork_a_cw_override = None
                fork_b_cw_override = None
                if self.difficulty_oracle:
                    fork_a_cw_override = self.difficulty_oracle.get_chain_weight('fork_a')
                    fork_b_cw_override = self.difficulty_oracle.get_chain_weight('fork_b')

                # Debug: log price update inputs
                if self.options.debug_prices:
                    self.log.info(f"  [PRICE UPDATE] fork_depth={fork_depth}, sustained={self.price_oracle.fork_sustained}")
                    self.log.info(f"  [PRICE UPDATE] chain_weights: fork_a={fork_a_cw_override}, fork_b={fork_b_cw_override}")
                    self.log.info(f"  [PRICE UPDATE] econ: fork_a={self.current_fork_a_economic}%, fork_b={self.current_fork_b_economic}%")
                    self.log.info(f"  [PRICE UPDATE] hash: fork_a={self.current_fork_a_hashrate}%, fork_b={self.current_fork_b_hashrate}%")

                old_fork_a_price = self.price_oracle.get_price('fork_a')
                old_fork_b_price = self.price_oracle.get_price('fork_b')

                # Liveness penalty: compute each chain's block production rate
                # relative to the simulation target (1 block per --interval seconds).
                # production_ratio = 1.0 means on-target; 0.0 means ghost town.
                fork_a_rate_ratio = None
                fork_b_rate_ratio = None
                if self.options.enable_liveness_penalty and self.difficulty_oracle:
                    target_bph = 3600.0 / self.difficulty_oracle.target_block_interval
                    fork_a_bph = self.difficulty_oracle.get_blocks_per_hour(
                        'fork_a', self.current_fork_a_hashrate
                    )
                    fork_b_bph = self.difficulty_oracle.get_blocks_per_hour(
                        'fork_b', self.current_fork_b_hashrate
                    )
                    fork_a_rate_ratio = min(1.0, fork_a_bph / target_bph)
                    fork_b_rate_ratio = min(1.0, fork_b_bph / target_bph)
                    if self.options.debug_prices:
                        self.log.info(
                            f"  [PRICE UPDATE] liveness: target={target_bph:.1f} bph, "
                            f"fork_a={fork_a_bph:.2f} bph (ratio={fork_a_rate_ratio:.3f}), "
                            f"fork_b={fork_b_bph:.2f} bph (ratio={fork_b_rate_ratio:.3f})"
                        )

                self.price_oracle.update_prices_from_state(
                    fork_a_height=fork_a_height,
                    fork_b_height=fork_b_height,
                    fork_a_economic_pct=self.current_fork_a_economic,
                    fork_b_economic_pct=self.current_fork_b_economic,
                    fork_a_hashrate_pct=self.current_fork_a_hashrate,
                    fork_b_hashrate_pct=self.current_fork_b_hashrate,
                    common_ancestor_height=self.options.start_height,
                    fork_a_chain_weight_override=fork_a_cw_override,
                    fork_b_chain_weight_override=fork_b_cw_override,
                    fork_a_block_rate_ratio=fork_a_rate_ratio,
                    fork_b_block_rate_ratio=fork_b_rate_ratio,
                )

                new_fork_a_price = self.price_oracle.get_price('fork_a')
                new_fork_b_price = self.price_oracle.get_price('fork_b')

                if self.options.debug_prices:
                    self.log.info(f"  [PRICE UPDATE] prices: fork_a=${old_fork_a_price:,.0f}->${new_fork_a_price:,.0f}, fork_b=${old_fork_b_price:,.0f}->${new_fork_b_price:,.0f}")

                # Update fees based on network state
                if self.difficulty_oracle:
                    fork_a_blocks_per_hour = self.difficulty_oracle.get_blocks_per_hour('fork_a', self.current_fork_a_hashrate)
                    fork_b_blocks_per_hour = self.difficulty_oracle.get_blocks_per_hour('fork_b', self.current_fork_b_hashrate)
                else:
                    fork_a_blocks_per_hour = (self.blocks_mined['fork_a'] / max(1, elapsed/3600))
                    fork_b_blocks_per_hour = (self.blocks_mined['fork_b'] / max(1, elapsed/3600))

                self.fee_oracle.update_fees_from_state(
                    fork_a_blocks_per_hour=fork_a_blocks_per_hour,
                    fork_b_blocks_per_hour=fork_b_blocks_per_hour,
                    fork_a_economic_pct=self.current_fork_a_economic,
                    fork_b_economic_pct=self.current_fork_b_economic,
                    price_oracle=self.price_oracle,
                    difficulty_oracle=self.difficulty_oracle,
                    fork_a_hashrate_pct=self.current_fork_a_hashrate,
                    fork_b_hashrate_pct=self.current_fork_b_hashrate,
                    # Pass transactional weights for fee calculation
                    # Fees are driven by transaction activity, not custody holdings
                    fork_a_transactional_pct=self.current_fork_a_transactional,
                    fork_b_transactional_pct=self.current_fork_b_transactional,
                )

                last_price_update = current_time

            # Update hashrate allocation (every 10 minutes by default)
            # Skip if reunion has completed - all hashrate is already on winning fork
            if self.reunion_completed:
                pass  # Hashrate locked to winning fork after reunion
            elif self.pool_strategy and (current_time - last_hashrate_update >= self.options.hashrate_update_interval):

                # Pools make decisions
                old_fork_a_hash = self.current_fork_a_hashrate
                old_fork_b_hash = self.current_fork_b_hashrate

                self.current_fork_a_hashrate, self.current_fork_b_hashrate = \
                    self.pool_strategy.calculate_hashrate_allocation(
                        current_time, self.price_oracle, self.fee_oracle,
                        difficulty_oracle=self.difficulty_oracle,
                    )

                # Ensure fork heights are current before detecting reorgs
                if self.reorg_oracle:
                    self.reorg_oracle.update_fork_heights(
                        fork_a_height=self.options.start_height + self.blocks_mined['fork_a'],
                        fork_b_height=self.options.start_height + self.blocks_mined['fork_b']
                    )

                # Detect fork switches and record reorgs
                if self.reorg_oracle:
                    for pool_id, pool in self.pool_strategy.pools.items():
                        old_fork = pool.current_fork
                        new_fork = self.pool_strategy.current_allocation.get(pool_id, old_fork)

                        if old_fork != new_fork:
                            # Pool is switching forks - this causes a reorg!
                            reorg_event = self.reorg_oracle.record_fork_switch(
                                node_id=pool_id,
                                old_fork=old_fork,
                                new_fork=new_fork,
                                sim_time=elapsed
                            )
                            self.log.info(f"  REORG: {pool_id} switched {old_fork}->{new_fork}, "
                                          f"depth={reorg_event.depth}, orphaned={len(reorg_event.blocks_invalidated)} blocks")

                        # Update pool's current_fork tracking
                        pool.current_fork = new_fork

                # Log significant changes
                hash_change = abs(self.current_fork_a_hashrate - old_fork_a_hash)
                if hash_change > 1.0:  # More than 1% change
                    self.log.info(f"\n HASHRATE REALLOCATION at {elapsed}s:")
                    self.log.info(f"   fork_a: {old_fork_a_hash:.1f}% -> {self.current_fork_a_hashrate:.1f}%")
                    self.log.info(f"   fork_b: {old_fork_b_hash:.1f}% -> {self.current_fork_b_hashrate:.1f}%")

                    # Log pool switches
                    recent_decisions = [d for d in self.pool_strategy.decision_history[-20:]
                                      if d.timestamp >= last_hashrate_update]

                    for decision in recent_decisions:
                        if decision.ideology_override:
                            self.log.info(f"   {decision.pool_id}: mining {decision.chosen_fork} "
                                        f"despite ${decision.opportunity_cost_usd:,.0f} loss (ideology)")
                        elif decision.chosen_fork != decision.rational_choice:
                            self.log.info(f"   {decision.pool_id}: forced to switch to {decision.chosen_fork}")

                last_hashrate_update = current_time

                # Evaluate economic/user node partition switches
                # These nodes may switch partitions based on price, ideology, etc.
                if self.options.enable_dynamic_switching:
                    self.evaluate_economic_node_switches(elapsed)

            # === BLOCK PRODUCTION ===
            if self.difficulty_oracle:
                # DIFFICULTY MODE: per-tick probability for each fork independently
                # Supports multiple blocks per tick when block times are faster than tick interval
                sim_elapsed = elapsed  # Use wall-clock elapsed as sim_time

                for fork_id in ['fork_a', 'fork_b']:
                    hashrate_pct = self.current_fork_a_hashrate if fork_id == 'fork_a' else self.current_fork_b_hashrate

                    # Get number of blocks to mine this tick (can be 0, 1, or more)
                    blocks_to_mine = self.difficulty_oracle.get_blocks_to_mine(fork_id, hashrate_pct, self.tick_interval)

                    for block_num in range(blocks_to_mine):
                        miner = self._select_miner_for_fork(fork_id)
                        if not miner:
                            continue

                        try:
                            miner_wallet = Commander.ensure_miner(miner)
                            address = miner_wallet.getnewaddress()
                            self.generatetoaddress(miner, 1, address, sync_fun=self.no_op)

                            # Asymmetric fork: push fork_a blocks into the fork_b island
                            self.propagate_to_foreign_accepting(miner, fork_id)

                            self.blocks_mined[fork_id] += 1
                            new_height = (self.fork_a_nodes[0].getblockcount() if fork_id == 'fork_a' and self.fork_a_nodes
                                          else self.fork_b_nodes[0].getblockcount() if fork_id == 'fork_b' and self.fork_b_nodes
                                          else self.options.start_height + self.blocks_mined[fork_id])

                            # Record block with reorg oracle
                            if self.reorg_oracle:
                                pool_id = self.get_node_pool_id(miner)
                                if pool_id:
                                    self.reorg_oracle.record_block_mined(pool_id, fork_id, new_height)
                                # Update fork heights
                                self.reorg_oracle.update_fork_heights(
                                    fork_a_height=self.options.start_height + self.blocks_mined['fork_a'],
                                    fork_b_height=self.options.start_height + self.blocks_mined['fork_b']
                                )

                            retarget_event = self.difficulty_oracle.record_block(fork_id, sim_elapsed, new_height)

                            if retarget_event:
                                eda_str = " (EDA)" if retarget_event.is_eda else ""
                                self.log.info(
                                    f"  >> {fork_id} RETARGET{eda_str} at {elapsed}s: "
                                    f"difficulty {retarget_event.old_difficulty:.6f} -> {retarget_event.new_difficulty:.6f} "
                                    f"(factor={retarget_event.adjustment_factor:.3f})"
                                )

                            # Log the block (show multi-block indicator if applicable)
                            fork_a_price = self.price_oracle.get_price('fork_a')
                            fork_b_price = self.price_oracle.get_price('fork_b')
                            fork_status = "SUSTAINED" if self.price_oracle.fork_sustained else "natural split"
                            diff_state = self.difficulty_oracle.forks[fork_id]
                            pool_id = self.get_node_pool_id(miner)
                            pool_str = f"({pool_id})" if pool_id else ""
                            multi_str = f" [{block_num+1}/{blocks_to_mine}]" if blocks_to_mine > 1 else ""

                            self.log.info(
                                f"[{elapsed:4d}s] {fork_id} block {pool_str:15s}{multi_str} | "
                                f"Blks: fork_a={self.blocks_mined['fork_a']:3d} fork_b={self.blocks_mined['fork_b']:3d} | "
                                f"Diff: {diff_state.current_difficulty:.4f} | "
                                f"Hash: {self.current_fork_a_hashrate:4.1f}%/{self.current_fork_b_hashrate:4.1f}% | "
                                f"Price: ${fork_a_price:,.0f}/${fork_b_price:,.0f} [{fork_status}]"
                            )

                        except Exception as e:
                            self.log.error(f"Error mining {fork_id} block: {e}")

                sleep(self.tick_interval)

            else:
                # LEGACY MODE: one block per interval, probabilistic fork selection
                miner, partition = self.select_mining_node()

                if not miner:
                    self.log.warning("No miner available, skipping block")
                    sleep(self.options.interval)
                    continue

                try:
                    miner_wallet = Commander.ensure_miner(miner)
                    address = miner_wallet.getnewaddress()
                    self.generatetoaddress(miner, 1, address, sync_fun=self.no_op)

                    # Asymmetric fork: push fork_a blocks into the fork_b island
                    self.propagate_to_foreign_accepting(miner, partition)

                    self.blocks_mined[partition] += 1

                    fork_a_height = self.fork_a_nodes[0].getblockcount() if self.fork_a_nodes else 0
                    fork_b_height = self.fork_b_nodes[0].getblockcount() if self.fork_b_nodes else 0
                    fork_depth = fork_a_height + fork_b_height - (2 * self.options.start_height)

                    # Record block with reorg oracle
                    if self.reorg_oracle:
                        pool_id = self.get_node_pool_id(miner)
                        if pool_id:
                            block_height = fork_a_height if partition == 'fork_a' else fork_b_height
                            self.reorg_oracle.record_block_mined(pool_id, partition, block_height)
                        # Update fork heights
                        self.reorg_oracle.update_fork_heights(fork_a_height, fork_b_height)

                    # Get current state
                    fork_a_price = self.price_oracle.get_price('fork_a')
                    fork_b_price = self.price_oracle.get_price('fork_b')
                    price_ratio = fork_a_price / fork_b_price if fork_b_price > 0 else 1.0

                    fork_status = "SUSTAINED" if self.price_oracle.fork_sustained else "natural split"

                    # Get pool name for logging
                    pool_id = self.get_node_pool_id(miner)
                    pool_str = f"({pool_id})" if pool_id else ""

                    self.log.info(
                        f"[{elapsed:4d}s] {partition} block {pool_str:15s} | "
                        f"Heights: fork_a={fork_a_height:3d} fork_b={fork_b_height:3d} | "
                        f"Hash: {self.current_fork_a_hashrate:4.1f}%/{self.current_fork_b_hashrate:4.1f}% | "
                        f"Econ: {self.current_fork_a_economic:4.1f}%/{self.current_fork_b_economic:4.1f}% | "
                        f"Price: ${fork_a_price:,.0f}/${fork_b_price:,.0f} [{fork_status}]"
                    )

                except Exception as e:
                    self.log.error(f"Error mining block: {e}")

                sleep(self.options.interval)

        # Fork reunion (before final summary so we can log it inline)
        reunion_results = self.reunite_forks()

        # Final summary
        elapsed_min = (time() - start_time) / 60.0
        total_blocks = self.blocks_mined['fork_a'] + self.blocks_mined['fork_b']

        self.log.info(f"\n{'='*70}")
        self.log.info(f"Partition Mining Complete")
        self.log.info(f"{'='*70}")
        self.log.info(f"Duration: {elapsed_min:.2f} minutes")
        self.log.info(f"Blocks mined: fork_a={self.blocks_mined['fork_a']}, fork_b={self.blocks_mined['fork_b']}, total={total_blocks}")

        # Final prices
        fork_a_price = self.price_oracle.get_price('fork_a')
        fork_b_price = self.price_oracle.get_price('fork_b')

        self.log.info(f"\nFinal State:")
        self.log.info(f"  Prices: fork_a=${fork_a_price:,.0f}, fork_b=${fork_b_price:,.0f}")
        self.log.info(f"  Hashrate: fork_a={self.current_fork_a_hashrate:.1f}%, fork_b={self.current_fork_b_hashrate:.1f}%")
        self.log.info(f"  Economic: fork_a={self.current_fork_a_economic:.1f}%, fork_b={self.current_fork_b_economic:.1f}%")

        # Difficulty oracle summary
        if self.difficulty_oracle:
            self.log.info("")
            winner_id, winner_cw, loser_cw = self.difficulty_oracle.get_winning_fork()
            self.log.info(f"  Difficulty Model:")
            self.log.info(f"    Winning fork: {winner_id} (chainwork: {winner_cw:.4f} vs {loser_cw:.4f})")
            for fid, state in self.difficulty_oracle.forks.items():
                self.log.info(f"    {fid}: difficulty={state.current_difficulty:.6f}, "
                            f"chainwork={state.cumulative_chainwork:.4f}, "
                            f"chain_weight={self.difficulty_oracle.get_chain_weight(fid):.4f}")
            adj_count = len(self.difficulty_oracle.adjustment_history)
            eda_count = sum(1 for e in self.difficulty_oracle.adjustment_history if e.is_eda)
            self.log.info(f"    Retargets: {adj_count} (EDA: {eda_count})")

        # Pool strategy summary
        if self.pool_strategy:
            self.log.info("")
            self.pool_strategy.print_allocation_summary()

        # Economic strategy summary
        if self.economic_strategy:
            self.log.info("")
            self.economic_strategy.print_allocation_summary()

        # Dynamic partition switching summary
        if self.partition_switch_history:
            self.log.info("")
            self.log.info("=" * 50)
            self.log.info("DYNAMIC PARTITION SWITCHES")
            self.log.info("=" * 50)
            self.log.info(f"  Total switches: {len(self.partition_switch_history)}")

            # Count by direction
            fork_a_to_fork_b = sum(1 for s in self.partition_switch_history if s['from'] == 'fork_a')
            fork_b_to_fork_a = sum(1 for s in self.partition_switch_history if s['from'] == 'fork_b')
            self.log.info(f"  fork_a -> fork_b: {fork_a_to_fork_b}")
            self.log.info(f"  fork_b -> fork_a: {fork_b_to_fork_a}")

            # Final partition distribution
            fork_a_count = sum(1 for p in self.node_current_partition.values() if p == 'fork_a')
            fork_b_count = sum(1 for p in self.node_current_partition.values() if p == 'fork_b')
            self.log.info(f"  Final distribution: fork_a={fork_a_count} nodes, fork_b={fork_b_count} nodes")

            # List recent switches
            self.log.info("\n  Recent switches:")
            for switch in self.partition_switch_history[-10:]:
                self.log.info(f"    {switch['node']}: {switch['from']} -> {switch['to']} ({switch['reason']})")

        # Reorg oracle summary
        if self.reorg_oracle:
            # Calculate reunion analysis
            if self.difficulty_oracle:
                fork_a_chainwork = self.difficulty_oracle.get_cumulative_chainwork('fork_a')
                fork_b_chainwork = self.difficulty_oracle.get_cumulative_chainwork('fork_b')
            else:
                fork_a_chainwork = float(self.blocks_mined['fork_a'])
                fork_b_chainwork = float(self.blocks_mined['fork_b'])

            reunion = self.reorg_oracle.calculate_reunion_reorg(fork_a_chainwork, fork_b_chainwork)

            self.log.info("")
            self.reorg_oracle.print_summary()

            self.log.info(f"\nReunion Analysis (hypothetical partition merge):")
            self.log.info(f"  Winning fork: {reunion['winning_fork']}")
            self.log.info(f"  Losing fork depth: {reunion['losing_fork_depth']} blocks")
            self.log.info(f"  Nodes on losing fork: {reunion['num_nodes_on_losing_fork']}")
            self.log.info(f"  Reunion reorg mass: {reunion['reunion_reorg_mass']}")
            self.log.info(f"  Additional orphans on reunion: {reunion['additional_orphans']}")

        # UASF summary
        if self.options.uasf_duration is not None:
            self.log.info("")
            self.log.info("=" * 50)
            self.log.info("TIME-LIMITED UASF SUMMARY")
            self.log.info("=" * 50)
            self.log.info(f"  Duration configured: {self.options.uasf_duration}s ({self.options.uasf_duration/3600:.2f} hours)")
            self.log.info(f"  Expiry action: {self.options.uasf_expiry_action}")

            if self.uasf_expired and self.uasf_expiry_data:
                self.log.info(f"  UASF EXPIRED at {self.uasf_expiry_data['expiry_elapsed_seconds']}s")
                self.log.info(f"  Outcome: {self.uasf_expiry_data.get('uasf_outcome', 'unknown')}")

                state = self.uasf_expiry_data.get('state_at_expiry', {})
                self.log.info(f"  State at expiry:")
                self.log.info(f"    fork_a: {state.get('fork_a_blocks', 0)} blocks, chainwork={state.get('fork_a_chainwork', 0):.2f}")
                self.log.info(f"    fork_b: {state.get('fork_b_blocks', 0)} blocks, chainwork={state.get('fork_b_chainwork', 0):.2f}")
                self.log.info(f"    Winning fork: {state.get('winning_fork_at_expiry', 'unknown')}")

                if self.uasf_expiry_data.get('uasf_outcome') == 'failed':
                    self.log.info(f"  Orphaned blocks (fork_a): {self.uasf_expiry_data.get('orphaned_blocks', 0)}")
                    self.log.info(f"  Wasted chainwork: {self.uasf_expiry_data.get('wasted_chainwork', 0):.2f}")
            else:
                remaining = self.options.duration - (self.options.uasf_duration or 0)
                self.log.info(f"  UASF DID NOT EXPIRE (simulation ended before expiry)")
                self.log.info(f"  Remaining time before expiry: {remaining}s")

        # Export results
        try:
            import json
            import base64
            from datetime import datetime

            # Generate results ID if not provided
            results_id = self.options.results_id
            if not results_id:
                results_id = datetime.now().strftime("%Y%m%d_%H%M%S")

            # Build consolidated results object
            consolidated_results = {
                'metadata': {
                    'results_id': results_id,
                    'timestamp': datetime.now().isoformat(),
                    'duration_seconds': self.options.duration,
                    'pool_scenario': self.options.pool_scenario,
                    'economic_scenario': self.options.economic_scenario,
                    'difficulty_enabled': self.options.enable_difficulty,
                    'reorg_metrics_enabled': self.options.enable_reorg_metrics,
                    'interval': self.options.interval,
                    'start_height': self.options.start_height,
                    'uasf_duration': self.options.uasf_duration,
                    'uasf_expiry_action': self.options.uasf_expiry_action if self.options.uasf_duration else None,
                    'uasf_expired': self.uasf_expired,
                },
                'summary': {
                    'blocks_mined': dict(self.blocks_mined),
                    'total_blocks': self.blocks_mined['fork_a'] + self.blocks_mined['fork_b'],
                    'final_hashrate': {
                        'fork_a': self.current_fork_a_hashrate,
                        'fork_b': self.current_fork_b_hashrate,
                    },
                    'final_economic': {
                        'fork_a': self.current_fork_a_economic,
                        'fork_b': self.current_fork_b_economic,
                    },
                    'final_prices': {
                        'fork_a': self.price_oracle.get_price('fork_a'),
                        'fork_b': self.price_oracle.get_price('fork_b'),
                    },
                },
                'time_series': self.time_series,  # For charting
                'partition_switches': {
                    'total_switches': len(self.partition_switch_history),
                    'switches': self.partition_switch_history,
                    'final_partition_state': dict(self.node_current_partition),
                },
            }

            # Capture final snapshot
            self.capture_time_series_snapshot(int(time() - start_time))

            # Add oracle exports
            if self.pool_strategy:
                self.pool_strategy.export_to_json('/tmp/partition_pools.json')
                # Read back for consolidated export
                with open('/tmp/partition_pools.json', 'r') as f:
                    consolidated_results['pools'] = json.load(f)

            if self.economic_strategy:
                self.economic_strategy.export_to_json('/tmp/partition_economic.json')
                with open('/tmp/partition_economic.json', 'r') as f:
                    consolidated_results['economic'] = json.load(f)

            self.price_oracle.export_to_json('/tmp/partition_prices.json')
            with open('/tmp/partition_prices.json', 'r') as f:
                consolidated_results['prices'] = json.load(f)

            self.fee_oracle.export_to_json('/tmp/partition_fees.json')
            with open('/tmp/partition_fees.json', 'r') as f:
                consolidated_results['fees'] = json.load(f)

            if self.difficulty_oracle:
                self.difficulty_oracle.export_to_json('/tmp/partition_difficulty.json')
                with open('/tmp/partition_difficulty.json', 'r') as f:
                    consolidated_results['difficulty'] = json.load(f)

            if self.reorg_oracle:
                reorg_export = self.reorg_oracle.export_to_json()
                if self.difficulty_oracle:
                    fork_a_cw = self.difficulty_oracle.get_cumulative_chainwork('fork_a')
                    fork_b_cw = self.difficulty_oracle.get_cumulative_chainwork('fork_b')
                else:
                    fork_a_cw = float(self.blocks_mined['fork_a'])
                    fork_b_cw = float(self.blocks_mined['fork_b'])
                reorg_export['reunion_analysis'] = self.reorg_oracle.calculate_reunion_reorg(fork_a_cw, fork_b_cw)
                consolidated_results['reorg'] = reorg_export
                with open('/tmp/partition_reorg.json', 'w') as f:
                    json.dump(reorg_export, f, indent=2)

            # Fork reunion results
            consolidated_results['reunion'] = reunion_results

            # UASF expiry data
            if self.uasf_expiry_data is not None:
                consolidated_results['uasf'] = self.uasf_expiry_data
            elif self.options.uasf_duration is not None:
                # UASF was configured but didn't expire during simulation
                consolidated_results['uasf'] = {
                    'configured': True,
                    'duration_seconds': self.options.uasf_duration,
                    'expiry_action': self.options.uasf_expiry_action,
                    'expired': False,
                    'reason': 'simulation_ended_before_uasf_expiry'
                }

            # Save consolidated results to file
            with open('/tmp/partition_results.json', 'w') as f:
                json.dump(consolidated_results, f, indent=2)

            self.log.info(f"\n Results exported to /tmp/partition_*.json")
            self.log.info(f" Results ID: {results_id}")

            # Output base64-encoded results to logs for extraction
            # This survives pod termination since it's in the logs
            results_json = json.dumps(consolidated_results)
            results_b64 = base64.b64encode(results_json.encode()).decode()

            self.log.info(f"\n{'='*70}")
            self.log.info("RESULTS_EXPORT_START")
            self.log.info(f"RESULTS_ID:{results_id}")
            # Split into chunks for readability (some log systems have line limits)
            chunk_size = 1000
            for i in range(0, len(results_b64), chunk_size):
                self.log.info(f"RESULTS_DATA:{results_b64[i:i+chunk_size]}")
            self.log.info("RESULTS_EXPORT_END")
            self.log.info(f"{'='*70}")

        except Exception as e:
            self.log.error(f"Error exporting results: {e}")
            import traceback
            self.log.error(traceback.format_exc())

        self.log.info(f"{'='*70}\n")


def main():
    ForkExperiment("").main()


if __name__ == "__main__":
    main()
