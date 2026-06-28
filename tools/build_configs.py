#!/usr/bin/env python3
"""
Step 2: Build Networks and Configurations

Takes the scenarios.json from step 1 and generates:
- Network YAML configs for each scenario
- Pool scenario configs
- Economic scenario configs
- Generated networks ready for warnet

Usage:
    # Build all configs from scenarios.json
    python 2_build_configs.py --input scenarios.json

    # Specify output directory
    python 2_build_configs.py --input scenarios.json --output-dir ./sweep_output

    # Build only configs (skip network generation)
    python 2_build_configs.py --input scenarios.json --configs-only

Output:
    output_dir/
    ├── networks/
    │   ├── sweep_0000/network.yaml
    │   ├── sweep_0001/network.yaml
    │   └── ...
    ├── configs/
    │   ├── network/
    │   │   ├── sweep_0000.yaml
    │   │   └── ...
    │   ├── pools/
    │   │   └── sweep_pools_config.yaml
    │   └── economic/
    │       └── sweep_economic_config.yaml
    └── build_manifest.json
"""

import argparse
import copy
import json
import os
import random
import shutil
import subprocess
import sys
import yaml
from pathlib import Path
from typing import Dict, List, Any, Optional



# Known network aliases → paths relative to the sweep tool directory
NETWORK_ALIASES = {
    'lite': '../../networks/realistic-economy-lite/network.yaml',
    'full': '../../networks/realistic-economy-v2/network.yaml',
}

# Real-world pool distribution (used when no base network specified)
DEFAULT_POOLS = [
    ("foundryusa", "Foundry USA", 26.89),
    ("antpool", "AntPool", 19.25),
    ("viabtc", "ViaBTC", 11.39),
    ("f2pool", "F2Pool", 11.25),
    ("binancepool", "Binance Pool", 10.04),
    ("marapool", "MARA Pool", 8.25),
    ("sbicrypto", "SBI Crypto", 4.57),
    ("luxor", "Luxor", 3.94),
    ("ocean", "OCEAN", 1.42),
    ("braiinspool", "Braiins Pool", 1.37),
]


def extract_pools_from_network(network: Dict) -> List[tuple]:
    """
    Extract pool definitions from a network.yaml file.

    Returns list of tuples: (pool_id, pool_name, hashrate_pct, fork_preference, initial_fork)
    """
    pools = []
    nodes = network.get('nodes', [])

    for node in nodes:
        metadata = node.get('metadata', {})
        if metadata.get('node_type') == 'mining_pool' or metadata.get('role') == 'mining_pool':
            pool_id = metadata.get('entity_id', node.get('name', 'unknown'))
            pool_name = metadata.get('pool_name', pool_id)
            hashrate = metadata.get('hashrate_pct', 0)
            fork_pref = metadata.get('fork_preference', 'neutral')
            # Initial fork is determined by the node's image tag
            image_tag = node.get('image', {}).get('tag', '27.0')
            initial_fork = 'v27' if '27' in str(image_tag) else 'v26'

            if hashrate > 0:
                pools.append((pool_id, pool_name, hashrate, fork_pref, initial_fork))

    return pools


