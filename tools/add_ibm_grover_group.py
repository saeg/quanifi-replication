#!/usr/bin/env python3
"""Add a one-shot two-qubit Grover hardware process group to a stopped NiFi."""

import argparse
import gzip
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from add_flow import NIFI_BUNDLE, PYTHON_BUNDLE, make_connection, make_processor


GROUP_NAME = "IBM Quantum — 2-Qubit Grover (Real Hardware)"
PARAMETER_CONTEXT = "IBM Quantum Credentials"
REPORT_BUNDLE = {"group": "org.apache.nifi", "artifact": "python-extensions", "version": "0.2.0"}


def uid():
    return str(uuid.uuid4())


def add_group(flow_path: Path, token_path: Path, reports_dir: Path) -> None:
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"IBM token file is empty: {token_path}")

    backup = flow_path.with_name(
        f"{flow_path.name}.bak_ibm_grover_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    shutil.copy2(flow_path, backup)
    with gzip.open(flow_path, "rt", encoding="utf-8") as handle:
        flow = json.load(handle)

    if any(context.get("name") == PARAMETER_CONTEXT for context in flow["parameterContexts"]):
        raise ValueError(f"Parameter Context already exists: {PARAMETER_CONTEXT}")
    if any(group.get("name") == GROUP_NAME for group in flow["rootGroup"]["processGroups"]):
        raise ValueError(f"Process Group already exists: {GROUP_NAME}")

    context_id, context_instance_id = uid(), uid()
    flow["parameterContexts"].append({
        "identifier": context_id,
        "instanceIdentifier": context_instance_id,
        "name": PARAMETER_CONTEXT,
        "description": "Sensitive credentials used by IBM Quantum hardware processors.",
        "parameters": [{
            "name": "ibm.quantum.token",
            "description": "IBM Quantum Platform API key.",
            "sensitive": True,
            "provided": False,
            "value": token,
            "referencedAssets": [],
        }],
        "inheritedParameterContexts": [],
        "componentType": "PARAMETER_CONTEXT",
    })

    root = flow["rootGroup"]
    group_id, group_instance_id = uid(), uid()
    group = {
        "identifier": group_id,
        "instanceIdentifier": group_instance_id,
        "name": GROUP_NAME,
        "comments": (
            "One-shot two-qubit Grover search on the least-busy IBM Quantum QPU. "
            "Uses the sensitive ibm.quantum.token parameter."
        ),
        "position": {"x": -25000.0, "y": -3600.0},
        "processGroups": [], "remoteProcessGroups": [], "processors": [],
        "inputPorts": [], "outputPorts": [], "connections": [], "labels": [],
        "funnels": [], "controllerServices": [],
        "defaultFlowFileExpiration": "0 sec",
        "defaultBackPressureObjectThreshold": 10000,
        "defaultBackPressureDataSizeThreshold": "1 GB",
        "scheduledState": "ENABLED",
        "executionEngine": "INHERITED",
        "maxConcurrentTasks": 1,
        "statelessFlowTimeout": "1 min",
        "flowFileOutboundPolicy": "STREAM_WHEN_AVAILABLE",
        "flowFileConcurrency": "UNBOUNDED",
        "parameterContextName": PARAMETER_CONTEXT,
        "componentType": "PROCESS_GROUP",
        "groupIdentifier": root["instanceIdentifier"],
    }

    specs = [
        ("Grover Hardware — One-Shot Trigger", "org.apache.nifi.processors.standard.GenerateFlowFile",
         NIFI_BUNDLE, {"File Size": "0B", "Batch Size": "1", "Unique FlowFiles": "false",
                       "Data Format": "Text"}, [], "1 day"),
        ("Grover Hardware — Build |11>", "QiskitGroverCircuit", PYTHON_BUNDLE,
         {"Marked State": "11", "Num Iterations": "1", "Insert Barriers": "false",
          "Output Format": "qasm2"}, ["original", "failure"], "0 sec"),
        ("Grover Hardware — IBM Least Busy", "QiskitRuntimeSampler", PYTHON_BUNDLE,
         {"Backend": "least_busy", "Token": "#{ibm.quantum.token}", "Shots": "100"},
         ["failure"], "0 sec"),
        ("Grover Hardware — Collect Report", "QuanifiReport", REPORT_BUNDLE,
         {"Reports Directory": str(reports_dir), "Flow Name": "ibm_hardware_grover_2q"},
         ["success", "failure"], "0 sec"),
    ]
    processors = []
    for index, (name, proc_type, bundle, properties, auto_terminate, schedule) in enumerate(specs):
        processor = make_processor(
            name=name, proc_type=proc_type, bundle=bundle, properties=properties,
            x=100.0 + 500.0 * index, y=200.0, group_id=group_instance_id,
            scheduling_period=schedule, auto_terminate=auto_terminate,
            scheduled_state="RUNNING",
        )
        processor["runDurationMillis"] = 0
        processors.append(processor)
    group["processors"] = processors
    group["connections"] = [
        make_connection(processors[index], "success", processors[index + 1],
                        group_instance_id, index + 1)
        for index in range(len(processors) - 1)
    ]
    root["processGroups"].append(group)

    with gzip.open(flow_path, "wt", encoding="utf-8") as handle:
        json.dump(flow, handle, separators=(",", ":"))
    print(f"Backup: {backup}")
    print(f"Added process group: {GROUP_NAME}")


def finalize_after_hardware_run(flow_path: Path) -> None:
    """Prevent another submission and enable the corrected report collector."""
    backup = flow_path.with_name(
        f"{flow_path.name}.bak_ibm_grover_finalize_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    shutil.copy2(flow_path, backup)
    with gzip.open(flow_path, "rt", encoding="utf-8") as handle:
        flow = json.load(handle)
    for group in flow["rootGroup"].get("processGroups", []):
        if group.get("name") != GROUP_NAME:
            continue
        for processor in group["processors"]:
            if processor["name"] == "Grover Hardware — Collect Report":
                processor["bundle"] = REPORT_BUNDLE
                processor["scheduledState"] = "RUNNING"
            else:
                processor["scheduledState"] = "DISABLED"
        break
    else:
        raise ValueError(f"Process Group not found: {GROUP_NAME}")
    with gzip.open(flow_path, "wt", encoding="utf-8") as handle:
        json.dump(flow, handle, separators=(",", ":"))
    print(f"Backup: {backup}")
    print("Disabled hardware submission nodes and enabled the report collector")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--flow", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args()
    if args.finalize:
        finalize_after_hardware_run(args.flow)
    else:
        add_group(args.flow, args.token_file, args.reports_dir)
