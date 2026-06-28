#!/usr/bin/env python3

"""
Reorg Metrics Oracle - Fork Impact Tracking

Tracks chain reorganization events and their impact on network nodes during
fork competition scenarios. Captures the "cost" of reorgs in terms of:
- Wasted work (orphaned blocks)
- Network instability (reorg frequency)
- Miner orphan rates

Key Concepts:
- Node-Local Reorg Event: R_i = (t_i, h_i, d_i, f_old, f_new)
- Fork Incident: Grouped reorgs sharing same LCA and winning fork
- Metrics: ReorgMass, Penetration, Node Exposure, Orphan Rate, Fork Volatility
"""

import json
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Set
from collections import defaultdict


@dataclass
class ReorgEvent:
    """
    Node-local reorg event.

    Represents a single node experiencing a chain reorganization when
    switching from one fork to another.

    Attributes:
        timestamp: Simulation time when reorg occurred
        node_id: Which node (pool) experienced this reorg
        lca_height: Height of last common ancestor (fork point)
        lca_hash: LCA block hash (for clustering into incidents)
        depth: Number of blocks invalidated (reorg depth)
        fork_old: Fork being abandoned
        fork_new: Fork being adopted
        blocks_invalidated: List of block heights this node mined that were orphaned
    """
    timestamp: float
    node_id: str
    lca_height: int
    lca_hash: str
    depth: int
    fork_old: str
    fork_new: str
    blocks_invalidated: List[int] = field(default_factory=list)


@dataclass
class ForkIncident:
    """
    Network-level incident grouping related reorgs.

    Multiple node-local events can be grouped into a single fork incident
    if they share the same LCA block hash and winning fork.

    Attributes:
        incident_id: Unique identifier for this incident
        lca_hash: Common ancestor block hash
        winning_fork: Fork that nodes are switching TO
        events: List of ReorgEvents comprising this incident
        first_observed: Timestamp of first event in incident
        last_observed: Timestamp of last event in incident
    """
    incident_id: str
    lca_hash: str
    winning_fork: str
    events: List[ReorgEvent] = field(default_factory=list)
    first_observed: float = 0.0
    last_observed: float = 0.0

    @property
    def reorg_mass(self) -> int:
        """Total blocks invalidated across all events in this incident."""
        return sum(e.depth for e in self.events)

    def penetration(self, total_nodes: int) -> float:
        """Fraction of network nodes affected by this incident."""
        if total_nodes <= 0:
            return 0.0
        unique_nodes = len(set(e.node_id for e in self.events))
        return unique_nodes / total_nodes


@dataclass
class NodeMetrics:
    """
    Per-node tracking of reorg impact.

    Tracks cumulative metrics for a single node over the simulation.

    Attributes:
        node_id: Node identifier
        fork_id: Current fork this node is on
        blocks_mined: Total blocks mined by this node
        blocks_mined_per_fork: Blocks mined on each fork
        blocks_orphaned: Blocks mined then invalidated due to reorgs
        total_exposure: Sum of reorg depths experienced
        reorg_count: Number of reorg events experienced
        time_on_losing_fork: Cumulative time spent on non-winning fork
    """
    node_id: str
    fork_id: str
    blocks_mined: int = 0
    blocks_mined_per_fork: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    blocks_orphaned: int = 0
    total_exposure: int = 0
    reorg_count: int = 0
    time_on_losing_fork: float = 0.0