def create_pool_scenario(scenario: Dict[str, Any], pools: List[tuple] = None) -> Dict[str, Any]:
    """Create mining pool scenario config from parameters.

    When pools is provided (from base network), uses the network's pool definitions
    directly, preserving fork_preference and initial_fork from the network structure.
    This is essential for balanced/custom networks.

    When pools is None (default), uses DEFAULT_POOLS with dynamic assignment:
    - fork_preference / ideology: controlled by pool_committed_split and pool_neutral_pct
    - initial_fork: controlled by hashrate_split
    """

    pool_configs = []

    if pools is not None:
        # Use pools from base network.
        # initial_fork is preserved from the network (correctly set by hashrate_split
        # via apply_scenario_to_base_network). fork_preference is computed dynamically
        # from pool_committed_split and pool_neutral_pct so those parameters are live.
        neutral_pct = scenario["pool_neutral_pct"] / 100
        committed_pct = 1.0 - neutral_pct
        split = scenario["pool_committed_split"]
        v27_pct = committed_pct * split
        v26_pct = committed_pct * (1.0 - split)

        total_hashrate = sum(p[2] for p in pools) or 100.0
        cumulative_hashrate = 0

        # Arm A: compositional mode — shuffle pool order before assignment so that
        # pool IDENTITY is decoupled from the aggregate hashrate scalar.
        # composition_seed controls the shuffle; absent = deterministic order (default).
        pool_list = list(pools)
        composition_seed = scenario.get("composition_seed")
        if composition_seed is not None:
            rng = random.Random(composition_seed)
            rng.shuffle(pool_list)

        for pool_tuple in pool_list:
            if len(pool_tuple) == 5:
                pool_id, pool_name, hashrate, _net_pref, initial_fork = pool_tuple
            else:
                pool_id, pool_name, hashrate = pool_tuple[:3]
                initial_fork = "v27"


            # Dynamic fork_preference assignment using cumulative hashrate position
            midpoint = (cumulative_hashrate + hashrate / 2) / total_hashrate
            if midpoint < v27_pct:
                fork_pref = "v27"
            elif midpoint < v27_pct + v26_pct:
                fork_pref = "v26"
            else:
                fork_pref = "neutral"

            if fork_pref == "neutral":
                ideology = 0.1
                max_loss = 0.02
            else:
                ideology = scenario["pool_ideology_strength"]
                max_loss = scenario["pool_max_loss_pct"]

            pool_configs.append({
                "pool_id": pool_id,
                "pool_name": pool_name,
                "hashrate_pct": hashrate,
                "fork_preference": fork_pref,
                "initial_fork": initial_fork,
                "ideology_strength": round(ideology, 3),
                "profitability_threshold": scenario["pool_profitability_threshold"],
                "max_loss_pct": round(max_loss, 3),
            })
            cumulative_hashrate += hashrate
    else:
        # Use DEFAULT_POOLS with dynamic assignment based on scenario parameters
        # --- Ideology / preference assignment (pool_committed_split, pool_neutral_pct) ---
        neutral_pct = scenario["pool_neutral_pct"] / 100
        committed_pct = 1.0 - neutral_pct
        split = scenario["pool_committed_split"]
        v27_pct = committed_pct * split
        v26_pct = committed_pct * (1.0 - split)

        # --- Initial fork assignment (hashrate_split) ---
        v27_init_threshold = scenario["hashrate_split"]

        cumulative_hashrate = 0

        for pool_id, pool_name, hashrate in DEFAULT_POOLS:
            midpoint = (cumulative_hashrate + hashrate / 2) / 100

            # Ideology assignment
            if midpoint < v27_pct:
                pref = "v27"
            elif midpoint < v27_pct + v26_pct:
                pref = "v26"
            else:
                pref = "neutral"

            # Initial fork assignment (independent of ideology)
            initial_fork = "v27" if midpoint < v27_init_threshold else "v26"

            # Neutral pools have minimal ideology
            if pref == "neutral":
                ideology = 0.1
                max_loss = 0.02
            else:
                ideology = scenario["pool_ideology_strength"]
                max_loss = scenario["pool_max_loss_pct"]

            pool_configs.append({
                "pool_id": pool_id,
                "pool_name": pool_name,
                "hashrate_pct": hashrate,
                "fork_preference": pref,
                "initial_fork": initial_fork,
                "ideology_strength": round(ideology, 3),
                "profitability_threshold": scenario["pool_profitability_threshold"],
                "max_loss_pct": round(max_loss, 3),
            })

            cumulative_hashrate += hashrate

    return {
        "description": f"Pool scenario for {scenario['scenario_id']}",
        "pools": pool_configs
    }


def create_economic_scenario(scenario: Dict[str, Any]) -> Dict[str, Any]:
    """Create economic scenario config from parameters"""
    cfg = {
        "description": f"Economic scenario for {scenario['scenario_id']}",
        "economic_defaults": {
            "fork_preference": "neutral",
            "ideology_strength": scenario["econ_ideology_strength"],
            "switching_threshold": scenario["econ_switching_threshold"],
            "inertia": scenario["econ_inertia"],
            "switching_cooldown": 300,
            "max_loss_pct": round(min(0.30, scenario["econ_ideology_strength"] * 0.5), 3),
        },
        "user_defaults": {
            "fork_preference": "neutral",
            "ideology_strength": scenario["user_ideology_strength"],
            "switching_threshold": scenario["user_switching_threshold"],
            "inertia": 0.05,
            "switching_cooldown": 600,
            "max_loss_pct": round(min(0.40, scenario["user_ideology_strength"] * 0.6), 3),
        }
    }
    # Inject user_custody_fraction only when explicitly set — omitting it from the
    # config leaves EconomicNodeStrategy at scale=1.0, reproducing all prior results.
    if scenario.get("user_custody_fraction") is not None:
        cfg["user_custody_fraction"] = round(float(scenario["user_custody_fraction"]), 6)
    return cfg


