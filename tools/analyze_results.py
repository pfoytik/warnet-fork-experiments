#!/usr/bin/env python3
"""
Step 4: Analyze Parameter Sweep Results

Loads all scenario results and identifies critical thresholds for fork outcomes.
Generates summary statistics, visualizations, and threshold analysis.

Usage:
    # Analyze results
    python 4_analyze_results.py --input sweep_output/results

    # Generate full report with visualizations
    python 4_analyze_results.py --input sweep_output/results --visualize

    # Export to specific format
    python 4_analyze_results.py --input sweep_output/results --export csv

Output:
    results/
    ├── analysis/
    │   ├── summary.json           # Overall statistics
    │   ├── thresholds.json        # Critical threshold analysis
    │   ├── correlations.json      # Parameter correlations
    │   ├── sweep_data.csv         # Full dataset
    │   └── figures/               # Visualizations (if --visualize)
    └── ...
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Any, Tuple, Optional
from collections import defaultdict


def load_time_series(scenario_dir: Path) -> List[Dict]:
    """Load time_series.csv for a scenario, return list of row dicts"""
    ts_path = scenario_dir / "time_series.csv"
    if not ts_path.exists():
        return []
    with open(ts_path) as f:
        return list(csv.DictReader(f))


def analyze_econ_trajectory(ts_rows: List[Dict]) -> Dict:
    """Extract econ switching metrics from time series rows"""
    if not ts_rows:
        return {}

    def fval(row, key, default=0.0):
        try:
            return float(row[key])
        except (KeyError, TypeError, ValueError):
            return default

    econ_initial = fval(ts_rows[0], "v27_economic")
    econ_final   = fval(ts_rows[-1], "v27_economic")
    econ_delta   = econ_final - econ_initial
    econ_switched = econ_delta > 5.0  # more than 5pp movement

    # Time of first meaningful econ departure (>2pp from initial)
    econ_switch_time_s: Optional[float] = None
    for row in ts_rows:
        if abs(fval(row, "v27_economic") - econ_initial) > 2.0:
            econ_switch_time_s = fval(row, "timestamps")
            break

    # Time econ first exceeded 95%
    econ_95pct_time_s: Optional[float] = None
    for row in ts_rows:
        if fval(row, "v27_economic") > 95.0:
            econ_95pct_time_s = fval(row, "timestamps")
            break

    # Peak price gap (v27 - v26) / v26 as percentage
    peak_price_gap_pct = 0.0
    for row in ts_rows:
        v27p = fval(row, "v27_price")
        v26p = fval(row, "v26_price")
        if v26p > 0:
            gap = (v27p - v26p) / v26p * 100
            if gap > peak_price_gap_pct:
                peak_price_gap_pct = gap

    # Pool cascade complete: first t where v27_hashrate > 65%
    cascade_time_s: Optional[float] = None
    for row in ts_rows:
        if fval(row, "v27_hashrate") > 65.0:
            cascade_time_s = fval(row, "timestamps")
            break

    # Lag: how long after cascade before econ moved
    econ_lag_s: Optional[float] = None
    if cascade_time_s is not None and econ_switch_time_s is not None:
        econ_lag_s = econ_switch_time_s - cascade_time_s

    # Econ outcome category
    if econ_final > 95.0:
        econ_outcome = "full_switch"
    elif econ_final > 60.0:
        econ_outcome = "partial_switch"
    else:
        econ_outcome = "no_switch"

    return {
        "econ_initial":        round(econ_initial, 2),
        "econ_final":          round(econ_final, 2),
        "econ_delta":          round(econ_delta, 2),
        "econ_switched":       econ_switched,
        "econ_outcome":        econ_outcome,
        "econ_switch_time_s":  econ_switch_time_s,
        "econ_95pct_time_s":   econ_95pct_time_s,
        "peak_price_gap_pct":  round(peak_price_gap_pct, 2),
        "cascade_time_s":      cascade_time_s,
        "econ_lag_s":          econ_lag_s,
    }


def load_scenario_results(results_dir: Path) -> List[Dict]:
    """Load all scenario results into a list"""
    results = []

    for scenario_dir in sorted(results_dir.iterdir()):
        if not scenario_dir.is_dir():
            continue
        if scenario_dir.name == "analysis":
            continue

        results_file = scenario_dir / "results.json"
        if not results_file.exists():
            continue

        try:
            with open(results_file) as f:
                data = json.load(f)

            # Extract key metrics
            row = {"scenario_id": scenario_dir.name}

            # Metadata/parameters
            metadata = data.get("metadata", {})
            row["duration"] = metadata.get("duration_seconds", 0)

            # Summary metrics
            summary = data.get("summary", {})
            row["v27_blocks"] = summary.get("blocks_mined", {}).get("v27", 0)
            row["v26_blocks"] = summary.get("blocks_mined", {}).get("v26", 0)
            row["total_blocks"] = summary.get("total_blocks", 0)

            row["final_v27_hashrate"] = summary.get("final_hashrate", {}).get("v27", 50)
            row["final_v26_hashrate"] = summary.get("final_hashrate", {}).get("v26", 50)
            row["final_v27_economic"] = summary.get("final_economic", {}).get("v27", 50)
            row["final_v26_economic"] = summary.get("final_economic", {}).get("v26", 50)

            # Price metrics
            row["final_v27_price"] = summary.get("final_prices", {}).get("v27", 0)
            row["final_v26_price"] = summary.get("final_prices", {}).get("v26", 0)

            # Reorg metrics
            reorg = data.get("reorg", {}).get("network_summary", {})
            row["total_reorgs"] = reorg.get("total_reorg_events", 0)
            row["total_orphans"] = reorg.get("total_blocks_orphaned", 0)
            row["reorg_mass"] = reorg.get("total_reorg_mass", 0)

            # Difficulty metrics
            difficulty = data.get("difficulty", {})
            row["winning_fork"] = difficulty.get("winning_fork", "unknown")

            # Calculate derived metrics
            total_hash = row["final_v27_hashrate"] + row["final_v26_hashrate"]
            row["v27_hash_share"] = row["final_v27_hashrate"] / total_hash if total_hash > 0 else 0.5

            total_econ = row["final_v27_economic"] + row["final_v26_economic"]
            row["v27_econ_share"] = row["final_v27_economic"] / total_econ if total_econ > 0 else 0.5

            total_blocks = row["v27_blocks"] + row["v26_blocks"]
            row["v27_block_share"] = row["v27_blocks"] / total_blocks if total_blocks > 0 else 0.5

            # Determine outcome category
            if row["v27_hash_share"] > 0.65:
                row["outcome"] = "v27_dominant"
            elif row["v27_hash_share"] < 0.35:
                row["outcome"] = "v26_dominant"
            else:
                row["outcome"] = "contested"

            # Fork valuation: sum custody_btc per fork × final price
            v27_custody = 0
            v26_custody = 0
            economic_nodes = data.get("economic", {}).get("nodes", {})
            for node_data in economic_nodes.values():
                profile = node_data.get("profile", {})
                allocation = node_data.get("current_allocation", "")
                custody = profile.get("custody_btc", 0)
                if allocation == "v27":
                    v27_custody += custody
                elif allocation == "v26":
                    v26_custody += custody
            row["v27_fork_valuation"] = v27_custody * row["final_v27_price"]
            row["v26_fork_valuation"] = v26_custody * row["final_v26_price"]

            # Total pool opportunity cost per fork
            v27_pool_cost = 0
            v26_pool_cost = 0
            pool_nodes = data.get("pools", {}).get("pools", {})
            for pool_data in pool_nodes.values():
                costs = pool_data.get("costs", {})
                allocation = pool_data.get("current_allocation", "")
                opp_cost = costs.get("cumulative_opportunity_cost_usd", 0) or 0
                if allocation == "v27":
                    v27_pool_cost += opp_cost
                elif allocation == "v26":
                    v26_pool_cost += opp_cost
            row["v27_pool_opportunity_cost"] = v27_pool_cost
            row["v26_pool_opportunity_cost"] = v26_pool_cost

            # Econ trajectory from time series
            ts_rows = load_time_series(scenario_dir)
            traj = analyze_econ_trajectory(ts_rows)
            row.update(traj)

            results.append(row)

        except Exception as e:
            print(f"  Warning: Failed to load {scenario_dir.name}: {e}")

    return results


def load_parameters(manifest_path: Path) -> Dict[str, Dict]:
    """Load scenario parameters from manifest"""
    if not manifest_path.exists():
        return {}

    with open(manifest_path) as f:
        manifest = json.load(f)

    return {s["scenario_id"]: s["parameters"] for s in manifest.get("scenarios", [])}


def merge_results_with_params(results: List[Dict], params: Dict[str, Dict]) -> List[Dict]:
    """Merge results with input parameters"""
    merged = []
    for r in results:
        scenario_id = r["scenario_id"]
        if scenario_id in params:
            merged_row = {**params[scenario_id], **r}
        else:
            merged_row = r
        merged.append(merged_row)
    return merged


def calculate_outcome_statistics(results: List[Dict]) -> Dict:
    """Calculate statistics by outcome category"""
    outcomes = defaultdict(list)

    for r in results:
        outcomes[r["outcome"]].append(r)

    stats = {}
    for outcome, rows in outcomes.items():
        stats[outcome] = {
            "count": len(rows),
            "percentage": round(len(rows) / len(results) * 100, 1) if results else 0
        }

    return stats


def find_critical_thresholds(results: List[Dict], params: Dict[str, Dict]) -> Dict:
    """Identify parameter thresholds that predict outcomes"""
    merged = merge_results_with_params(results, params)

    if not merged:
        return {}

    # Parameters to analyze
    param_names = [
        "economic_split", "hashrate_split",
        "pool_ideology_strength", "pool_profitability_threshold", "pool_max_loss_pct",
        "pool_committed_split", "pool_neutral_pct",
        "econ_ideology_strength", "econ_switching_threshold",
        "user_ideology_strength", "transaction_velocity"
    ]

    thresholds = {}

    for param in param_names:
        # Get values for each outcome
        by_outcome = defaultdict(list)
        for row in merged:
            if param in row:
                by_outcome[row["outcome"]].append(row[param])

        if not by_outcome:
            continue

        # Calculate statistics for each outcome
        param_stats = {}
        for outcome, values in by_outcome.items():
            if values:
                param_stats[outcome] = {
                    "mean": round(sum(values) / len(values), 3),
                    "min": round(min(values), 3),
                    "max": round(max(values), 3),
                    "count": len(values)
                }

        # Identify separation thresholds
        if "v27_dominant" in param_stats and "v26_dominant" in param_stats:
            v27_mean = param_stats["v27_dominant"]["mean"]
            v26_mean = param_stats["v26_dominant"]["mean"]
            threshold_estimate = (v27_mean + v26_mean) / 2

            param_stats["threshold_estimate"] = round(threshold_estimate, 3)
            param_stats["separation"] = round(abs(v27_mean - v26_mean), 3)
            param_stats["v27_favored_direction"] = "higher" if v27_mean > v26_mean else "lower"

        thresholds[param] = param_stats

    # Sort by separation (most discriminating parameters first)
    sorted_thresholds = dict(sorted(
        thresholds.items(),
        key=lambda x: x[1].get("separation", 0),
        reverse=True
    ))

    return sorted_thresholds


def calculate_correlations(results: List[Dict], params: Dict[str, Dict]) -> Dict:
    """Calculate correlations between parameters and outcomes"""
    merged = merge_results_with_params(results, params)

    if not merged:
        return {}

    # Numeric parameters and outcomes
    param_names = [
        "economic_split", "hashrate_split",
        "pool_ideology_strength", "pool_profitability_threshold", "pool_max_loss_pct",
        "econ_ideology_strength", "user_ideology_strength", "transaction_velocity"
    ]

    outcome_metrics = [
        "v27_hash_share", "v27_econ_share", "v27_block_share", "total_reorgs",
        "econ_final", "econ_delta", "econ_switch_time_s", "peak_price_gap_pct", "cascade_time_s",
    ]

    correlations = {}

    for param in param_names:
        param_corrs = {}
        param_values = [r.get(param) for r in merged if param in r]

        if not param_values:
            continue

        for metric in outcome_metrics:
            metric_values = [r.get(metric) for r in merged if param in r and metric in r]
            # Drop rows where either value is None
            pairs = [(p, m) for p, m in zip(param_values, metric_values) if p is not None and m is not None]
            if not pairs:
                continue
            param_values_m, metric_values = zip(*pairs)
            param_values_m = list(param_values_m)
            metric_values = list(metric_values)

            if len(param_values_m) != len(metric_values) or len(param_values_m) < 3:
                continue

            # Simple correlation calculation (Pearson)
            n = len(param_values_m)
            mean_p = sum(param_values_m) / n
            mean_m = sum(metric_values) / n

            cov = sum((p - mean_p) * (m - mean_m) for p, m in zip(param_values_m, metric_values)) / n
            std_p = (sum((p - mean_p) ** 2 for p in param_values_m) / n) ** 0.5
            std_m = (sum((m - mean_m) ** 2 for m in metric_values) / n) ** 0.5

            if std_p > 0 and std_m > 0:
                corr = cov / (std_p * std_m)
                param_corrs[metric] = round(corr, 3)

        if param_corrs:
            correlations[param] = param_corrs

    return correlations


def generate_summary_report(results: List[Dict], thresholds: Dict, correlations: Dict) -> str:
    """Generate human-readable summary report"""
    lines = [
        "=" * 70,
        "PARAMETER SWEEP ANALYSIS REPORT",
        "=" * 70,
        "",
        f"Total scenarios analyzed: {len(results)}",
        "",
    ]

    # Outcome distribution
    outcome_stats = calculate_outcome_statistics(results)
    lines.append("OUTCOME DISTRIBUTION:")
    for outcome, stats in sorted(outcome_stats.items()):
        lines.append(f"  {outcome}: {stats['count']} ({stats['percentage']}%)")
    lines.append("")

    # Top discriminating parameters
    lines.append("MOST DISCRIMINATING PARAMETERS:")
    lines.append("(Parameters with largest difference between v27/v26 dominant outcomes)")
    lines.append("")

    for i, (param, stats) in enumerate(list(thresholds.items())[:5]):
        sep = stats.get("separation", 0)
        direction = stats.get("v27_favored_direction", "?")
        threshold = stats.get("threshold_estimate", "?")

        lines.append(f"  {i+1}. {param}")
        lines.append(f"     Separation: {sep}")
        lines.append(f"     v27 favored when: {direction}")
        lines.append(f"     Estimated threshold: {threshold}")

        if "v27_dominant" in stats:
            lines.append(f"     v27_dominant mean: {stats['v27_dominant']['mean']}")
        if "v26_dominant" in stats:
            lines.append(f"     v26_dominant mean: {stats['v26_dominant']['mean']}")
        lines.append("")

    # Econ trajectory table
    lines.append("ECON SWITCHING TRAJECTORIES:")
    lines.append("(Economic node allocation shift over run duration)")
    lines.append("")
    traj_cols = ["scenario_id", "econ_outcome", "econ_initial", "econ_final",
                 "econ_switch_time_s", "cascade_time_s", "econ_lag_s", "peak_price_gap_pct"]
    header = f"  {'scenario':<12} {'econ_outcome':<14} {'initial%':>8} {'final%':>7} {'switch_t':>9} {'cascade_t':>10} {'lag_s':>7} {'peak_gap%':>10}"
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for r in results:
        if "econ_outcome" not in r:
            continue
        sw_t  = f"{r['econ_switch_time_s']:.0f}" if r.get("econ_switch_time_s") is not None else "never"
        cas_t = f"{r['cascade_time_s']:.0f}"     if r.get("cascade_time_s") is not None else "never"
        lag   = f"{r['econ_lag_s']:.0f}"         if r.get("econ_lag_s") is not None else "n/a"
        lines.append(
            f"  {r['scenario_id']:<12} {r['econ_outcome']:<14} "
            f"{r['econ_initial']:>7.1f}% {r['econ_final']:>6.1f}% "
            f"{sw_t:>9} {cas_t:>10} {lag:>7} {r['peak_price_gap_pct']:>9.1f}%"
        )
    lines.append("")

    # Econ outcome distribution
    econ_dist = defaultdict(int)
    for r in results:
        econ_dist[r.get("econ_outcome", "unknown")] += 1
    lines.append("ECON OUTCOME DISTRIBUTION:")
    for outcome, count in sorted(econ_dist.items()):
        lines.append(f"  {outcome}: {count} ({count/len(results)*100:.0f}%)")
    lines.append("")

    # Strong correlations
    lines.append("STRONG CORRELATIONS:")
    lines.append("(Parameters most correlated with v27 hashrate share / econ final)")
    lines.append("")

    hash_corrs = [(p, c.get("v27_hash_share", 0)) for p, c in correlations.items()]
    hash_corrs.sort(key=lambda x: abs(x[1]), reverse=True)

    for param, corr in hash_corrs[:5]:
        direction = "+" if corr > 0 else "-"
        econ_corr = correlations.get(param, {}).get("v27_econ_share", 0)
        econ_dir  = "+" if econ_corr > 0 else "-"
        lines.append(f"  {param}: hash={direction}{abs(corr):.3f}  econ={econ_dir}{abs(econ_corr):.3f}")
    lines.append("")

    # Key insights
    lines.append("KEY INSIGHTS:")
    lines.append("")

    # Find strongest predictors
    if thresholds:
        top_param = list(thresholds.keys())[0]
        top_stats = thresholds[top_param]
        if "threshold_estimate" in top_stats:
            lines.append(f"  * {top_param} is the strongest predictor of fork outcome")
            lines.append(f"    Threshold around {top_stats['threshold_estimate']}")
            lines.append(f"    v27 dominant when {top_stats.get('v27_favored_direction', '?')}")

    lines.append("")
    lines.append("=" * 70)

    return "\n".join(lines)


def export_to_csv(results: List[Dict], params: Dict[str, Dict], output_path: Path):
    """Export merged results to CSV"""
    merged = merge_results_with_params(results, params)

    if not merged:
        print("No data to export")
        return

    # Get all columns
    all_cols = set()
    for row in merged:
        all_cols.update(row.keys())

    # Order columns logically
    priority_cols = [
        "scenario_id", "outcome", "econ_outcome",
        "economic_split", "hashrate_split",
        "pool_ideology_strength", "pool_profitability_threshold", "pool_max_loss_pct",
        "pool_committed_split", "pool_neutral_pct",
        "econ_ideology_strength", "econ_neutral_fraction", "user_ideology_strength",
        "v27_hash_share", "v27_econ_share", "v27_block_share",
        "econ_initial", "econ_final", "econ_delta", "econ_switched", "econ_outcome",
        "econ_switch_time_s", "econ_95pct_time_s", "cascade_time_s", "econ_lag_s",
        "peak_price_gap_pct",
        "total_reorgs", "total_orphans"
    ]

    cols = [c for c in priority_cols if c in all_cols]
    cols += sorted([c for c in all_cols if c not in cols])

    with open(output_path, "w") as f:
        # Header
        f.write(",".join(cols) + "\n")

        # Rows
        for row in merged:
            values = [str(row.get(c, "")) for c in cols]
            f.write(",".join(values) + "\n")

    print(f"Exported {len(merged)} rows to {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Analyze parameter sweep results",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--input", "-i", type=str, required=True,
                        help="Results directory from step 3")
    parser.add_argument("--manifest", "-m", type=str, default=None,
                        help="Build manifest file (for parameter info)")
    parser.add_argument("--export", type=str, choices=["csv", "json", "both"], default="both",
                        help="Export format (default: both)")
    parser.add_argument("--visualize", action="store_true",
                        help="Generate visualizations (requires matplotlib)")

    args = parser.parse_args()

    results_dir = Path(args.input)
    if not results_dir.exists():
        print(f"Error: Results directory not found: {results_dir}")
        sys.exit(1)

    # Find manifest
    if args.manifest:
        manifest_path = Path(args.manifest)
    else:
        manifest_path = results_dir.parent / "build_manifest.json"

    print(f"Loading results from {results_dir}...")
    results = load_scenario_results(results_dir)
    print(f"Loaded {len(results)} scenario results")

    if not results:
        print("No results to analyze!")
        sys.exit(1)

    # Load parameters
    params = load_parameters(manifest_path)
    print(f"Loaded parameters for {len(params)} scenarios")

    # Create analysis directory
    analysis_dir = results_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)

    # Calculate statistics
    print("\nCalculating statistics...")
    outcome_stats = calculate_outcome_statistics(results)
    thresholds = find_critical_thresholds(results, params)
    correlations = calculate_correlations(results, params)

    # Generate report
    report = generate_summary_report(results, thresholds, correlations)
    print("\n" + report)

    # Save report
    report_path = analysis_dir / "report.txt"
    with open(report_path, "w") as f:
        f.write(report)
    print(f"\nSaved report to {report_path}")

    # Export data
    if args.export in ["json", "both"]:
        summary_path = analysis_dir / "summary.json"
        with open(summary_path, "w") as f:
            json.dump({
                "outcome_statistics": outcome_stats,
                "scenario_count": len(results),
            }, f, indent=2)

        thresholds_path = analysis_dir / "thresholds.json"
        with open(thresholds_path, "w") as f:
            json.dump(thresholds, f, indent=2)

        correlations_path = analysis_dir / "correlations.json"
        with open(correlations_path, "w") as f:
            json.dump(correlations, f, indent=2)

        print(f"Saved JSON analysis to {analysis_dir}")

    if args.export in ["csv", "both"]:
        csv_path = analysis_dir / "sweep_data.csv"
        export_to_csv(results, params, csv_path)

    # Visualizations
    if args.visualize:
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt

            figures_dir = analysis_dir / "figures"
            figures_dir.mkdir(exist_ok=True)

            merged = merge_results_with_params(results, params)

            # Outcome distribution pie chart
            fig, ax = plt.subplots(figsize=(8, 6))
            labels = list(outcome_stats.keys())
            sizes = [outcome_stats[k]["count"] for k in labels]
            colors = {"v27_dominant": "#2ecc71", "contested": "#f1c40f", "v26_dominant": "#e74c3c"}
            ax.pie(sizes, labels=labels, autopct='%1.1f%%',
                   colors=[colors.get(l, "#95a5a6") for l in labels])
            ax.set_title("Fork Outcome Distribution")
            plt.savefig(figures_dir / "outcome_distribution.png", dpi=150, bbox_inches='tight')
            plt.close()

            # Top parameter scatter plots
            if thresholds:
                top_params = list(thresholds.keys())[:4]

                fig, axes = plt.subplots(2, 2, figsize=(12, 10))
                axes = axes.flatten()

                for ax, param in zip(axes, top_params):
                    x = [r.get(param) for r in merged if param in r]
                    y = [r.get("v27_hash_share") for r in merged if param in r]
                    c = [r.get("outcome") for r in merged if param in r]

                    color_map = {"v27_dominant": "#2ecc71", "contested": "#f1c40f", "v26_dominant": "#e74c3c"}
                    colors = [color_map.get(o, "#95a5a6") for o in c]

                    ax.scatter(x, y, c=colors, alpha=0.6, s=30)
                    ax.set_xlabel(param)
                    ax.set_ylabel("v27 Hashrate Share")
                    ax.axhline(y=0.5, color='gray', linestyle='--', alpha=0.5)

                plt.tight_layout()
                plt.savefig(figures_dir / "parameter_effects.png", dpi=150, bbox_inches='tight')
                plt.close()

            print(f"Saved visualizations to {figures_dir}")

        except ImportError:
            print("Warning: matplotlib not available, skipping visualizations")

    print(f"\n{'='*60}")
    print("Analysis complete!")
    print(f"{'='*60}")
    print(f"Results saved to: {analysis_dir}")


if __name__ == "__main__":
    main()