class ReorgOracle:
    """
    Oracle for tracking chain reorganization events during fork simulations.

    Monitors pool/node fork switches and calculates various reorg metrics
    including orphan rates, reorg mass, penetration, and consensus stress.

    In a partitioned fork simulation:
    - Fork formation: Chains diverge from a common LCA
    - Fork growth: Each fork grows independently with allocated hashrate
    - Pool switching: When pools change fork preference, they experience a reorg

    Primary reorg trigger: Pool switching forks due to profitability decisions.
    """

    def __init__(
        self,
        lca_height: int,
        lca_hash: str = "genesis",
        propagation_window: float = 30.0,
        total_nodes: int = 1
    ):
        """
        Initialize the ReorgOracle.

        Args:
            lca_height: Height of last common ancestor (fork point)
            lca_hash: Hash of LCA block (for incident clustering)
            propagation_window: Seconds within which reorgs are considered simultaneous
            total_nodes: Total number of nodes for penetration calculation
        """
        self.lca_height = lca_height
        self.lca_hash = lca_hash
        self.propagation_window = propagation_window
        self.total_nodes = max(1, total_nodes)

        # Fork state tracking
        self.fork_heights: Dict[str, int] = {}  # fork_id -> current height
        self.fork_blocks: Dict[str, List[int]] = defaultdict(list)  # fork_id -> block heights

        # Node state tracking
        self.node_metrics: Dict[str, NodeMetrics] = {}  # node_id -> metrics

        # Reorg event history
        self.reorg_events: List[ReorgEvent] = []
        self.fork_incidents: List[ForkIncident] = []
        self._incident_counter = 0

        # Blocks mined per node per fork (for orphan tracking)
        self.node_blocks: Dict[str, Dict[str, List[int]]] = defaultdict(lambda: defaultdict(list))

    def initialize_fork(self, fork_id: str, initial_height: int) -> None:
        """
        Set up fork tracking from LCA.

        Args:
            fork_id: Fork identifier ('fork_a' or 'fork_b')
            initial_height: Starting height (should match lca_height)
        """
        self.fork_heights[fork_id] = initial_height
        self.fork_blocks[fork_id] = []

    def register_node(self, node_id: str, fork_id: str) -> None:
        """
        Register a node's initial fork assignment.

        Args:
            node_id: Node/pool identifier
            fork_id: Initial fork assignment
        """
        self.node_metrics[node_id] = NodeMetrics(
            node_id=node_id,
            fork_id=fork_id,
            blocks_mined_per_fork=defaultdict(int)
        )

    def record_block_mined(
        self,
        node_id: str,
        fork_id: str,
        height: int
    ) -> None:
        """
        Track a block mined by a node on a specific fork.

        Args:
            node_id: Node/pool that mined the block
            fork_id: Fork the block was mined on
            height: Block height
        """
        # Register node if not already known
        if node_id not in self.node_metrics:
            self.register_node(node_id, fork_id)

        metrics = self.node_metrics[node_id]
        metrics.blocks_mined += 1
        metrics.blocks_mined_per_fork[fork_id] += 1

        # Track block for orphan detection
        self.node_blocks[node_id][fork_id].append(height)
        self.fork_blocks[fork_id].append(height)

    def update_fork_heights(self, fork_a_height: int, fork_b_height: int) -> None:
        """
        Update current fork tip heights.

        Should be called each tick/block to keep fork state current.

        Args:
            fork_a_height: Current height of fork_a fork
            fork_b_height: Current height of fork_b fork
        """
        self.fork_heights['fork_a'] = fork_a_height
        self.fork_heights['fork_b'] = fork_b_height

    def _calculate_reorg_depth(self, node_id: str, old_fork: str) -> int:
        """
        Calculate reorg depth when switching away from a fork.

        Depth = current height of old fork - LCA height

        Args:
            node_id: Node experiencing the reorg
            old_fork: Fork being abandoned

        Returns:
            Number of blocks that must be invalidated
        """
        old_fork_height = self.fork_heights.get(old_fork, self.lca_height)
        depth = old_fork_height - self.lca_height
        return max(0, depth)

    def _find_orphaned_blocks(self, node_id: str, old_fork: str) -> List[int]:
        """
        Find blocks mined by node on old fork that are now orphaned.

        Args:
            node_id: Node experiencing the reorg
            old_fork: Fork being abandoned

        Returns:
            List of block heights that were orphaned
        """
        if node_id not in self.node_blocks:
            return []

        return list(self.node_blocks[node_id][old_fork])

    def record_fork_switch(
        self,
        node_id: str,
        old_fork: str,
        new_fork: str,
        sim_time: float
    ) -> ReorgEvent:
        """
        Record a reorg event when a pool/node switches forks.

        This is the KEY METHOD - called when a pool changes fork preference.
        The switch causes a reorg from the old fork's tip back to LCA, then
        forward on the new fork.

        Args:
            node_id: Node/pool switching forks
            old_fork: Fork being abandoned
            new_fork: Fork being adopted
            sim_time: Simulation time of the switch

        Returns:
            ReorgEvent capturing the reorg details
        """
        # Register node if not already known
        if node_id not in self.node_metrics:
            self.register_node(node_id, old_fork)

        # Calculate reorg depth
        depth = self._calculate_reorg_depth(node_id, old_fork)

        # Find orphaned blocks (blocks this node mined on old fork)
        orphaned_blocks = self._find_orphaned_blocks(node_id, old_fork)

        # Create reorg event
        event = ReorgEvent(
            timestamp=sim_time,
            node_id=node_id,
            lca_height=self.lca_height,
            lca_hash=self.lca_hash,
            depth=depth,
            fork_old=old_fork,
            fork_new=new_fork,
            blocks_invalidated=orphaned_blocks
        )

        # Update node metrics
        metrics = self.node_metrics[node_id]
        metrics.fork_id = new_fork
        metrics.blocks_orphaned += len(orphaned_blocks)
        metrics.total_exposure += depth
        metrics.reorg_count += 1

        # Clear orphaned blocks from tracking (they're gone)
        if node_id in self.node_blocks and old_fork in self.node_blocks[node_id]:
            self.node_blocks[node_id][old_fork] = []

        # Store event
        self.reorg_events.append(event)

        # Try to cluster into existing incident or create new one
        self._cluster_into_incident(event)

        return event

    def _cluster_into_incident(self, event: ReorgEvent) -> None:
        """
        Cluster a reorg event into an existing incident or create new one.

        Events are clustered if they:
        - Share the same LCA hash
        - Share the same winning (new) fork
        - Occur within the propagation window of existing events

        Args:
            event: ReorgEvent to cluster
        """
        # Look for matching incident
        for incident in self.fork_incidents:
            if (incident.lca_hash == event.lca_hash and
                incident.winning_fork == event.fork_new and
                abs(event.timestamp - incident.last_observed) <= self.propagation_window):
                # Add to existing incident
                incident.events.append(event)
                incident.last_observed = max(incident.last_observed, event.timestamp)
                return

        # Create new incident
        self._incident_counter += 1
        incident = ForkIncident(
            incident_id=f"incident-{self._incident_counter}",
            lca_hash=event.lca_hash,
            winning_fork=event.fork_new,
            events=[event],
            first_observed=event.timestamp,
            last_observed=event.timestamp
        )
        self.fork_incidents.append(incident)

    def get_orphan_rate(self, node_id: str) -> float:
        """
        Get orphan rate for a specific node.

        Orphan rate = blocks orphaned / blocks mined

        Args:
            node_id: Node identifier

        Returns:
            Orphan rate (0.0 to 1.0)
        """
        if node_id not in self.node_metrics:
            return 0.0

        metrics = self.node_metrics[node_id]
        if metrics.blocks_mined == 0:
            return 0.0

        return metrics.blocks_orphaned / metrics.blocks_mined

    def get_node_exposure(self, node_id: str) -> int:
        """
        Get total reorg depth experienced by a node.

        Args:
            node_id: Node identifier

        Returns:
            Total reorg depth (sum of all reorg depths)
        """
        if node_id not in self.node_metrics:
            return 0
        return self.node_metrics[node_id].total_exposure

    def get_reorg_count(self, node_id: str) -> int:
        """
        Get number of reorg events experienced by a node.

        Args:
            node_id: Node identifier

        Returns:
            Count of reorg events
        """
        if node_id not in self.node_metrics:
            return 0
        return self.node_metrics[node_id].reorg_count

    def get_total_reorg_mass(self) -> int:
        """
        Get total reorg mass across all events.

        ReorgMass = Σ d_i (sum of all reorg depths)

        Returns:
            Total blocks invalidated across all reorgs
        """
        return sum(e.depth for e in self.reorg_events)

    def get_normalized_reorg_mass(self) -> float:
        """
        Get normalized reorg mass (per-node average).

        NormalizedReorgMass = ReorgMass / N

        Returns:
            Average reorg depth per node
        """
        if self.total_nodes == 0:
            return 0.0
        return self.get_total_reorg_mass() / self.total_nodes

    def get_fork_volatility_index(self, elapsed_seconds: float) -> float:
        """
        Get fork volatility index.

        Fork Volatility Index = ReorgMass / time_window

        Args:
            elapsed_seconds: Time window in seconds

        Returns:
            Volatility index (reorg mass per second)
        """
        if elapsed_seconds <= 0:
            return 0.0
        return self.get_total_reorg_mass() / elapsed_seconds

    def get_consensus_stress_score(self) -> float:
        """
        Get consensus stress score.

        Consensus Stress = Σ (Penetration × NRM) for each incident

        Returns:
            Cumulative stress score
        """
        stress = 0.0
        for incident in self.fork_incidents:
            penetration = incident.penetration(self.total_nodes)
            nrm = incident.reorg_mass / max(1, self.total_nodes)
            stress += penetration * nrm
        return stress

    def calculate_reunion_reorg(
        self,
        fork_a_chainwork: float,
        fork_b_chainwork: float
    ) -> Dict:
        """
        Calculate hypothetical reunion reorg if partitions merged.

        At scenario end, determines what would happen if the network
        reconnected and had to converge on a single chain.

        Args:
            fork_a_chainwork: Cumulative chainwork of fork_a fork
            fork_b_chainwork: Cumulative chainwork of fork_b fork

        Returns:
            Dict with reunion analysis:
            - winning_fork: Fork with higher chainwork
            - losing_fork: Fork that would be abandoned
            - losing_fork_depth: Blocks on losing fork since LCA
            - nodes_on_losing_fork: List of nodes that would need to reorg
            - reunion_reorg_mass: Total blocks × affected nodes
            - additional_orphans: Blocks those nodes mined on losing fork
        """
        # Determine winner by chainwork (or height as proxy)
        if fork_a_chainwork >= fork_b_chainwork:
            winning_fork = 'fork_a'
            losing_fork = 'fork_b'
        else:
            winning_fork = 'fork_b'
            losing_fork = 'fork_a'

        # Calculate losing fork depth
        losing_fork_height = self.fork_heights.get(losing_fork, self.lca_height)
        losing_fork_depth = losing_fork_height - self.lca_height

        # Find nodes still on losing fork
        nodes_on_losing = []
        additional_orphans = 0

        for node_id, metrics in self.node_metrics.items():
            if metrics.fork_id == losing_fork:
                nodes_on_losing.append(node_id)
                # Count blocks they mined on losing fork that would be orphaned
                additional_orphans += metrics.blocks_mined_per_fork.get(losing_fork, 0)

        # Calculate reunion reorg mass
        reunion_reorg_mass = losing_fork_depth * len(nodes_on_losing)

        return {
            'winning_fork': winning_fork,
            'losing_fork': losing_fork,
            'losing_fork_depth': losing_fork_depth,
            'nodes_on_losing_fork': nodes_on_losing,
            'num_nodes_on_losing_fork': len(nodes_on_losing),
            'reunion_reorg_mass': reunion_reorg_mass,
            'additional_orphans': additional_orphans,
            'fork_a_chainwork': fork_a_chainwork,
            'fork_b_chainwork': fork_b_chainwork,
        }

    def get_network_summary(self) -> Dict:
        """
        Get comprehensive network-level metrics summary.

        Returns:
            Dict with all network metrics
        """
        total_blocks_mined = sum(
            m.blocks_mined for m in self.node_metrics.values()
        )
        total_blocks_orphaned = sum(
            m.blocks_orphaned for m in self.node_metrics.values()
        )

        return {
            'lca_height': self.lca_height,
            'lca_hash': self.lca_hash,
            'fork_a_final_height': self.fork_heights.get('fork_a', self.lca_height),
            'fork_b_final_height': self.fork_heights.get('fork_b', self.lca_height),
            'total_nodes_tracked': len(self.node_metrics),
            'total_blocks_mined': total_blocks_mined,
            'total_blocks_orphaned': total_blocks_orphaned,
            'total_reorg_events': len(self.reorg_events),
            'total_fork_incidents': len(self.fork_incidents),
            'total_reorg_mass': self.get_total_reorg_mass(),
            'normalized_reorg_mass': self.get_normalized_reorg_mass(),
            'consensus_stress_score': self.get_consensus_stress_score(),
        }

    def get_node_summary(self, node_id: str) -> Dict:
        """
        Get metrics summary for a specific node.

        Args:
            node_id: Node identifier

        Returns:
            Dict with node metrics
        """
        if node_id not in self.node_metrics:
            return {
                'node_id': node_id,
                'error': 'Node not found'
            }

        metrics = self.node_metrics[node_id]
        return {
            'node_id': node_id,
            'current_fork': metrics.fork_id,
            'blocks_mined': metrics.blocks_mined,
            'blocks_mined_per_fork': dict(metrics.blocks_mined_per_fork),
            'blocks_orphaned': metrics.blocks_orphaned,
            'orphan_rate': self.get_orphan_rate(node_id),
            'reorg_count': metrics.reorg_count,
            'total_exposure': metrics.total_exposure,
        }

    def get_all_node_summaries(self) -> Dict[str, Dict]:
        """
        Get metrics summaries for all tracked nodes.

        Returns:
            Dict mapping node_id to node summary
        """
        return {
            node_id: self.get_node_summary(node_id)
            for node_id in self.node_metrics
        }

    def export_to_json(self) -> Dict:
        """
        Export all metrics for post-run analysis.

        Returns:
            Dict containing full export data
        """
        return {
            'config': {
                'lca_height': self.lca_height,
                'lca_hash': self.lca_hash,
                'propagation_window': self.propagation_window,
                'total_nodes': self.total_nodes,
            },
            'fork_heights': dict(self.fork_heights),
            'network_summary': self.get_network_summary(),
            'node_summaries': self.get_all_node_summaries(),
            'reorg_events': [
                {
                    'timestamp': e.timestamp,
                    'node_id': e.node_id,
                    'lca_height': e.lca_height,
                    'depth': e.depth,
                    'fork_old': e.fork_old,
                    'fork_new': e.fork_new,
                    'blocks_orphaned': len(e.blocks_invalidated),
                }
                for e in self.reorg_events
            ],
            'fork_incidents': [
                {
                    'incident_id': i.incident_id,
                    'lca_hash': i.lca_hash,
                    'winning_fork': i.winning_fork,
                    'event_count': len(i.events),
                    'reorg_mass': i.reorg_mass,
                    'penetration': i.penetration(self.total_nodes),
                    'first_observed': i.first_observed,
                    'last_observed': i.last_observed,
                }
                for i in self.fork_incidents
            ],
        }

    def print_summary(self) -> None:
        """Print human-readable summary of reorg metrics."""
        print("\n" + "=" * 70)
        print("REORG METRICS SUMMARY")
        print("=" * 70)

        network = self.get_network_summary()
        print(f"\nFork State:")
        print(f"  LCA Height: {network['lca_height']}")
        print(f"  fork_a Final Height: {network['fork_a_final_height']}")
        print(f"  fork_b Final Height: {network['fork_b_final_height']}")

        print(f"\nReorg Statistics:")
        print(f"  Total Reorg Events: {network['total_reorg_events']}")
        print(f"  Total Fork Incidents: {network['total_fork_incidents']}")
        print(f"  Total Reorg Mass: {network['total_reorg_mass']} blocks")
        print(f"  Normalized Reorg Mass: {network['normalized_reorg_mass']:.2f} blocks/node")

        print(f"\nBlock Statistics:")
        print(f"  Total Blocks Mined: {network['total_blocks_mined']}")
        print(f"  Total Blocks Orphaned: {network['total_blocks_orphaned']}")
        if network['total_blocks_mined'] > 0:
            orphan_rate = network['total_blocks_orphaned'] / network['total_blocks_mined']
            print(f"  Network Orphan Rate: {orphan_rate * 100:.2f}%")

        print(f"\nConsensus Health:")
        print(f"  Consensus Stress Score: {network['consensus_stress_score']:.4f}")

        if self.reorg_events:
            print(f"\nReorg Events:")
            for event in self.reorg_events:
                orphaned = len(event.blocks_invalidated)
                print(f"  [{event.timestamp:.0f}s] {event.node_id}: "
                      f"{event.fork_old} -> {event.fork_new}, "
                      f"depth={event.depth}, orphaned={orphaned} blocks")

        print(f"\nPer-Node Summary:")
        for node_id in sorted(self.node_metrics.keys()):
            summary = self.get_node_summary(node_id)
            print(f"  {node_id}:")
            print(f"    Current Fork: {summary['current_fork']}")
            print(f"    Blocks Mined: {summary['blocks_mined']}")
            print(f"    Blocks Orphaned: {summary['blocks_orphaned']}")
            print(f"    Orphan Rate: {summary['orphan_rate'] * 100:.1f}%")
            print(f"    Reorg Count: {summary['reorg_count']}")
            print(f"    Total Exposure: {summary['total_exposure']}")

        print("\n" + "=" * 70)


