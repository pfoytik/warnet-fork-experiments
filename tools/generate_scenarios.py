#!/usr/bin/env python3
"""
Step 1: Generate LHS Parameter Scenarios

Generates a statistically efficient set of parameter combinations using
Latin Hypercube Sampling to explore the fork threshold parameter space.

Usage:
    # Generate 200 scenarios
    python 1_generate_scenarios.py --samples 200

    # Generate with specific seed for reproducibility
    python 1_generate_scenarios.py --samples 300 --seed 42

    # Preview without saving
    python 1_generate_scenarios.py --samples 50 --preview

Output:
    scenarios.json - Array of parameter combinations to test
"""

import argparse
import json
import os
import numpy as np
from dataclasses import dataclass
from typing import List, Any
from pathlib import Path


@dataclass
class ParameterRange:
    """Defines a parameter's range and type"""
    name: str
    min_val: float
    max_val: float
    param_type: str = "continuous"  # continuous, discrete
    description: str = ""

    def sample(self, normalized_value: float) -> Any:
        """Convert normalized [0,1] value to actual parameter value"""
        if self.param_type == "continuous":
            return self.min_val + normalized_value * (self.max_val - self.min_val)
        elif self.param_type == "discrete":
            val = int(self.min_val + normalized_value * (self.max_val - self.min_val + 0.999))
            return min(val, int(self.max_val))
        return normalized_value


# Define the parameter space
PARAMETER_SPACE = [
    # Initial conditions (symmetric: 0=all v26, 0.5=equal, 1=all v27)
    ParameterRange("economic_split", 0.0, 1.0, "continuous",
                   "Fraction of economic custody starting on v27 (0=all v26, 0.5=equal, 1=all v27)"),
    ParameterRange("hashrate_split", 0.0, 1.0, "continuous",
                   "Fraction of initial hashrate starting on v27 (0=all v26, 0.5=equal, 1=all v27)"),

    # Pool behavior
    ParameterRange("pool_ideology_strength", 0.1, 0.9, "continuous",
                   "How much pools sacrifice for ideology (0=rational, 1=ideological)"),
    ParameterRange("pool_profitability_threshold", 0.02, 0.30, "continuous",
                   "Min profit advantage for pools to consider switching"),
    ParameterRange("pool_max_loss_pct", 0.02, 0.50, "continuous",
                   "Max revenue loss pools tolerate for ideology"),
    ParameterRange("pool_committed_split", 0.0, 1.0, "continuous",
                   "Fraction of committed pool hashrate preferring v27 (0=all v26, 0.5=equal, 1=all v27)"),
    ParameterRange("pool_neutral_pct", 10, 50, "continuous",
                   "Percentage of pools that are neutral/rational"),

    # Economic node behavior
    ParameterRange("econ_ideology_strength", 0.0, 0.8, "continuous",
                   "How much economic nodes sacrifice for ideology"),
    ParameterRange("econ_switching_threshold", 0.02, 0.25, "continuous",
                   "Min price advantage for economic nodes to switch"),
    ParameterRange("econ_inertia", 0.05, 0.30, "continuous",
                   "Additional switching friction (infrastructure costs)"),

    # User behavior
    ParameterRange("user_ideology_strength", 0.1, 0.9, "continuous",
                   "How much users sacrifice for ideology"),
    ParameterRange("user_switching_threshold", 0.05, 0.20, "continuous",
                   "Min price advantage for users to switch"),

    # Fee market
    ParameterRange("transaction_velocity", 0.1, 0.9, "continuous",
                   "Rate of fee-generating transactions (0=custodial, 1=high volume)"),

    # Network structure
    ParameterRange("user_nodes_per_partition", 2, 10, "discrete",
                   "Number of user nodes per partition"),
    ParameterRange("economic_nodes_per_partition", 1, 3, "discrete",
                   "Number of economic/exchange nodes per partition"),
    ParameterRange("solo_miner_hashrate", 0.02, 0.15, "continuous",
                   "Hashrate percentage per solo miner"),
]