def create_network_config(scenario: Dict[str, Any]) -> Dict[str, Any]:
    """Create network generation config from parameters"""
    return {
        "name": scenario["scenario_id"],
        "description": f"Parameter sweep scenario {scenario['scenario_id']}",
        "v27_economic_pct": scenario["economic_split"] * 100,
        "v27_hashrate_pct": scenario["hashrate_split"] * 100,
        "economic_nodes_per_partition": int(scenario["economic_nodes_per_partition"]),
        "user_nodes_per_partition": int(scenario["user_nodes_per_partition"]),
        "econ_neutral_fraction": scenario.get("econ_neutral_fraction", 0.0),
        "v27_economic": {
            "fork_preference": "v27",
            "ideology_strength": scenario["econ_ideology_strength"],
            "switching_threshold": scenario["econ_switching_threshold"],
            "inertia": scenario["econ_inertia"],
            "activity_type": "transactional",
            "transaction_velocity": scenario["transaction_velocity"],
        },
        "v26_economic": {
            "fork_preference": "v26",
            "ideology_strength": scenario["econ_ideology_strength"],
            "switching_threshold": scenario["econ_switching_threshold"],
            "inertia": scenario["econ_inertia"],
            "activity_type": "transactional",
            "transaction_velocity": scenario["transaction_velocity"],
        },
        "user_config": {
            "fork_preference": "neutral",
            "ideology_strength": scenario["user_ideology_strength"],
            "switching_threshold": scenario["user_switching_threshold"],
            "inertia": 0.05,
            "activity_type": "mixed",
            "transaction_velocity": scenario["transaction_velocity"],
            "is_solo_miner": True,
            "hashrate_pct": scenario["solo_miner_hashrate"],
        },
        "partition_mode": "static",
        "fork_observer_enabled": False,
    }


def generate_network(config_path: Path, output_path: Path, generator_path: Path) -> bool:
    """Run the network generator for a single config"""
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(generator_path),
                "--config", str(config_path),
                "-o", str(output_path)
            ],
            capture_output=True,
            text=True,
            timeout=60
        )
        return result.returncode == 0
    except Exception as e:
        print(f"  Error generating network: {e}")
        return False


def load_base_network(network_path: Path) -> Dict:
    """Load a base network template"""
    with open(network_path) as f:
        return yaml.safe_load(f)