# Standalone test block
if __name__ == "__main__":
    print("=" * 70)
    print("REORG ORACLE TEST")
    print("=" * 70)

    # Simulate a fork scenario
    oracle = ReorgOracle(
        lca_height=100,
        lca_hash="abc123",
        propagation_window=30.0,
        total_nodes=4
    )

    # Initialize forks
    oracle.initialize_fork('fork_a', 100)
    oracle.initialize_fork('fork_b', 100)

    # Register pools
    oracle.register_node('foundry', 'fork_a')
    oracle.register_node('antpool', 'fork_a')
    oracle.register_node('f2pool', 'fork_b')
    oracle.register_node('viabtc', 'fork_b')

    print("\nSimulating block production...")

    # Simulate blocks on each fork
    for i in range(25):
        oracle.record_block_mined('foundry', 'fork_a', 101 + i)
        oracle.record_block_mined('antpool', 'fork_a', 101 + i)
        if i < 22:  # fork_b is slightly behind
            oracle.record_block_mined('f2pool', 'fork_b', 101 + i)
            oracle.record_block_mined('viabtc', 'fork_b', 101 + i)

    # Update fork heights
    oracle.update_fork_heights(fork_a_height=125, fork_b_height=122)

    print(f"  fork_a: 25 blocks, height=125")
    print(f"  fork_b: 22 blocks, height=122")

    # Simulate antpool switching from fork_a to fork_b at t=300
    print("\nSimulating pool switch: antpool fork_a -> fork_b at t=300...")
    event1 = oracle.record_fork_switch(
        node_id='antpool',
        old_fork='fork_a',
        new_fork='fork_b',
        sim_time=300.0
    )
    print(f"  Reorg depth: {event1.depth}")
    print(f"  Blocks orphaned: {len(event1.blocks_invalidated)}")

    # More blocks mined
    for i in range(50):
        oracle.record_block_mined('foundry', 'fork_a', 126 + i)
        oracle.record_block_mined('antpool', 'fork_b', 123 + i)
        oracle.record_block_mined('f2pool', 'fork_b', 123 + i)
        oracle.record_block_mined('viabtc', 'fork_b', 123 + i)

    oracle.update_fork_heights(fork_a_height=175, fork_b_height=172)

    # Simulate viabtc also switching to fork_b at t=1800
    print("\nSimulating pool switch: viabtc staying on fork_b (no reorg)...")

    # Calculate reunion analysis
    print("\nCalculating reunion analysis...")
    reunion = oracle.calculate_reunion_reorg(
        fork_a_chainwork=175.0,  # Using height as proxy
        fork_b_chainwork=172.0
    )
    print(f"  Winning fork: {reunion['winning_fork']}")
    print(f"  Losing fork depth: {reunion['losing_fork_depth']}")
    print(f"  Nodes on losing fork: {reunion['nodes_on_losing_fork']}")
    print(f"  Reunion reorg mass: {reunion['reunion_reorg_mass']}")
    print(f"  Additional orphans on reunion: {reunion['additional_orphans']}")

    # Print full summary
    oracle.print_summary()

    # Test JSON export
    print("\nTesting JSON export...")
    export_data = oracle.export_to_json()
    print(f"  Export contains {len(export_data)} top-level keys")
    print(f"  Reorg events: {len(export_data['reorg_events'])}")
    print(f"  Fork incidents: {len(export_data['fork_incidents'])}")

    # Test metrics
    print("\nMetrics Tests:")
    print(f"  Fork Volatility Index (300s): {oracle.get_fork_volatility_index(300.0):.4f}")
    print(f"  Consensus Stress Score: {oracle.get_consensus_stress_score():.4f}")
    print(f"  antpool orphan rate: {oracle.get_orphan_rate('antpool') * 100:.1f}%")
    print(f"  antpool exposure: {oracle.get_node_exposure('antpool')}")
    print(f"  antpool reorg count: {oracle.get_reorg_count('antpool')}")

    print("\n" + "=" * 70)
    print("✓ ReorgOracle test complete")
    print("=" * 70)
