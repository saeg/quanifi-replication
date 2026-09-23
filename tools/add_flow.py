#!/usr/bin/env python3
"""
Programmatically adds a processor chain to a NiFi flow.json.gz.

Usage:
    python3 add_flow.py [--conf /path/to/conf/flow.json.gz]

Pass the target NiFi ``conf/flow.json.gz`` with ``--conf``.
NiFi must be stopped before running this script. Changes take effect on restart.

To add a different chain, edit the FLOW_CHAIN list at the bottom.
"""

import argparse
import gzip
import json
import shutil
import uuid
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NIFI_BUNDLE   = {"group": "org.apache.nifi", "artifact": "nifi-standard-nar",   "version": "2.9.0"}
PYTHON_BUNDLE = {"group": "org.apache.nifi", "artifact": "python-extensions",   "version": "0.1.0"}


def nid():
    return str(uuid.uuid4())


def make_processor(*, name, proc_type, bundle, properties,
                   x, y, group_id,
                   scheduling_period="0 sec",
                   auto_terminate=None,
                   scheduled_state="RUNNING",
                   run_duration_millis=0):
    """Build one processor entry for flow.json.gz.

    ``run_duration_millis`` defaults to 0, which is NiFi's own default, and
    changing it is almost never what you want here. Run Duration tells NiFi to
    keep calling onTrigger in a loop for that many milliseconds *per scheduled
    execution*. On a processor with an input queue that is a harmless throughput
    tweak, but on a SOURCE it overrides the Run Schedule entirely: a
    GenerateFlowFile with "Run Schedule = 8760 h" and Run Duration 25 ms emits
    hundreds of FlowFiles in one burst, because the schedule only decides when
    the loop starts, not how many times it runs.

    This file used to hardcode 25, which is why add_iqm_group.py and
    add_ibm_grover_group.py both had to reset it to 0 by hand on their one-shot
    triggers, and why a single "fire once" on the arithmetic canvas produced 458
    batches instead of one.
    """
    pid = nid()
    iid = nid()
    return {
        "identifier":         pid,
        "instanceIdentifier": iid,
        "name":               name,
        "comments":           "",
        "position":           {"x": float(x), "y": float(y)},
        "type":               proc_type,
        "bundle":             bundle,
        "properties":         properties,
        "propertyDescriptors": {},
        "style":              {},
        "schedulingPeriod":   scheduling_period,
        "schedulingStrategy": "TIMER_DRIVEN",
        "executionNode":      "ALL",
        "penaltyDuration":    "30 sec",
        "yieldDuration":      "1 sec",
        "bulletinLevel":      "WARN",
        "runDurationMillis":  run_duration_millis,
        "concurrentlySchedulableTaskCount": 1,
        "autoTerminatedRelationships": auto_terminate or [],
        "scheduledState":     scheduled_state,
        "retryCount":         10,
        "retriedRelationships": [],
        "backoffMechanism":   "PENALIZE_FLOWFILE",
        "maxBackoffPeriod":   "10 mins",
        "componentType":      "PROCESSOR",
        "groupIdentifier":    group_id,
    }


def make_connection(src, src_rel, dst, group_id, z_index):
    return {
        "identifier":         nid(),
        "instanceIdentifier": nid(),
        "name":               "",
        "source": {
            "id":                 src["identifier"],
            "type":               "PROCESSOR",
            "groupId":            group_id,
            "name":               src["name"],
            "comments":           "",
            "instanceIdentifier": src["instanceIdentifier"],
        },
        "destination": {
            "id":                 dst["identifier"],
            "type":               "PROCESSOR",
            "groupId":            group_id,
            "name":               dst["name"],
            "comments":           "",
            "instanceIdentifier": dst["instanceIdentifier"],
        },
        "labelIndex":                    0,
        "zIndex":                        z_index,
        "selectedRelationships":         [src_rel],
        "backPressureObjectThreshold":   10000,
        "backPressureDataSizeThreshold": "1 GB",
        "flowFileExpiration":            "0 sec",
        "prioritizers":                  [],
        "bends":                         [],
        "loadBalanceStrategy":           "DO_NOT_LOAD_BALANCE",
        "partitioningAttribute":         "",
        "loadBalanceCompression":        "DO_NOT_COMPRESS",
        "componentType":                 "CONNECTION",
        "groupIdentifier":               group_id,
    }


def make_label(text, x, y, width, height, group_id, style=None):
    return {
        "identifier":         nid(),
        "instanceIdentifier": nid(),
        "position":           {"x": float(x), "y": float(y)},
        "width":              float(width),
        "height":             float(height),
        "label":              text,
        "style":              dict({
            "background-color": "#1c2128",
            "border-color":     "#21262d",
            "font-color":       "#8b949e",
            "font-size":        "12px",
        }, **(style or {})),
        "zIndex":             0,
        "componentType":      "LABEL",
        "groupIdentifier":    group_id,
    }


