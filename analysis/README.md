# Analysis

See the [Re-Analyzing Previous Runs](../README.md#re-analyzing-previous-runs) section in the main README.

```bash
kernel-ci-cloud-runner aws analyze \
  --bucket kernel-ci-myname-results \
  --run-prefix run_test-001_20260325_120000 \
  --region us-west-2
```

Requires: `pip install -e ".[analysis]"`

## Output Structure

```
analysis/data/{run_prefix}/
├── downloads/                      # Downloaded individual CSVs
├── plots/                          # Generated comparison plots
│   ├── regression_overall.png
│   ├── regression_x86_64.png
│   └── regression_aarch64.png
├── combined_results.csv            # All benchmark data combined
└── regression_results.csv          # Regression analysis
```