def apply_scenario_to_base_network(base_network: Dict, scenario: Dict) -> Dict:
    """
    Apply scenario parameters to a base network template.

    This modifies node metadata based on scenario parameters while preserving
    the network structure (nodes, connections, etc.).
    """
    network = copy.deepcopy(base_network)
    nodes = network.get('nodes', [])

    # Extract parameters with defaults for backward compatibility
    pool_ideology = scenario.get('pool_ideology_strength', 0.5)
    pool_max_loss = scenario.get('pool_max_loss_pct', 0.25)
    pool_prof_threshold = scenario.get('pool_profitability_threshold', 0.1)

    econ_ideology = scenario.get('econ_ideology_strength', 0.2)
    econ_switching = scenario.get('econ_switching_threshold', 0.1)
    econ_inertia = scenario.get('econ_inertia', 0.15)
    econ_neutral_fraction = scenario.get('econ_neutral_fraction', 0.0)

    user_ideology = scenario.get('user_ideology_strength', 0.5)
    user_switching = scenario.get('user_switching_threshold', 0.1)

    solo_hashrate_mult = scenario.get('solo_miner_hashrate', 0.05)
    transaction_velocity = scenario.get('transaction_velocity', 0.5)

    # Calculate target distributions (split parameters are 0–1 fractions)
    v27_hash_target = scenario.get('hashrate_split', 0.5) * 100
    v27_econ_target = scenario.get('economic_split', 0.5) * 100

    # --- Assign economic node image tags based on economic_split ---
    # Sort economic nodes by custody descending, assign the top ones to v27
    # until cumulative custody reaches v27_econ_target % of total.
    econ_roles = {'major_exchange', 'exchange', 'institutional', 'payment_processor', 'merchant',
                  'economic_aggregate'}
    econ_nodes = [n for n in nodes if n.get('metadata', {}).get('role') in econ_roles]
    total_econ_custody = sum(n['metadata'].get('custody_btc', 0) for n in econ_nodes)

    if total_econ_custody > 0:
        econ_nodes_sorted = sorted(
            econ_nodes,
            key=lambda n: n['metadata'].get('custody_btc', 0),
            reverse=True
        )
        v27_custody_acc = 0
        v27_custody_target = total_econ_custody * (v27_econ_target / 100)
        for econ_node in econ_nodes_sorted:
            custody = econ_node['metadata'].get('custody_btc', 0)
            midpoint = v27_custody_acc + custody / 2
            if midpoint < v27_custody_target:
                econ_node['image'] = {'tag': '27.0'}
            else:
                econ_node['image'] = {'tag': '26.0'}
            v27_custody_acc += custody

    # --- Assign user node image tags based on user_split (optional) ---
    # user_split controls what fraction of user custody weight starts on v27.
    # Mirrors the economic_split assignment above. When user_split is absent from
    # the scenario, user nodes keep their original image tags from network.yaml —
    # preserving backwards compatibility with all prior sweeps.
    user_split = scenario.get('user_split', None)
    if user_split is not None:
        user_roles = {'power_user', 'casual_user', 'power_user_aggregate', 'casual_user_aggregate'}
        user_nodes = [n for n in nodes if n.get('metadata', {}).get('role') in user_roles]
        total_user_custody = sum(n['metadata'].get('custody_btc', 0) for n in user_nodes)
        if total_user_custody > 0:
            user_nodes_sorted = sorted(
                user_nodes,
                key=lambda n: n['metadata'].get('custody_btc', 0),
                reverse=True
            )
            v27_user_target = total_user_custody * float(user_split)
            v27_user_acc = 0.0
            for user_node in user_nodes_sorted:
                custody = user_node['metadata'].get('custody_btc', 0)
                midpoint = v27_user_acc + custody / 2
                if midpoint < v27_user_target:
                    user_node['image'] = {'tag': '27.0'}
                else:
                    user_node['image'] = {'tag': '26.0'}
                v27_user_acc += custody

    # --- Compute neutral economic node assignments ---
    # Neutral nodes (ideology=0, fork_preference=neutral) switch on price alone.
    # Per partition: sort by custody descending, mark the first N as neutral.
    neutral_node_ids = set()
    if econ_neutral_fraction > 0:
        for partition_tag in ['27.0', '26.0']:
            partition_econ = [
                n for n in econ_nodes
                if n.get('image', {}).get('tag') == partition_tag
            ]
            partition_econ_sorted = sorted(
                partition_econ,
                key=lambda n: n['metadata'].get('custody_btc', 0),
                reverse=True
            )
            n_neutral = round(econ_neutral_fraction * len(partition_econ_sorted))
            for n in partition_econ_sorted[:n_neutral]:
                neutral_node_ids.add(n['name'])

    # --- Assign pool image tags based on hashrate_split ---
    # This controls the pool's initial mining fork via node version.
    # Note: pool ideology/preference is controlled separately by the pool
    # config YAML (create_pool_scenario), not by node metadata.
    pool_nodes = [n for n in nodes if n.get('metadata', {}).get('role') == 'mining_pool']
    total_hashrate = sum(n['metadata'].get('hashrate_pct', 0) for n in pool_nodes)
    v27_hashrate_acc = 0

    for node in nodes:
        metadata = node.get('metadata', {})
        role = metadata.get('role', '')

        if role == 'mining_pool':
            hashrate = metadata.get('hashrate_pct', 0)
            midpoint = v27_hashrate_acc + hashrate / 2
            if midpoint < v27_hash_target:
                node['image'] = {'tag': '27.0'}
            else:
                node['image'] = {'tag': '26.0'}
            v27_hashrate_acc += hashrate

            # Apply ideology parameters based on original ideology strength
            original_ideology = metadata.get('ideology_strength', 0.5)
            if original_ideology > 0.7:
                # Committed pools - scale with scenario parameter
                metadata['ideology_strength'] = round(pool_ideology * 1.2, 3)
                metadata['max_loss_pct'] = round(pool_max_loss * 1.5, 3)
            elif original_ideology > 0.4:
                # Moderate pools
                metadata['ideology_strength'] = round(pool_ideology, 3)
                metadata['max_loss_pct'] = round(pool_max_loss, 3)
            else:
                # Rational pools
                metadata['ideology_strength'] = round(pool_ideology * 0.3, 3)
                metadata['max_loss_pct'] = round(pool_max_loss * 0.5, 3)

            metadata['profitability_threshold'] = round(pool_prof_threshold, 3)

        elif role in ['major_exchange', 'exchange', 'economic_aggregate']:
            if node['name'] in neutral_node_ids:
                # Neutral node: purely rational, no fork loyalty, switches on price alone
                metadata['ideology_strength'] = 0.0
                metadata['fork_preference'] = 'neutral'
            else:
                # Partisan node: loyalty matches their fork (v27 tag → v27 preference)
                partition_tag = node.get('image', {}).get('tag', '27.0')
                metadata['fork_preference'] = 'v27' if partition_tag == '27.0' else 'v26'
                metadata['ideology_strength'] = round(econ_ideology, 3)
            metadata['switching_threshold'] = round(econ_switching, 3)
            metadata['inertia'] = round(econ_inertia, 3)
            metadata['max_loss_pct'] = round(econ_ideology * 0.5, 3)
            if 'transaction_velocity' in metadata:
                metadata['transaction_velocity'] = round(transaction_velocity, 3)

        elif role == 'institutional':
            metadata['ideology_strength'] = round(econ_ideology * 1.2, 3)
            metadata['inertia'] = round(econ_inertia * 2, 3)
            metadata['max_loss_pct'] = round(econ_ideology * 0.4, 3)

        elif role == 'payment_processor':
            metadata['ideology_strength'] = round(user_ideology * 0.8, 3)
            metadata['switching_threshold'] = round(user_switching, 3)
            if 'transaction_velocity' in metadata:
                metadata['transaction_velocity'] = round(transaction_velocity * 1.2, 3)

        elif role == 'merchant':
            metadata['ideology_strength'] = round(user_ideology * 0.6, 3)
            metadata['switching_threshold'] = round(user_switching, 3)

        elif role in ['power_user', 'power_user_aggregate']:
            metadata['ideology_strength'] = round(user_ideology, 3)
            metadata['switching_threshold'] = round(user_switching * 1.5, 3)
            # Scale solo mining hashrate
            if metadata.get('hashrate_pct', 0) > 0:
                metadata['hashrate_pct'] = round(metadata['hashrate_pct'] * (solo_hashrate_mult / 0.05), 4)

        elif role in ['casual_user', 'casual_user_aggregate']:
            metadata['ideology_strength'] = round(user_ideology * 0.5, 3)
            metadata['switching_threshold'] = round(user_switching * 0.8, 3)
            if metadata.get('hashrate_pct', 0) > 0:
                metadata['hashrate_pct'] = round(metadata['hashrate_pct'] * (solo_hashrate_mult / 0.05), 4)

        node['metadata'] = metadata

    return network


