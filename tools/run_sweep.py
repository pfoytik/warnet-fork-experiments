#!/usr/bin/env python3
"""
Step 3: Run Parameter Sweep

Executes each scenario from the build manifest and saves results.
Supports resuming interrupted sweeps by skipping completed scenarios.

Usage:
    # Run all scenarios
    python 3_run_sweep.py --input sweep_output/build_manifest.json

    # Run with custom duration
    python 3_run_sweep.py --input sweep_output/build_manifest.json --duration 3600

    # Dry run (show what would be executed)
    python 3_run_sweep.py --input sweep_output/build_manifest.json --dry-run

    # Run specific scenarios
    python 3_run_sweep.py --input sweep_output/build_manifest.json --scenarios sweep_0000 sweep_0001

Output:
    results/
    ├── sweep_0000/
    │   ├── results.json
    │   ├── scenario.log
    │   └── ...
    ├── sweep_0001/
    │   └── ...
    └── sweep_progress.json
"""

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
import yaml
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def get_completed_scenarios(results_dir: Path) -> set:
    """Get set of scenario IDs that have completed results"""
    completed = set()

    if not results_dir.exists():
        return completed

    for scenario_dir in results_dir.iterdir():
        if scenario_dir.is_dir():
            results_file = scenario_dir / "results.json"
            if results_file.exists():
                completed.add(scenario_dir.name)

    return completed


def force_cleanup_cluster(max_attempts: int = 3, namespace: str = "default") -> bool:
    """
    Aggressively clean up the cluster to ensure no pods are left running.
    This is more thorough than stop_warnet for end-of-sweep cleanup.
    """
    print("  Force cleaning cluster...")
    ns_args = ["-n", namespace]

    for attempt in range(max_attempts):
        try:
            # Tear down namespace resources. warnet down --force has no namespace
            # support, so use kubectl directly for non-default namespaces.
            if namespace == "default":
                subprocess.run(
                    ["warnet", "down", "--force"],
                    capture_output=True, text=True, timeout=120
                )
            else:
                subprocess.run(
                    ["kubectl", "delete", "all", "--all"] + ns_args + ["--force", "--grace-period=0"],
                    capture_output=True, text=True, timeout=120
                )
            time.sleep(5)

            # Check if any pods are still running
            result = subprocess.run(
                ["kubectl", "get", "pods", "--no-headers"] + ns_args,
                capture_output=True, text=True, timeout=30
            )

            if result.returncode == 0:
                pods = [l.strip() for l in result.stdout.strip().split('\n') if l.strip()]
                if not pods:
                    print("  Cluster is clean")
                    return True

                print(f"  {len(pods)} pods still running, attempt {attempt + 1}")

                subprocess.run(
                    ["kubectl", "delete", "pods", "--all"] + ns_args + ["--force", "--grace-period=0"],
                    capture_output=True, text=True, timeout=60
                )
                time.sleep(10)

        except subprocess.TimeoutExpired:
            print(f"  Cleanup command timed out, attempt {attempt + 1}")
        except Exception as e:
            print(f"  Cleanup error: {e}")

        time.sleep(5)

    # Final check
    try:
        result = subprocess.run(
            ["kubectl", "get", "pods", "--no-headers"] + ns_args,
            capture_output=True, text=True, timeout=15
        )
        pods = [l.strip() for l in result.stdout.strip().split('\n') if l.strip()]
        if pods:
            print(f"  Warning: {len(pods)} pods still running after cleanup attempts")
            return False
    except Exception:
        pass

    return True


