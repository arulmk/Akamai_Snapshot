# Akamai Snapshot

A **read-only** command-line tool that exports a snapshot of your Akamai Property Manager (PAPI) properties and their activation status to CSV and JSON.

It only makes `GET` requests. It never activates, edits or deletes anything, and its output files contain no credentials.

## What it exports

Each run writes a timestamped folder containing:

| File | Contents |
|---|---|
| `properties.csv` | One row per property: group and contract, latest / staging / production versions, live version and last activation per network, pending and failed flags, optional hostnames and rule validation errors |
| `activations.csv` | Full activation history for every property |
| `snapshot.json` | Everything above in a single file |

It also prints a terminal summary and a "needs attention" table.

### Flags

| Flag | Meaning |
|---|---|
| `PENDING_ACTIVATION` | An activation is still in progress (attention) |
| `LAST_ACTIVATION_FAILED` | The most recent activation failed or was aborted (attention) |
| `VALIDATION_ERRORS` | The latest version has rule-tree errors that would block activation (attention, needs `--validate-rules`) |
| `FETCH_ERROR` | A call for this property failed; the rest of the snapshot continued (attention) |
| `NOT_ON_STAGING` / `NOT_ON_PRODUCTION` | Version was never activated on that network |
| `STAGING_BEHIND_LATEST` | Latest version is newer than the one on staging |
| `PROD_DIFFERS_FROM_STAGING` | Production and staging run different versions |

## Requirements

- Python 3.8+
- An Akamai API client (`.edgerc`) with **Property Manager (PAPI) read** access

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Quick test on 5 properties
python akamai_snapshot.py --limit 5

# Full snapshot with rule validation (catches errors that would block activation)
python akamai_snapshot.py --validate-rules

# Also list hostnames
python akamai_snapshot.py --hostnames

# One group (ID or part of its name) / name filter
python akamai_snapshot.py --group grp_123456
python akamai_snapshot.py --name-filter basic

# Fastest: skip per-property activation history
python akamai_snapshot.py --skip-activations
```

### Options

| Option | Description |
|---|---|
| `--edgerc PATH` | Path to `.edgerc` (default `~/.edgerc`) |
| `--section NAME` | `.edgerc` section to use (default `lab`) |
| `--out DIR` | Output folder (default `akamai_snapshot_<timestamp>`) |
| `--group TEXT` | Only this group (group ID, or part of the group name) |
| `--name-filter TEXT` | Only properties whose name contains this text |
| `--limit N` | Only process the first N properties |
| `--skip-activations` | Skip activation history (fastest) |
| `--hostnames` | Also fetch hostnames (one extra call per property) |
| `--validate-rules` | Check each property's latest version for validation errors (one extra call per property) |

Calls that hit rate limits (429) or server errors (5xx) are retried automatically with backoff.

## Security

- Credentials are read from your local `.edgerc` and are never written to the output.
- The `.gitignore` excludes `.edgerc` and all snapshot output. The output holds property and hostname names, so check it before sharing.

## Credits

Built with the help of [Claude](https://claude.ai) (Anthropic).