# ---------------------------------------------------------------------------
# Chain builder
# ---------------------------------------------------------------------------

def add_chain(flow_path, chain_spec, start_x, start_y, h_gap=536.0):
    """
    chain_spec: list of dicts, each with keys:
        name, type, bundle, properties,
        auto_terminate (list), scheduling_period (str, optional),
        outgoing_relationship (str) — relationship to connect to next step
    """
    path = Path(flow_path)

    # Backup
    backup = path.with_suffix(f".{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak.gz")
    shutil.copy2(path, backup)
    print(f"Backup written: {backup}")

    with gzip.open(path, "rb") as f:
        flow = json.load(f)

    root     = flow["rootGroup"]
    group_id = root["instanceIdentifier"]
    max_z    = max((c.get("zIndex", 0) for c in root["connections"]), default=0)

    processors = []
    for i, spec in enumerate(chain_spec):
        x = start_x + i * h_gap
        p = make_processor(
            name             = spec["name"],
            proc_type        = spec["type"],
            bundle           = spec["bundle"],
            properties       = spec.get("properties", {}),
            x                = x,
            y                = start_y,
            group_id         = group_id,
            scheduling_period= spec.get("scheduling_period", "0 sec"),
            auto_terminate   = spec.get("auto_terminate", []),
            scheduled_state  = spec.get("scheduled_state", "RUNNING"),
        )
        processors.append(p)

    connections = []
    for i in range(len(processors) - 1):
        rel = chain_spec[i].get("outgoing_relationship", "success")
        connections.append(
            make_connection(processors[i], rel, processors[i + 1], group_id, max_z + i + 1)
        )

    # Label above the chain
    label_text = " → ".join(s["name"] for s in chain_spec)
    label = make_label(
        label_text,
        x      = start_x,
        y      = start_y - 60,
        width  = h_gap * (len(chain_spec) - 1) + 380,
        height = 40,
        group_id = group_id,
    )

    root["processors"].extend(processors)
    root["connections"].extend(connections)
    root["labels"].append(label)

    with gzip.open(path, "wb") as f:
        f.write(json.dumps(flow, indent=2).encode("utf-8"))

    print(f"Added {len(processors)} processors, {len(connections)} connections.")
    for p in processors:
        print(f"  {p['name']:<35} @ ({p['position']['x']:.0f}, {p['position']['y']:.0f})")


# ---------------------------------------------------------------------------
# Flow definition
# ---------------------------------------------------------------------------

REPORTS_DIR = "reports"

QISKIT_GROVER_CHAIN = [
    {
        "name":                  "GenerateFlowFile",
        "type":                  "org.apache.nifi.processors.standard.GenerateFlowFile",
        "bundle":                NIFI_BUNDLE,
        "properties":            {"File Size": "0B", "Batch Size": "1",
                                  "Unique FlowFiles": "false", "Data Format": "Text"},
        "scheduling_period":     "30 sec",
        "scheduled_state":       "RUNNING",
        "auto_terminate":        [],
        "outgoing_relationship": "success",
    },
    {
        "name":                  "QiskitGroverCircuit",
        "type":                  "QiskitGroverCircuit",
        "bundle":                PYTHON_BUNDLE,
        "properties":            {"Marked State": "0101", "Num Iterations": "2",
                                  "Insert Barriers": "false", "Output Format": "qasm3"},
        "auto_terminate":        ["original", "failure"],
        "outgoing_relationship": "success",
    },
    {
        "name":                  "QiskitAerSimulator",
        "type":                  "QiskitAerSimulator",
        "bundle":                PYTHON_BUNDLE,
        "properties":            {"Shots": "1024"},
        "auto_terminate":        ["failure"],
        "outgoing_relationship": "success",
    },
    {
        "name":                  "QuanifiReport",
        "type":                  "QuanifiReport",
        "bundle":                PYTHON_BUNDLE,
        "properties":            {"Reports Directory": REPORTS_DIR,
                                  "Flow Name": "grover-qiskit"},
        "auto_terminate":        ["success", "failure"],
    },
]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add a processor chain to a NiFi flow.json.gz")
    parser.add_argument(
        "--conf",
        required=True,
        help="Path to NiFi conf/flow.json.gz",
    )
    parser.add_argument("--x", type=float, default=456.0,  help="Start X position")
    parser.add_argument("--y", type=float, default=200.0,  help="Start Y position")
    parser.add_argument("--gap", type=float, default=536.0, help="Horizontal gap between processors")
    args = parser.parse_args()

    add_chain(args.conf, QISKIT_GROVER_CHAIN, start_x=args.x, start_y=args.y, h_gap=args.gap)
    print(f"\nDone. Restart NiFi to see the new flow.")