def restart_minikube(wait_after: int = 60) -> bool:
    """
    Restart minikube to recover from resource exhaustion.
    This is a heavy operation but reliably recovers the cluster.
    """
    print("\n  === RESTARTING MINIKUBE TO RECOVER CLUSTER ===")

    try:
        # Stop minikube
        print("  Stopping minikube...")
        subprocess.run(
            ["minikube", "stop"],
            capture_output=True,
            text=True,
            timeout=300
        )
        time.sleep(10)

        # Start minikube
        print("  Starting minikube...")
        result = subprocess.run(
            ["minikube", "start"],
            capture_output=True,
            text=True,
            timeout=600
        )

        if result.returncode != 0:
            print(f"  Warning: minikube start returned {result.returncode}")
            print(f"  {result.stderr[:300] if result.stderr else ''}")

        # Wait for cluster to be ready
        print(f"  Waiting {wait_after}s for cluster to stabilize...")
        time.sleep(wait_after)

        # Verify cluster is responsive
        if wait_for_cluster_ready(120):
            print("  Minikube restarted successfully")
            return True
        else:
            print("  Warning: Cluster may not be fully ready after restart")
            return False

    except subprocess.TimeoutExpired:
        print("  Error: Minikube operation timed out")
        return False
    except Exception as e:
        print(f"  Error restarting minikube: {e}")
        return False


def inject_sweep_config(
    scenario_id: str,
    pools_config: Path,
    economic_config: Path,
    scenarios_dir: Path
) -> bool:
    """
    Inject sweep scenario configs into the main config files.

    This copies the sweep scenario into the bundled config files so they
    get packaged with the scenario archive and are available in the pod.
    """
    try:
        # Load sweep configs
        with open(pools_config, 'r') as f:
            sweep_pools = yaml.safe_load(f)

        with open(economic_config, 'r') as f:
            sweep_econ = yaml.safe_load(f)

        # Get the specific scenario
        if scenario_id not in sweep_pools:
            print(f"  Warning: {scenario_id} not found in pool config")
            return False

        if scenario_id not in sweep_econ:
            print(f"  Warning: {scenario_id} not found in economic config")
            return False

        # Load existing main configs
        main_pools_path = scenarios_dir / "config" / "mining_pools_config.yaml"
        main_econ_path = scenarios_dir / "config" / "economic_nodes_config.yaml"
        lock_path = scenarios_dir / "config" / ".inject.lock"

        # Acquire exclusive lock to prevent concurrent corruption from parallel runners
        with open(lock_path, 'w') as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                with open(main_pools_path, 'r') as f:
                    main_pools = yaml.safe_load(f)

                with open(main_econ_path, 'r') as f:
                    main_econ = yaml.safe_load(f)

                # Inject sweep scenario
                main_pools[scenario_id] = sweep_pools[scenario_id]
                main_econ[scenario_id] = sweep_econ[scenario_id]

                # Write back
                with open(main_pools_path, 'w') as f:
                    yaml.dump(main_pools, f, default_flow_style=False, sort_keys=False)

                with open(main_econ_path, 'w') as f:
                    yaml.dump(main_econ, f, default_flow_style=False, sort_keys=False)
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

        return True

    except Exception as e:
        print(f"  Error injecting config: {e}")
        return False


def inject_network_metadata(network_path: Path, scenarios_dir: Path) -> bool:
    """
    Copy the sweep-specific network YAML to the bundled network_metadata.yaml.

    The scenario runs inside a pod where the scenarios/config/ directory is
    bundled. By overwriting network_metadata.yaml before each run, we ensure
    the scenario uses the correct per-scenario node structure (with image tags
    set from economic_split) rather than the static default.
    """
    try:
        dest = scenarios_dir / "config" / "network_metadata.yaml"
        shutil.copy2(str(network_path), str(dest))
        return True
    except Exception as e:
        print(f"  Error injecting network metadata: {e}")
        return False


def wait_for_cluster_ready(timeout: int = 60, namespace: str = "default") -> bool:
    """Wait for the cluster to be responsive with exponential backoff"""
    start = time.time()
    wait_time = 2  # Start with 2 second wait
    attempts = 0

    while time.time() - start < timeout:
        try:
            result = subprocess.run(
                ["kubectl", "get", "pods", "--no-headers", "-n", namespace],
                capture_output=True,
                text=True,
                timeout=15
            )
            if result.returncode == 0:
                if attempts > 0:
                    print(f"  Cluster ready after {attempts} attempts")
                return True
        except subprocess.TimeoutExpired:
            attempts += 1
            print(f"  Cluster check timed out, attempt {attempts}...")
        except Exception as e:
            attempts += 1
            print(f"  Cluster check failed ({e}), attempt {attempts}...")

        time.sleep(min(wait_time, 15))  # Cap at 15 second wait
        wait_time *= 1.5  # Exponential backoff

    return False