def generate_from_base_network(
    base_network: Dict,
    scenario: Dict,
    output_dir: Path,
    node_defaults_path: Optional[Path] = None
) -> bool:
    """Generate a network by applying scenario to base network template"""
    try:
        # Apply scenario parameters to base network
        network = apply_scenario_to_base_network(base_network, scenario)

        # Create output directory
        scenario_id = scenario['scenario_id']
        network_dir = output_dir / scenario_id
        network_dir.mkdir(parents=True, exist_ok=True)

        # Write network.yaml
        network_file = network_dir / "network.yaml"
        with open(network_file, "w") as f:
            yaml.dump(network, f, default_flow_style=False, sort_keys=False)

        # Copy node-defaults.yaml if provided
        if node_defaults_path and node_defaults_path.exists():
            shutil.copy(node_defaults_path, network_dir / "node-defaults.yaml")
        else:
            # Create a minimal node-defaults.yaml
            node_defaults = {
                "chain": "regtest",
                "image": {
                    "repository": "bitcoindevproject/bitcoin",
                    "pullPolicy": "IfNotPresent"
                },
                "defaultConfig": "regtest=1\n  server=1\n  txindex=1\n  fallbackfee=0.00001\n  rpcuser=bitcoin\n  rpcpassword=bitcoin\n  rpcallowip=0.0.0.0/0\n  rpcbind=0.0.0.0\n  rpcport=18443\n  zmqpubrawblock=tcp://0.0.0.0:28332\n  zmqpubrawtx=tcp://0.0.0.0:28333\n  debug=rpc",
                "collectLogs": False,
                "metricsExport": False
            }
            with open(network_dir / "node-defaults.yaml", "w") as f:
                yaml.dump(node_defaults, f, default_flow_style=False)

        return True
    except Exception as e:
        print(f"  Error generating network from base: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Build network and scenario configs from parameter scenarios",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Generate networks from scratch (default)
    python 2_build_configs.py --input scenarios.json

    # Use realistic-economy as base template
    python 2_build_configs.py --input scenarios.json \\
        --base-network ../../networks/realistic-economy/network.yaml

    # Use custom network as base
    python 2_build_configs.py --input scenarios.json \\
        --base-network path/to/my-network/network.yaml
        """
    )
    parser.add_argument("--input", "-i", type=str, required=True,
                        help="Input scenarios.json file from step 1")
    parser.add_argument("--output-dir", "-o", type=str, default="./sweep_output",
                        help="Output directory (default: ./sweep_output)")
    parser.add_argument("--configs-only", action="store_true",
                        help="Only generate config files, skip network generation")
    parser.add_argument("--generator", "-g", type=str, default=None,
                        help="Path to scenario_network_generator.py (for from-scratch generation)")
    parser.add_argument("--base-network", "-b", type=str, default=None,
                        help="Path to base network.yaml to use as template (e.g., realistic-economy)")

    args = parser.parse_args()

    # Load scenarios
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}")
        sys.exit(1)

    with open(input_path) as f:
        data = json.load(f)

    scenarios = data["scenarios"]
    metadata = data.get("metadata", {})

    print(f"Loaded {len(scenarios)} scenarios from {input_path}")

    # Setup output directories
    output_dir = Path(args.output_dir)
    networks_dir = output_dir / "networks"
    configs_dir = output_dir / "configs"
    network_configs_dir = configs_dir / "network"
    pools_dir = configs_dir / "pools"
    economic_dir = configs_dir / "economic"

    for d in [output_dir, networks_dir, configs_dir, network_configs_dir, pools_dir, economic_dir]:
        d.mkdir(parents=True, exist_ok=True)

    # Resolve base network: --base-network flag > spec metadata > error
    script_dir = Path(__file__).parent
    spec_network = metadata.get("base_network", "")

    if args.base_network:
        # Explicit flag — resolve alias if needed
        base_network_arg = NETWORK_ALIASES.get(args.base_network, args.base_network)
        if spec_network and spec_network != args.base_network:
            spec_resolved = NETWORK_ALIASES.get(spec_network, spec_network)
            if base_network_arg != spec_resolved:
                print(f"  ⚠️  WARNING: --base-network '{args.base_network}' overrides spec network '{spec_network}'")
    elif spec_network:
        # Use network from spec metadata
        base_network_arg = NETWORK_ALIASES.get(spec_network, spec_network)
        print(f"  Using network from spec: {spec_network}  ({base_network_arg})")
        args.base_network = base_network_arg
    else:
        base_network_arg = None

    if base_network_arg:
        args.base_network = base_network_arg

    # Determine network generation mode
    use_base_network = args.base_network is not None
    base_network = None
    node_defaults_path = None

    # Pools extracted from base network (if using one)
    network_pools = None

    if use_base_network:
        base_network_path = Path(args.base_network)
        if not base_network_path.exists():
            print(f"Error: Base network not found: {base_network_path}")
            sys.exit(1)

        print(f"Using base network template: {base_network_path}")
        base_network = load_base_network(base_network_path)

        # Extract pools from the base network
        network_pools = extract_pools_from_network(base_network)
        if network_pools:
            total_hashrate = sum(p[2] for p in network_pools)
            v27_hashrate = sum(p[2] for p in network_pools if p[4] == 'v27')
            print(f"  Extracted {len(network_pools)} pools from network:")
            print(f"    Total hashrate: {total_hashrate:.1f}%")
            print(f"    V27 initial: {v27_hashrate:.1f}%, V26 initial: {total_hashrate - v27_hashrate:.1f}%")
        else:
            print("  Warning: No pools found in base network, using DEFAULT_POOLS")

        # Look for node-defaults.yaml
        node_defaults_path = base_network_path.parent / "node-defaults.yaml"
        if not node_defaults_path.exists():
            node_defaults_path = base_network_path.parent.parent / "node-defaults.yaml"
        if not node_defaults_path.exists():
            node_defaults_path = None
            print("  Note: No node-defaults.yaml found, will create default")

    else:
        # Find network generator for from-scratch generation
        if args.generator:
            generator_path = Path(args.generator)
        else:
            # Try to find it relative to this script
            script_dir = Path(__file__).parent
            generator_path = script_dir.parent.parent / "networkGen" / "scenario_network_generator.py"

        if not generator_path.exists():
            print(f"Warning: Network generator not found at {generator_path}")
            print("  Network generation will be skipped")
            print("  Tip: Use --base-network to generate from an existing network template")
            args.configs_only = True

    # Generate configs
    print(f"\nGenerating configs...")

    all_pool_scenarios = {}
    all_economic_scenarios = {}
    manifest = {
        "metadata": metadata,
        "scenarios": [],
        "generated_networks": 0,
        "failed_networks": 0,
    }

    for i, scenario in enumerate(scenarios):
        scenario_id = scenario["scenario_id"]

        # Create individual network config
        network_config = create_network_config(scenario)
        network_config_path = network_configs_dir / f"{scenario_id}.yaml"
        with open(network_config_path, "w") as f:
            yaml.dump(network_config, f, default_flow_style=False, sort_keys=False)

        # Create pool scenario (use network pools if available)
        pool_config = create_pool_scenario(scenario, pools=network_pools)
        all_pool_scenarios[scenario_id] = pool_config

        # Create economic scenario
        econ_config = create_economic_scenario(scenario)
        all_economic_scenarios[scenario_id] = econ_config

        # Track in manifest
        manifest["scenarios"].append({
            "scenario_id": scenario_id,
            "parameters": scenario,
            "network_config": str(network_config_path),
            "network_path": str(Path("networks") / scenario_id / "network.yaml"),
        })

        if (i + 1) % 50 == 0:
            print(f"  Generated configs for {i + 1}/{len(scenarios)} scenarios")

    # Save combined pool scenarios
    pools_config_path = pools_dir / "sweep_pools_config.yaml"
    with open(pools_config_path, "w") as f:
        yaml.dump(all_pool_scenarios, f, default_flow_style=False, sort_keys=False)
    print(f"Saved pool configs to {pools_config_path}")

    # Save combined economic scenarios
    econ_config_path = economic_dir / "sweep_economic_config.yaml"
    with open(econ_config_path, "w") as f:
        yaml.dump(all_economic_scenarios, f, default_flow_style=False, sort_keys=False)
    print(f"Saved economic configs to {econ_config_path}")

    # Generate networks
    if not args.configs_only:
        if use_base_network:
            print(f"\nGenerating networks from base template...")
        else:
            print(f"\nGenerating networks using {generator_path}...")

        for i, scenario in enumerate(scenarios):
            scenario_id = scenario["scenario_id"]
            network_output = networks_dir / scenario_id / "network.yaml"

            # Always regenerate (stale skipping caused silent build bugs)

            if use_base_network:
                # Generate from base network template
                success = generate_from_base_network(
                    base_network,
                    scenario,
                    networks_dir,
                    node_defaults_path
                )
            else:
                # Generate from scratch using generator
                config_path = network_configs_dir / f"{scenario_id}.yaml"
                success = generate_network(config_path, network_output, generator_path)

            if success:
                manifest["generated_networks"] += 1
            else:
                manifest["failed_networks"] += 1
                print(f"  Failed: {scenario_id}")

            if (i + 1) % 20 == 0:
                print(f"  Generated {i + 1}/{len(scenarios)} networks")

        print(f"\nNetworks: {manifest['generated_networks']} generated, {manifest['failed_networks']} failed")

    # Save manifest
    manifest_path = output_dir / "build_manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nSaved build manifest to {manifest_path}")

    print(f"\n{'='*60}")
    print("Build complete!")
    print(f"{'='*60}")
    print(f"\nOutput directory: {output_dir}")
    if use_base_network:
        print(f"  - Base network: {args.base_network}")
    print(f"  - Network configs: {network_configs_dir}")
    print(f"  - Pool scenarios:  {pools_config_path}")
    print(f"  - Economic scenarios: {econ_config_path}")
    if not args.configs_only:
        print(f"  - Networks: {networks_dir}")

    print(f"\nNext step: python 3_run_sweep.py --input {manifest_path}")


if __name__ == "__main__":
    main()