def latin_hypercube_sample(n_samples: int, n_dimensions: int, seed: int = None) -> np.ndarray:
    """
    Generate Latin Hypercube Samples.

    LHS ensures that each parameter's range is evenly covered by dividing
    it into n_samples intervals and sampling exactly once from each interval.

    Returns array of shape (n_samples, n_dimensions) with values in [0, 1]
    """
    rng = np.random.RandomState(seed)
    samples = np.zeros((n_samples, n_dimensions))

    for dim in range(n_dimensions):
        # Create evenly spaced intervals
        intervals = np.linspace(0, 1, n_samples + 1)

        # Sample uniformly within each interval
        points = np.array([
            rng.uniform(intervals[i], intervals[i + 1])
            for i in range(n_samples)
        ])

        # Randomly permute to break correlation between dimensions
        rng.shuffle(points)
        samples[:, dim] = points

    return samples


def generate_scenarios(n_samples: int, seed: int = None) -> List[dict]:
    """Generate LHS scenarios across the parameter space"""
    n_dims = len(PARAMETER_SPACE)
    lhs_samples = latin_hypercube_sample(n_samples, n_dims, seed)

    scenarios = []
    for i, sample in enumerate(lhs_samples):
        scenario = {"scenario_id": f"sweep_{i:04d}"}

        for j, param in enumerate(PARAMETER_SPACE):
            value = param.sample(sample[j])
            # Round for readability
            if isinstance(value, float):
                value = round(value, 3)
            scenario[param.name] = value

        scenarios.append(scenario)

    return scenarios


def get_parameter_metadata() -> List[dict]:
    """Get metadata about all parameters for documentation"""
    return [
        {
            "name": p.name,
            "min": p.min_val,
            "max": p.max_val,
            "type": p.param_type,
            "description": p.description
        }
        for p in PARAMETER_SPACE
    ]


def print_coverage_stats(scenarios: List[dict]):
    """Print statistics about parameter coverage"""
    import pandas as pd

    df = pd.DataFrame(scenarios)
    print("\n=== Parameter Coverage Statistics ===\n")

    # Drop scenario_id for stats
    stats_df = df.drop(columns=["scenario_id"])

    print(stats_df.describe().round(3).to_string())
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Generate LHS parameter scenarios for fork threshold testing",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--samples", "-n", type=int, default=200,
                        help="Number of scenarios to generate (default: 200)")
    parser.add_argument("--seed", "-s", type=int, default=42,
                        help="Random seed for reproducibility (default: 42)")
    parser.add_argument("--output", "-o", type=str, default="scenarios.json",
                        help="Output file path (default: scenarios.json)")
    parser.add_argument("--preview", action="store_true",
                        help="Preview scenarios without saving")

    args = parser.parse_args()

    print(f"Generating {args.samples} LHS scenarios (seed={args.seed})...")
    print(f"Parameter dimensions: {len(PARAMETER_SPACE)}")

    scenarios = generate_scenarios(args.samples, args.seed)

    # Try to print coverage stats if pandas available
    try:
        print_coverage_stats(scenarios)
    except ImportError:
        print("\n(Install pandas for coverage statistics)")

    if args.preview:
        print("\n=== Sample Scenarios ===\n")
        for s in scenarios[:3]:
            print(json.dumps(s, indent=2))
        print(f"\n... and {len(scenarios) - 3} more")
        return

    # Save scenarios
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    output_data = {
        "metadata": {
            "n_samples": args.samples,
            "seed": args.seed,
            "n_parameters": len(PARAMETER_SPACE),
            "parameters": get_parameter_metadata()
        },
        "scenarios": scenarios
    }

    with open(output_path, "w") as f:
        json.dump(output_data, f, indent=2)

    print(f"\nSaved {len(scenarios)} scenarios to: {output_path}")
    print(f"\nNext step: python 2_build_configs.py --input {output_path}")


if __name__ == "__main__":
    main()