def stop_warnet(dry_run: bool = False, cooldown: int = 30, namespace: str = "default") -> bool:
    """Stop any running warnet network with retry logic"""
    if dry_run:
        print(f"  [DRY RUN] Would stop warnet (namespace={namespace})")
        return True

    # warnet down --force has no namespace support. For non-default namespaces
    # use kubectl to tear down all resources in the namespace instead.
    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            if namespace == "default":
                cmd = ["warnet", "down", "--force"]
            else:
                cmd = ["kubectl", "delete", "all", "--all", "-n", namespace, "--force", "--grace-period=0"]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if result.returncode == 0:
                break
            else:
                print(f"  Warning: stop command returned {result.returncode}")
                if result.stderr:
                    print(f"    {result.stderr[:200]}")
        except subprocess.TimeoutExpired:
            print(f"  Warning: stop command timed out (attempt {attempt + 1})")
            if attempt < max_attempts - 1:
                time.sleep(10)
                continue
        except Exception as e:
            print(f"  Warning: Error stopping warnet: {e}")

    # Give it time to fully shut down regardless of success
    print(f"  Cooldown {cooldown}s...")
    time.sleep(cooldown)

    # Wait for cluster to be responsive
    if not wait_for_cluster_ready(60, namespace=namespace):
        print(f"  Warning: Cluster may not be fully ready after shutdown")
        time.sleep(15)

    return True


