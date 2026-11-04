#!/usr/bin/env python3
"""
akamai_snapshot.py - READ-ONLY snapshot of Akamai PAPI properties and activation status.

Makes GET requests only (plus nothing that changes your account). It never activates,
edits or deletes anything, and the output files contain no credentials.

What it exports (into a timestamped folder):
  properties.csv   one row per property: group/contract, latest/staging/production
                   versions, live version + last activation per network, pending /
                   failed flags, optional hostnames and rule-tree validation errors
  activations.csv  full activation history for every property
  snapshot.json    everything above in one file

Usage:
  python akamai_snapshot.py                          # fast: properties + activation status
  python akamai_snapshot.py --limit 5                # try it on 5 properties first
  python akamai_snapshot.py --hostnames              # also list hostnames
  python akamai_snapshot.py --validate-rules         # also check latest version for validation errors
  python akamai_snapshot.py --group grp_277191       # one group (ID or part of its name)
  python akamai_snapshot.py --name-filter basic      # only properties whose name contains "basic"
  python akamai_snapshot.py --skip-activations       # fastest: list-level data only

Requires: pip install edgegrid-python requests tabulate
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime

import requests
from akamai.edgegrid import EdgeGridAuth, EdgeRc
from tabulate import tabulate

REQUEST_TIMEOUT = 60
MAX_RETRIES = 3
RETRY_STATUS = {429, 500, 502, 503, 504}
PENDING_STATES = {"PENDING", "NEW", "ZONE_1", "ZONE_2", "ZONE_3", "PENDING_DEACTIVATION"}
FAILED_STATES = {"FAILED", "ABORTED"}
RULES_ACCEPT = "application/vnd.akamai.papirules.latest+json"
ATTENTION_FLAGS = ("PENDING_ACTIVATION", "LAST_ACTIVATION_FAILED", "VALIDATION_ERRORS")

log = logging.getLogger("akamai_snapshot")


class PapiError(Exception):
    """Any failed PAPI call, with a readable message."""


# --------------------------------------------------------------------------- #
# API client (GET only)
# --------------------------------------------------------------------------- #
class PapiClient:
    def __init__(self, edgerc_path, section):
        edgerc_path = os.path.expanduser(edgerc_path)
        if not os.path.isfile(edgerc_path):
            raise PapiError(f"edgerc file not found: {edgerc_path}")
        try:
            edgerc = EdgeRc(edgerc_path)
            host = edgerc.get(section, "host")
            auth = EdgeGridAuth.from_edgerc(edgerc, section)
        except Exception as exc:
            raise PapiError(f"Could not read section '{section}' from {edgerc_path}: {exc}")

        host = host.strip().replace("https://", "").replace("http://", "").rstrip("/")
        self.base_url = f"https://{host}"
        self.session = requests.Session()
        self.session.auth = auth

    @staticmethod
    def _describe_error(resp):
        try:
            body = resp.json()
        except ValueError:
            return resp.text[:300] or "(empty response body)"
        parts = [body.get("title") or "", body.get("detail") or ""]
        for item in body.get("errors", []) or []:
            msg = item.get("detail") or item.get("title") or ""
            if msg:
                parts.append(f"error: {msg}")
        text = " | ".join(p for p in parts if p)
        if resp.status_code == 403:
            text += " [Hint: the API client needs PAPI read access to this group/contract]"
        return text or "(empty response body)"

    def get(self, path, params=None, accept="application/json"):
        url = f"{self.base_url}{path}"
        resp = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.get(url, params=params, headers={"Accept": accept},
                                        timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES:
                    raise PapiError(f"Network error calling {path}: {exc}")
                time.sleep(2 ** attempt)
                continue
            if resp.status_code in RETRY_STATUS and attempt < MAX_RETRIES:
                try:
                    wait = int(resp.headers.get("Retry-After", ""))
                except ValueError:
                    wait = 2 ** attempt
                time.sleep(min(wait, 30))
                continue
            break

        if resp.status_code >= 400:
            raise PapiError(f"HTTP {resp.status_code} on {path}: {self._describe_error(resp)}")
        try:
            return resp.json()
        except ValueError:
            return {}


# --------------------------------------------------------------------------- #
# Data collection
# --------------------------------------------------------------------------- #
def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def list_properties(client, group_filter=None):
    """Walk every group/contract pair and return unique properties."""
    groups = client.get("/papi/v1/groups").get("groups", {}).get("items", [])
    needle = group_filter.lower() if group_filter else None
    seen = {}
    for g in groups:
        gid = g.get("groupId", "")
        gname = g.get("groupName") or ""
        if needle and needle != gid.lower() and needle not in gname.lower():
            continue
        for cid in g.get("contractIds") or []:
            try:
                body = client.get("/papi/v1/properties", params={"contractId": cid, "groupId": gid})
            except PapiError as exc:
                log.warning("Skipping group %s / contract %s: %s", gid, cid, exc)
                continue
            for p in body.get("properties", {}).get("items", []):
                pid = p.get("propertyId")
                if pid and pid not in seen:
                    p.setdefault("contractId", cid)
                    p.setdefault("groupId", gid)
                    p["groupName"] = gname
                    seen[pid] = p
    return list(seen.values())


def summarize_activations(items):
    """Per-network live version, last activation and pending/failed state."""
    out = {}
    for net in ("STAGING", "PRODUCTION"):
        key = net.lower()
        net_items = sorted((a for a in items if a.get("network") == net),
                           key=lambda a: a.get("submitDate") or "")
        live = [a for a in net_items if a.get("status") == "ACTIVE"
                and a.get("activationType") == "ACTIVATE"]
        last = net_items[-1] if net_items else None
        out[f"{key}_live_version"] = live[-1].get("propertyVersion", "") if live else ""
        out[f"{key}_live_since"] = live[-1].get("updateDate", "") if live else ""
        out[f"{key}_last_activation"] = (
            f"v{last.get('propertyVersion')} {last.get('activationType')} {last.get('status')}"
            if last else ""
        )
        out[f"{key}_last_submit"] = last.get("submitDate", "") if last else ""
        out[f"{key}_pending"] = any(a.get("status") in PENDING_STATES for a in net_items)
        out[f"{key}_last_failed"] = bool(last and last.get("status") in FAILED_STATES)
    return out


def fetch_hostnames(client, prop, version):
    body = client.get(
        f"/papi/v1/properties/{prop['propertyId']}/versions/{version}/hostnames",
        params={"contractId": prop["contractId"], "groupId": prop["groupId"]},
    )
    items = body.get("hostnames", {}).get("items", [])
    return sorted({h.get("cnameFrom", "") for h in items if h.get("cnameFrom")})


def fetch_rule_errors(client, prop, version):
    body = client.get(
        f"/papi/v1/properties/{prop['propertyId']}/versions/{version}/rules",
        params={"contractId": prop["contractId"], "groupId": prop["groupId"],
                "validateRules": "true"},
        accept=RULES_ACCEPT,
    )
    errors = []
    for e in body.get("errors", []) or []:
        msg = e.get("detail") or e.get("title") or ""
        if msg:
            errors.append(msg.strip())
    return errors


def compute_flags(row):
    flags = []
    latest, staging, prod = (to_int(row["latest_version"]), to_int(row["staging_version"]),
                             to_int(row["production_version"]))
    if staging is None:
        flags.append("NOT_ON_STAGING")
    elif latest is not None and staging < latest:
        flags.append("STAGING_BEHIND_LATEST")
    if prod is None:
        flags.append("NOT_ON_PRODUCTION")
    elif staging is not None and prod != staging:
        flags.append("PROD_DIFFERS_FROM_STAGING")
    if row.get("staging_pending") or row.get("production_pending"):
        flags.append("PENDING_ACTIVATION")
    if row.get("staging_last_failed") or row.get("production_last_failed"):
        flags.append("LAST_ACTIVATION_FAILED")
    if row.get("validation_error_count"):
        flags.append("VALIDATION_ERRORS")
    if row.get("error"):
        flags.append("FETCH_ERROR")
    return flags


def collect(client, props, args):
    rows, activations = [], []
    total = len(props)
    for idx, p in enumerate(props, start=1):
        row = {
            "group_id": p.get("groupId", ""),
            "group_name": p.get("groupName", ""),
            "contract_id": p.get("contractId", ""),
            "property_id": p.get("propertyId", ""),
            "property_name": p.get("propertyName", ""),
            "latest_version": p.get("latestVersion", ""),
            "staging_version": p.get("stagingVersion") if p.get("stagingVersion") is not None else "",
            "production_version": p.get("productionVersion") if p.get("productionVersion") is not None else "",
            "asset_id": p.get("assetId", ""),
            "note": p.get("note", ""),
        }
        errors = []

        # Row-level isolation: one property failing never stops the snapshot.
        if not args.skip_activations:
            try:
                items = client.get(
                    f"/papi/v1/properties/{p['propertyId']}/activations",
                    params={"contractId": p["contractId"], "groupId": p["groupId"]},
                ).get("activations", {}).get("items", [])
                row.update(summarize_activations(items))
                for a in items:
                    activations.append({
                        "property_id": p["propertyId"],
                        "property_name": p.get("propertyName", ""),
                        "activation_id": a.get("activationId", ""),
                        "network": a.get("network", ""),
                        "version": a.get("propertyVersion", ""),
                        "type": a.get("activationType", ""),
                        "status": a.get("status", ""),
                        "submitted": a.get("submitDate", ""),
                        "updated": a.get("updateDate", ""),
                        "note": a.get("note", ""),
                        "notify_emails": "; ".join(a.get("notifyEmails") or []),
                    })
            except PapiError as exc:
                errors.append(f"activations: {exc}")

        if args.hostnames:
            ref_version = to_int(row["production_version"]) or to_int(row["staging_version"]) \
                or to_int(row["latest_version"])
            try:
                row["hostnames_version"] = ref_version or ""
                row["hostnames"] = "; ".join(fetch_hostnames(client, p, ref_version)) if ref_version else ""
            except PapiError as exc:
                errors.append(f"hostnames: {exc}")

        if args.validate_rules:
            latest = to_int(row["latest_version"])
            try:
                errs = fetch_rule_errors(client, p, latest) if latest else []
                row["validation_error_count"] = len(errs)
                row["validation_errors"] = " | ".join(errs[:3])
            except PapiError as exc:
                errors.append(f"rules: {exc}")

        row["error"] = " ; ".join(errors)
        row["flags"] = compute_flags(row)
        rows.append(row)
        if idx % 10 == 0 or idx == total:
            log.info("Processed %d/%d properties", idx, total)
    rows.sort(key=lambda r: (r["property_name"] or "").lower())
    return rows, activations


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_csv(path, records, preferred_order=None):
    if not records:
        return
    fields = list(preferred_order or [])
    for rec in records:
        for k in rec:
            if k not in fields:
                fields.append(k)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            out = dict(rec)
            if isinstance(out.get("flags"), list):
                out["flags"] = "; ".join(out["flags"])
            writer.writerow(out)


def print_summary(rows, out_dir, args):
    def count(flag):
        return sum(1 for r in rows if flag in r["flags"])

    print()
    print("=" * 60)
    print("SNAPSHOT SUMMARY")
    print("=" * 60)
    print(f"Properties scanned        : {len(rows)}")
    print(f"Live on STAGING           : {len(rows) - count('NOT_ON_STAGING')}")
    print(f"Live on PRODUCTION        : {len(rows) - count('NOT_ON_PRODUCTION')}")
    print(f"Staging behind latest     : {count('STAGING_BEHIND_LATEST')}")
    print(f"Prod differs from staging : {count('PROD_DIFFERS_FROM_STAGING')}")
    if not args.skip_activations:
        print(f"Pending activations       : {count('PENDING_ACTIVATION')}")
        print(f"Last activation failed    : {count('LAST_ACTIVATION_FAILED')}")
    if args.validate_rules:
        print(f"Latest has rule errors    : {count('VALIDATION_ERRORS')}")
    print(f"Fetch errors              : {count('FETCH_ERROR')}")

    attention = [r for r in rows if any(f in r["flags"] for f in ATTENTION_FLAGS + ("FETCH_ERROR",))]
    if attention:
        print("\nNeeds attention:")
        table = [
            [r["property_name"], r["property_id"], r["latest_version"], r["staging_version"] or "-",
             r["production_version"] or "-",
             ", ".join(f for f in r["flags"] if f in ATTENTION_FLAGS + ("FETCH_ERROR",))]
            for r in attention
        ]
        print(tabulate(table, headers=["Property", "ID", "Latest", "Staging", "Prod", "Flags"],
                       tablefmt="grid"))
    print(f"\nFiles written to: {out_dir}")
    print("=" * 60)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args():
    p = argparse.ArgumentParser(description="Read-only snapshot of Akamai properties and activation status.")
    p.add_argument("--edgerc", default="~/.edgerc", help="Path to .edgerc (default: ~/.edgerc)")
    p.add_argument("--section", default="lab", help="Section in .edgerc (default: lab)")
    p.add_argument("--out", default=None, help="Output folder (default: akamai_snapshot_<timestamp>)")
    p.add_argument("--group", help="Only this group (group ID, or part of the group name)")
    p.add_argument("--name-filter", help="Only properties whose name contains this text")
    p.add_argument("--limit", type=int, help="Only process the first N properties (good for a test run)")
    p.add_argument("--skip-activations", action="store_true",
                   help="Skip per-property activation history (fastest)")
    p.add_argument("--hostnames", action="store_true", help="Also fetch hostnames (1 extra call per property)")
    p.add_argument("--validate-rules", action="store_true",
                   help="Also check each property's latest version for rule-tree validation errors "
                        "(1 extra call per property)")
    return p.parse_args()


def run_snapshot(client, args):
    props = list_properties(client, args.group)
    if args.name_filter:
        props = [p for p in props if args.name_filter.lower() in (p.get("propertyName") or "").lower()]
    props.sort(key=lambda p: (p.get("propertyName") or "").lower())
    if args.limit:
        props = props[: args.limit]
    log.info("Found %d properties to process", len(props))
    if not props:
        return [], [], []
    rows, activations = collect(client, props, args)
    return props, rows, activations


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = parse_args()

    try:
        client = PapiClient(args.edgerc, args.section)
        props, rows, activations = run_snapshot(client, args)
    except PapiError as exc:
        log.error("%s", exc)
        return 1

    if not rows:
        log.warning("No properties found. Check the section, group filter and API client permissions.")
        return 1

    out_dir = args.out or datetime.now().strftime("akamai_snapshot_%Y%m%d_%H%M%S")
    os.makedirs(out_dir, exist_ok=True)

    order = ["property_name", "property_id", "group_id", "group_name", "contract_id",
             "latest_version", "staging_version", "production_version", "flags"]
    write_csv(os.path.join(out_dir, "properties.csv"), rows, order)
    write_csv(os.path.join(out_dir, "activations.csv"), activations)
    with open(os.path.join(out_dir, "snapshot.json"), "w", encoding="utf-8") as fh:
        json.dump({"generated": datetime.now().isoformat(timespec="seconds"),
                   "section": args.section, "properties": rows, "activations": activations},
                  fh, indent=2)

    print_summary(rows, out_dir, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