def get_commander_pod_name(namespace: str = "default") -> Optional[str]:
    """Get the commander pod name from warnet status or kubectl"""
    try:
        result = subprocess.run(
            ["kubectl", "get", "pods", "-o", "name", "-n", namespace],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if 'commander' in line.lower():
                    # Extract pod name from "pod/commander-xxx"
                    return line.replace('pod/', '').strip()
    except Exception:
        pass
    return None


def get_pod_status(pod_name: str, namespace: str = "default") -> Optional[str]:
    """Get the status of a pod (Running, Succeeded, Failed, etc.)"""
    try:
        result = subprocess.run(
            ["kubectl", "get", "pod", pod_name, "-o", "jsonpath={.status.phase}", "-n", namespace],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def wait_for_scenario_completion(duration: int, poll_interval: int = 10, dry_run: bool = False, namespace: str = "default") -> Tuple[bool, Optional[str]]:
    """
    Wait for the scenario to complete by monitoring pod status and logs.

    The scenario exports results with RESULTS_EXPORT_START/END markers when done.
    We poll the pod status and logs until we see completion or timeout.

    Returns:
        Tuple of (completed, logs_content)
    """
    if dry_run:
        print(f"  [DRY RUN] Would wait up to {duration + 120}s for scenario completion")
        return True, None

    timeout = duration + 3600  # Allow extra time: pod init (~10min) + slow simulation (~10%) + result export
    start_time = time.time()

    print(f"  Waiting for scenario completion (up to {timeout}s)...")

    commander_pod = None
    last_logs = ""
    pod_completed = False

    while time.time() - start_time < timeout:
        try:
            # Find commander pod if we don't have it
            if not commander_pod:
                commander_pod = get_commander_pod_name(namespace=namespace)
                if commander_pod:
                    print(f"    Found commander pod: {commander_pod}")

            # Check pod status first - this is more reliable than log parsing
            if commander_pod:
                pod_status = get_pod_status(commander_pod, namespace=namespace)
                if pod_status in ["Succeeded", "Failed"]:
                    elapsed = int(time.time() - start_time)
                    print(f"    Pod status: {pod_status} after {elapsed}s")
                    pod_completed = True
                    # Pod finished, fetch final logs
                    try:
                        result = subprocess.run(
                            ["kubectl", "logs", commander_pod, "--all-containers", "-n", namespace],
                            capture_output=True,
                            text=True,
                            timeout=60
                        )
                        if result.returncode == 0 and result.stdout:
                            last_logs = result.stdout
                    except Exception as e:
                        print(f"    Warning: Could not fetch final logs: {e}")

                    return pod_status == "Succeeded", last_logs

            # Also check logs for completion markers (backup method)
            if commander_pod and not pod_completed:
                try:
                    result = subprocess.run(
                        ["kubectl", "logs", commander_pod, "--all-containers", "-n", namespace],
                        capture_output=True,
                        text=True,
                        timeout=60
                    )

                    if result.returncode == 0 and result.stdout:
                        last_logs = result.stdout

                        # Check for results export markers (indicates scenario completed)
                        if "RESULTS_EXPORT_END" in last_logs:
                            elapsed = int(time.time() - start_time)
                            print(f"  Scenario completed (found export marker) after {elapsed}s")
                            return True, last_logs

                        # Check for test passed indicator
                        if "Tests successful" in last_logs or "Passed" in last_logs:
                            elapsed = int(time.time() - start_time)
                            print(f"  Scenario completed (test passed) after {elapsed}s")
                            return True, last_logs
                except subprocess.TimeoutExpired:
                    pass  # Log fetch timed out, try again
                except Exception:
                    pass  # Ignore log fetch errors, we'll keep trying

        except Exception as e:
            print(f"  Warning: Error checking status: {e}")

        # Progress indicator
        elapsed = int(time.time() - start_time)
        if elapsed % 90 == 0 and elapsed > 0:
            status_str = f", pod: {get_pod_status(commander_pod, namespace=namespace)}" if commander_pod else ""
            print(f"    Still waiting... ({elapsed}s elapsed{status_str})")

        time.sleep(poll_interval)

    print(f"  Warning: Scenario did not complete within {timeout}s")
    # Return whatever logs we have
    return False, last_logs


def deploy_network(network_path: Path, startup_wait: int = 45, dry_run: bool = False, namespace: str = "default") -> bool:
    """Deploy a warnet network and wait for nodes to start"""
    if dry_run:
        print(f"  [DRY RUN] Would deploy: {network_path.parent} (namespace={namespace})")
        return True

    try:
        result = subprocess.run(
            ["warnet", "deploy", str(network_path.parent), "--namespace", namespace],
            capture_output=True,
            text=True,
            timeout=420  # 7 min for deployment (can be slow with many nodes)
        )

        if result.returncode != 0:
            print(f"  Error deploying network: {result.stderr[:300] if result.stderr else 'unknown error'}")
            return False

        # Wait for nodes to start up and be ready
        print(f"  Waiting {startup_wait}s for nodes to start...")
        time.sleep(startup_wait)

        # Verify nodes are actually running
        try:
            check = subprocess.run(
                ["kubectl", "get", "pods", "--no-headers", "-n", namespace],
                capture_output=True,
                text=True,
                timeout=15
            )
            if check.returncode == 0:
                pod_count = len([l for l in check.stdout.strip().split('\n') if l.strip()])
                print(f"  Network deployed with {pod_count} pods")
        except Exception:
            pass

        return True

    except subprocess.TimeoutExpired:
        print(f"  Error: Network deployment timed out")
        return False
    except Exception as e:
        print(f"  Error deploying network: {e}")
        return False


def run_scenario(
    scenario_id: str,
    network_path: Path,
    pools_config: Path,
    economic_config: Path,
    results_dir: Path,
    scenarios_dir: Path,
    duration: int,
    interval: int,
    startup_wait: int,
    cooldown: int,
    extract_script: Path,
    dry_run: bool = False,
    random_seed: int = None,
    retarget_interval: int = 2016,
    enable_liveness_penalty: bool = False,
    use_economic_ema: bool = False,
    economic_ema_alpha: float = 0.15,
    use_sigmoid: bool = False,
    sigmoid_steepness: float = 6.0,
    use_cost_floor: bool = False,
    cost_floor_margin_buffer: float = 0.05,
    max_price_divergence: float = None,
    namespace: str = "default",
) -> bool:
    """Run a single scenario and extract results"""

    scenario_results_dir = results_dir / scenario_id
    scenario_results_dir.mkdir(parents=True, exist_ok=True)

    log_file = scenario_results_dir / "scenario.log"

    # Step 0a: Pre-flight check - ensure cluster is responsive
    if not dry_run:
        if not wait_for_cluster_ready(60, namespace=namespace):
            print(f"  Error: Cluster not responsive, skipping scenario")
            return False

    # Step 0b: Inject sweep config into main config files
    print(f"  Injecting sweep config...")
    if not dry_run:
        if not inject_sweep_config(scenario_id, pools_config, economic_config, scenarios_dir):
            print(f"  Warning: Could not inject config, scenario may fail")

    # Step 0c: Inject per-scenario network metadata so economic_split image tags
    # are bundled into the pod (overrides the static network_metadata.yaml default)
    print(f"  Injecting network metadata...")
    if not dry_run:
        if not inject_network_metadata(network_path, scenarios_dir):
            print(f"  Warning: Could not inject network metadata, economic_split may be wrong")

    # Step 1: Stop any running network
    print(f"  Stopping previous network...")
    stop_warnet(dry_run, cooldown, namespace=namespace)

    # Step 2: Deploy the new network
    print(f"  Deploying network...")
    if not deploy_network(network_path, startup_wait, dry_run, namespace=namespace):
        return False

    # Step 3: Run the scenario
    # Note: We don't pass --pool-config/--economic-config because the scenario
    # runs inside a pod where local paths don't exist. Instead, we inject the
    # sweep config into the main config files which get bundled with the scenario.
    cmd = [
        "warnet", "run",
        str(scenarios_dir / "partition_miner_with_pools.py"),
        "--namespace", namespace,
        f"--pool-scenario={scenario_id}",
        f"--economic-scenario={scenario_id}",
        "--enable-difficulty",
        "--enable-reorg-metrics",
        f"--retarget-interval={retarget_interval}",
        f"--duration={duration}",
        f"--interval={interval}",
        f"--results-id={scenario_id}",
        "--snapshot-interval=60",
    ]

    # Add random seed if provided (for baseline reproducibility testing)
    if random_seed is not None:
        cmd.append(f"--random-seed={random_seed}")

    if enable_liveness_penalty:
        cmd.append("--enable-liveness-penalty")
    if use_economic_ema:
        cmd.append("--use-economic-ema")
        cmd.append(f"--economic-ema-alpha={economic_ema_alpha}")
    if use_sigmoid:
        cmd.append("--use-sigmoid")
        cmd.append(f"--sigmoid-steepness={sigmoid_steepness}")
    if use_cost_floor:
        cmd.append("--use-cost-floor")
        cmd.append(f"--cost-floor-margin-buffer={cost_floor_margin_buffer}")
    if max_price_divergence is not None:
        cmd.append(f"--max-price-divergence={max_price_divergence}")

    if dry_run:
        print(f"  [DRY RUN] Would execute:")
        print(f"    {' '.join(cmd)}")
        wait_for_scenario_completion(duration, dry_run=True, namespace=namespace)
        return True

    # Variable to store logs from completion monitoring
    scenario_logs = None

    print(f"  Deploying scenario commander...")
    try:
        with open(log_file, "w") as f:
            f.write(f"# Scenario: {scenario_id}\n")
            f.write(f"# Started: {datetime.now().isoformat()}\n")
            f.write(f"# Command: {' '.join(cmd)}\n\n")

            # warnet run returns after deploying the commander pod
            # It does NOT wait for the scenario to complete
            result = subprocess.run(
                cmd,
                stdout=f,
                stderr=subprocess.STDOUT,
                timeout=1200  # Allow extra time when cluster is under load from parallel deployments
            )

        if result.returncode != 0:
            print(f"  Warning: Scenario deployment returned non-zero exit code: {result.returncode}")

    except subprocess.TimeoutExpired:
        print(f"  Error: Scenario deployment timed out")
        return False
    except Exception as e:
        print(f"  Error deploying scenario: {e}")
        return False

    # Step 3b: Wait for the scenario to actually complete
    completed, scenario_logs = wait_for_scenario_completion(duration, namespace=namespace)
    if not completed:
        print(f"  Warning: Scenario may not have completed properly")
        # Continue anyway to try to extract whatever results exist

    # Step 4: Extract results from logs BEFORE stopping network
    print(f"  Extracting results...")
    try:
        # Use logs from completion monitoring if available, otherwise fetch fresh
        if scenario_logs:
            logs_content = scenario_logs
        else:
            commander_pod = get_commander_pod_name(namespace=namespace)
            logs_content = None

            if commander_pod:
                # Try kubectl directly - more reliable than warnet logs
                try:
                    logs_result = subprocess.run(
                        ["kubectl", "logs", commander_pod, "--all-containers", "-n", namespace],
                        capture_output=True,
                        text=True,
                        timeout=120
                    )
                    if logs_result.returncode == 0 and logs_result.stdout:
                        logs_content = logs_result.stdout
                except subprocess.TimeoutExpired:
                    print(f"  Warning: kubectl logs timed out")
                except Exception as e:
                    print(f"  Warning: kubectl logs failed: {e}")

            if not logs_content:
                print(f"  Warning: Could not get scenario logs")

        if logs_content:
            # Save raw logs
            raw_log_file = scenario_results_dir / "warnet_logs.txt"
            with open(raw_log_file, "w") as f:
                f.write(logs_content)

            # Extract results
            extract_result = subprocess.run(
                [sys.executable, str(extract_script), "--output-dir", str(results_dir)],
                input=logs_content,
                capture_output=True,
                text=True,
                timeout=120
            )

            if extract_result.returncode != 0:
                print(f"  Warning: Results extraction failed: {extract_result.stderr}")
            else:
                print(f"  Results extracted successfully")

    except Exception as e:
        print(f"  Warning: Error extracting results: {e}")

    return True


def save_progress(progress_file: Path, progress: Dict):
    """Save progress to file"""
    with open(progress_file, "w") as f:
        json.dump(progress, f, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="Run parameter sweep scenarios",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--input", "-i", type=str, required=True,
                        help="Input build_manifest.json from step 2")
    parser.add_argument("--duration", "-d", type=int, default=1800,
                        help="Scenario duration in seconds (default: 1800 = 30min)")
    parser.add_argument("--interval", type=int, default=10,
                        help="Block interval in seconds (default: 10)")
    parser.add_argument("--startup-wait", type=int, default=45,
                        help="Seconds to wait for nodes after deploy (default: 45)")
    parser.add_argument("--cooldown", type=int, default=30,
                        help="Seconds to wait after stopping network (default: 30)")
    parser.add_argument("--results-dir", "-r", type=str, default=None,
                        help="Results directory (default: <input_dir>/results)")
    parser.add_argument("--scenarios", nargs="+", type=str, default=None,
                        help="Run only specific scenario IDs")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be executed without running")
    parser.add_argument("--max-scenarios", type=int, default=None,
                        help="Maximum number of scenarios to run")
    parser.add_argument("--max-consecutive-failures", type=int, default=3,
                        help="Restart minikube after N consecutive failures (default: 3)")
    parser.add_argument("--no-auto-restart", action="store_true",
                        help="Disable automatic minikube restart on failures")
    parser.add_argument("--retarget-interval", type=int, default=144,
                        help="Difficulty retarget interval in blocks (default: 144)")
    parser.add_argument("--enable-liveness-penalty", action="store_true", default=False,
                        help="Pass --enable-liveness-penalty to each scenario run (Option B price oracle)")
    parser.add_argument("--use-economic-ema", action="store_true", default=False,
                        help="Proposal 1: EMA lag on economic weight (Kristoufek 2015)")
    parser.add_argument("--economic-ema-alpha", type=float, default=0.15,
                        help="EMA smoothing factor (default: 0.15)")
    parser.add_argument("--use-sigmoid", action="store_true", default=False,
                        help="Proposal 2: Sigmoid factor mapping on economic weight (Biais et al. 2019)")
    parser.add_argument("--sigmoid-steepness", type=float, default=6.0,
                        help="Sigmoid steepness k (default: 6.0)")
    parser.add_argument("--use-cost-floor", action="store_true", default=False,
                        help="Proposal 3: Asymmetric cost-of-production price floor (Hayes 2019)")
    parser.add_argument("--cost-floor-margin-buffer", type=float, default=0.05,
                        help="Cost floor margin buffer below breakeven (default: 0.05)")
    parser.add_argument("--max-price-divergence", type=float, default=None,
                        help="Max price divergence cap (e.g., 0.20 for ±20%%). If not set, uses scenario default.")
    parser.add_argument("--namespace", type=str, default="default",
                        help="Kubernetes namespace to deploy into (default: default). "
                             "Allows multiple sweeps to run in parallel on different namespaces.")

    args = parser.parse_args()

    # Load manifest
    manifest_path = Path(args.input)
    if not manifest_path.exists():
        print(f"Error: Manifest file not found: {manifest_path}")
        sys.exit(1)

    with open(manifest_path) as f:
        manifest = json.load(f)

    scenarios = manifest["scenarios"]
    base_dir = manifest_path.parent

    # Setup paths
    results_dir = Path(args.results_dir) if args.results_dir else base_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    # Find other required paths
    configs_dir = base_dir / "configs"
    pools_config = configs_dir / "pools" / "sweep_pools_config.yaml"
    economic_config = configs_dir / "economic" / "sweep_economic_config.yaml"

    # Find scenarios directory
    script_dir = Path(__file__).parent
    scenarios_dir = script_dir.parent.parent / "scenarios"

    # Find extract script
    extract_script = script_dir.parent / "extract_results.py"

    # Validate paths
    if not pools_config.exists():
        print(f"Error: Pool config not found: {pools_config}")
        sys.exit(1)
    if not economic_config.exists():
        print(f"Error: Economic config not found: {economic_config}")
        sys.exit(1)
    if not scenarios_dir.exists():
        print(f"Error: Scenarios directory not found: {scenarios_dir}")
        sys.exit(1)

    # Filter scenarios
    if args.scenarios:
        scenarios = [s for s in scenarios if s["scenario_id"] in args.scenarios]
        print(f"Filtered to {len(scenarios)} specified scenarios")

    # Check what's already completed
    completed = get_completed_scenarios(results_dir)
    pending = [s for s in scenarios if s["scenario_id"] not in completed]

    if args.max_scenarios:
        pending = pending[:args.max_scenarios]

    # Calculate more accurate time estimate including startup/shutdown overhead
    overhead_per_scenario = args.startup_wait + 10  # startup wait + shutdown time
    time_per_scenario = args.duration + overhead_per_scenario

    print(f"\n{'='*60}")
    print(f"Parameter Sweep Runner")
    print(f"{'='*60}")
    print(f"Total scenarios: {len(scenarios)}")
    print(f"Already completed: {len(completed)}")
    print(f"Pending: {len(pending)}")
    print(f"Duration per scenario: {args.duration}s ({args.duration/60:.0f} min)")
    print(f"Startup wait: {args.startup_wait}s")
    print(f"Estimated total time: {len(pending) * time_per_scenario / 3600:.1f} hours")
    print(f"Results directory: {results_dir}")
    print(f"Namespace: {args.namespace}")
    if not args.no_auto_restart:
        print(f"Auto-restart: after {args.max_consecutive_failures} consecutive failures")
    else:
        print(f"Auto-restart: disabled")

    if args.dry_run:
        print("\n[DRY RUN MODE - No scenarios will be executed]")

    if not pending:
        print("\nNo pending scenarios to run!")
        return

    # Progress tracking
    progress_file = results_dir / "sweep_progress.json"
    progress = {
        "started": datetime.now().isoformat(),
        "total": len(scenarios),
        "completed": len(completed),
        "failed": 0,
        "current": None,
        "history": []
    }

    print(f"\nStarting sweep at {progress['started']}...")
    print("-" * 60)

    # Initial cleanup - ensure no stale networks are running
    if not args.dry_run:
        print("Initial cleanup - stopping any running networks...")
        stop_warnet(dry_run=False, cooldown=args.cooldown, namespace=args.namespace)

    start_time = time.time()
    consecutive_failures = 0

    try:
        for i, scenario in enumerate(pending):
            scenario_id = scenario["scenario_id"]
            # Resolve network_path relative to the manifest file so the sweep
            # can be invoked from any working directory.
            network_path = (manifest_path.parent / scenario["network_path"]).resolve()

            # Check network exists
            if not network_path.exists():
                print(f"[{i+1}/{len(pending)}] SKIP {scenario_id} - network not found: {network_path}")
                progress["failed"] += 1
                continue

            print(f"[{i+1}/{len(pending)}] Running {scenario_id}...")
            progress["current"] = scenario_id
            save_progress(progress_file, progress)

            scenario_start = time.time()

            # Extract random_seed from scenario parameters if present (for baseline tests)
            scenario_params = scenario.get("parameters", scenario)
            random_seed = scenario_params.get("random_seed", None)

            success = run_scenario(
                scenario_id=scenario_id,
                network_path=network_path,
                pools_config=pools_config,
                economic_config=economic_config,
                results_dir=results_dir,
                scenarios_dir=scenarios_dir,
                duration=args.duration,
                interval=args.interval,
                startup_wait=args.startup_wait,
                cooldown=args.cooldown,
                extract_script=extract_script,
                dry_run=args.dry_run,
                random_seed=random_seed,
                retarget_interval=args.retarget_interval,
                enable_liveness_penalty=args.enable_liveness_penalty,
                use_economic_ema=args.use_economic_ema,
                economic_ema_alpha=args.economic_ema_alpha,
                use_sigmoid=args.use_sigmoid,
                sigmoid_steepness=args.sigmoid_steepness,
                use_cost_floor=args.use_cost_floor,
                cost_floor_margin_buffer=args.cost_floor_margin_buffer,
                max_price_divergence=args.max_price_divergence,
                namespace=args.namespace,
            )

            scenario_elapsed = time.time() - scenario_start

            if success:
                progress["completed"] += 1
                status = "OK"
                consecutive_failures = 0  # Reset on success
            else:
                progress["failed"] += 1
                status = "FAILED"
                consecutive_failures += 1

            progress["history"].append({
                "scenario_id": scenario_id,
                "status": status,
                "elapsed": round(scenario_elapsed, 1),
                "timestamp": datetime.now().isoformat()
            })

            save_progress(progress_file, progress)

            # Progress update
            elapsed = time.time() - start_time
            remaining = len(pending) - (i + 1)
            avg_time = elapsed / (i + 1)
            eta = timedelta(seconds=int(remaining * avg_time))

            print(f"  {status} ({scenario_elapsed:.0f}s) - ETA: {eta}")

            # Check for cluster exhaustion and restart if needed
            if (not args.dry_run and
                not args.no_auto_restart and
                consecutive_failures >= args.max_consecutive_failures and
                remaining > 0):

                print(f"\n  {consecutive_failures} consecutive failures detected - cluster likely exhausted")
                if restart_minikube(wait_after=90):
                    consecutive_failures = 0  # Reset after successful restart
                    print("  Cluster recovered, continuing sweep...")
                else:
                    print("  Warning: Minikube restart failed, continuing anyway...")

    finally:
        # Final cleanup - always run to ensure cluster is clean
        if not args.dry_run:
            print(f"\n" + "="*60)
            print("Final cleanup - ensuring cluster is clean...")
            print("="*60)
            force_cleanup_cluster(max_attempts=3, namespace=args.namespace)

    # Final summary
    total_elapsed = time.time() - start_time
    progress["finished"] = datetime.now().isoformat()
    progress["total_elapsed"] = round(total_elapsed, 1)
    progress["current"] = None
    save_progress(progress_file, progress)

    print(f"\n{'='*60}")
    print("Sweep Complete!")
    print(f"{'='*60}")
    print(f"Completed: {progress['completed']}")
    print(f"Failed: {progress['failed']}")
    print(f"Total time: {timedelta(seconds=int(total_elapsed))}")
    print(f"Results saved to: {results_dir}")
    print(f"\nNext step: python 4_analyze_results.py --input {results_dir}")


if __name__ == "__main__":
    main()
