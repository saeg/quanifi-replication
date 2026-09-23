#!/usr/bin/env python3
"""Add a one-shot two-qubit Grover IQM hardware process group to a stopped NiFi.

Mirrors add_ibm_grover_group.py, with one structural difference: the IBM group
submits and waits inside a single processor, while IQM's queue can be long
enough that parking a NiFi thread on it is a bad idea. So the submission is
fire-and-forget (QrispIQMDevice, Wait For Results = false) and a separate
IQMJobPoller polls the IQM Resonance REST API, looping on its own 'pending'
relationship until the job reaches a terminal state:

    Trigger -> QiskitGroverCircuit -> QrispIQMDevice -> IQMJobPoller -> QuanifiReport
                                                            ^     |
                                                            +-----+  (pending)

NiFi must be stopped. A timestamped backup of flow.json.gz is written first.
"""

import argparse
import gzip
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from add_flow import NIFI_BUNDLE, PYTHON_BUNDLE, make_connection, make_processor


GROUP_NAME = "IQM Quantum — 2-Qubit Grover (Real Hardware)"
PARAMETER_CONTEXT = "IQM Quantum Credentials"
TOKEN_PARAMETER = "iqm.resonance.token"
# QuanifiReport is the one processor already on the canvas at 0.2.0.
REPORT_BUNDLE = {"group": "org.apache.nifi", "artifact": "python-extensions", "version": "0.2.0"}


def uid():
    return str(uuid.uuid4())


def add_group(flow_path: Path, token_path: Path, reports_dir: Path,
              device_instance: str, shots: int, armed: bool = False) -> None:
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"IQM token file is empty: {token_path}")

    backup = flow_path.with_name(
        f"{flow_path.name}.bak_iqm_grover_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
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
        "description": "Sensitive credentials used by the IQM Resonance processors.",
        "parameters": [{
            "name": TOKEN_PARAMETER,
            "description": "IQM Resonance API token.",
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
            "One-shot two-qubit Grover search on an IQM Resonance QPU, submitted "
            "through Qrisp's Backend Interface and collected by polling the IQM "
            f"Resonance REST API. Uses the sensitive {TOKEN_PARAMETER} parameter."
        ),
        # Parked next to, not on top of, the IBM group.
        "position": {"x": -25000.0, "y": -2800.0},
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

    token_ref = "#{" + TOKEN_PARAMETER + "}"
    specs = [
        ("IQM Grover — One-Shot Trigger",
         "org.apache.nifi.processors.standard.GenerateFlowFile", NIFI_BUNDLE,
         {"File Size": "0B", "Batch Size": "1", "Unique FlowFiles": "false",
          "Data Format": "Text"}, [], "1 day"),
        # qasm2, not qasm3: Qrisp's importer is the qasm2 one.
        ("IQM Grover — Build |11>", "QiskitGroverCircuit", PYTHON_BUNDLE,
         {"Marked State": "11", "Num Iterations": "1", "Insert Barriers": "false",
          "Output Format": "qasm2"}, ["original", "failure"], "0 sec"),
        ("IQM Grover — Submit to IQM", "QrispIQMDevice", PYTHON_BUNDLE,
         {"Device Instance": device_instance, "API Token": token_ref,
          "Shots": str(shots), "Wait For Results": "false"},
         ["failure"], "0 sec"),
        # Runs every 30s: each pass polls for up to 60s, then re-queues itself
        # on 'pending', so a long queue costs many cheap passes.
        ("IQM Grover — Poll Resonance API", "IQMJobPoller", PYTHON_BUNDLE,
         {"API Token": token_ref, "Job ID": "${hw.job_id}",
          "Poll Timeout Seconds": "60", "Poll Interval Seconds": "5",
          "Max Poll Interval Seconds": "60"},
         ["failure"], "30 sec"),
        ("IQM Grover — Collect Report", "QuanifiReport", REPORT_BUNDLE,
         {"Reports Directory": str(reports_dir), "Flow Name": "iqm_hardware_grover_2q"},
         ["success", "failure"], "0 sec"),
    ]
    processors = []
    for index, (name, proc_type, bundle, properties, auto_terminate, schedule) in enumerate(specs):
        # The trigger is left DISABLED unless --armed: starting NiFi would
        # otherwise immediately spend real QPU credits on the user's IQM
        # account. Everything downstream is running, so arming it in the UI
        # (right-click -> Enable -> Start) is a one-click decision.
        is_trigger = index == 0
        processor = make_processor(
            name=name, proc_type=proc_type, bundle=bundle, properties=properties,
            x=100.0 + 500.0 * index, y=200.0, group_id=group_instance_id,
            scheduling_period=schedule, auto_terminate=auto_terminate,
            scheduled_state="RUNNING" if (armed or not is_trigger) else "DISABLED",
        )
        processor["runDurationMillis"] = 0
        processors.append(processor)
    group["processors"] = processors

    poller = processors[3]
    connections = [
        make_connection(processors[index], "success", processors[index + 1],
                        group_instance_id, index + 1)
        for index in range(len(processors) - 1)
    ]
    # Self-loop: a job that is still queued comes straight back for another pass.
    pending = make_connection(poller, "pending", poller, group_instance_id,
                              len(connections) + 1)
    pending["name"] = "still queued"
    pending["bends"] = [{"x": 1700.0, "y": 60.0}]
    connections.append(pending)
    group["connections"] = connections

    root["processGroups"].append(group)

    identifiers = []
    for section in ("processors", "connections", "labels", "funnels", "processGroups"):
        identifiers.extend(item["identifier"] for item in group.get(section, []))
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("duplicate component UUID detected; refusing to write")

    with gzip.open(flow_path, "wt", encoding="utf-8") as handle:
        json.dump(flow, handle, separators=(",", ":"))
    print(f"Backup: {backup}")
    print(f"Added process group: {GROUP_NAME}")
    print("  " + " -> ".join(spec[0] for spec in specs))
    print(f"  device_instance={device_instance}  shots={shots}")
    print("  trigger: " + ("RUNNING — will submit on NiFi start"
                           if armed else
                           "DISABLED — enable + start it in the UI to submit"))


def quiesce(flow_path: Path) -> None:
    """Stop the trigger and the submitter after a run, leaving the poller alive.

    A one-shot hardware group should not resubmit on the next NiFi restart, but
    an in-flight job still needs collecting — so only the two upstream nodes
    are disabled.
    """
    backup = flow_path.with_name(
        f"{flow_path.name}.bak_iqm_quiesce_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    shutil.copy2(flow_path, backup)
    with gzip.open(flow_path, "rt", encoding="utf-8") as handle:
        flow = json.load(handle)
    for group in flow["rootGroup"].get("processGroups", []):
        if group.get("name") != GROUP_NAME:
            continue
        changed = 0
        for processor in group["processors"]:
            if processor["name"] in ("IQM Grover — One-Shot Trigger",
                                     "IQM Grover — Submit to IQM"):
                processor["scheduledState"] = "DISABLED"
                changed += 1
        break
    else:
        raise ValueError(f"Process Group not found: {GROUP_NAME}")
    with gzip.open(flow_path, "wt", encoding="utf-8") as handle:
        json.dump(flow, handle, separators=(",", ":"))
    print(f"Backup: {backup}")
    print(f"Disabled {changed} submission processors; the poller stays running")


def remove_group(flow_path: Path) -> None:
    """Undo add_group: drop the process group and its parameter context."""
    backup = flow_path.with_name(
        f"{flow_path.name}.bak_iqm_remove_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    shutil.copy2(flow_path, backup)
    with gzip.open(flow_path, "rt", encoding="utf-8") as handle:
        flow = json.load(handle)
    before = len(flow["rootGroup"]["processGroups"])
    flow["rootGroup"]["processGroups"] = [
        g for g in flow["rootGroup"]["processGroups"] if g.get("name") != GROUP_NAME
    ]
    flow["parameterContexts"] = [
        c for c in flow["parameterContexts"] if c.get("name") != PARAMETER_CONTEXT
    ]
    if len(flow["rootGroup"]["processGroups"]) == before:
        raise ValueError(f"Process Group not found: {GROUP_NAME}")
    with gzip.open(flow_path, "wt", encoding="utf-8") as handle:
        json.dump(flow, handle, separators=(",", ":"))
    print(f"Backup: {backup}")
    print(f"Removed process group and parameter context: {GROUP_NAME}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--flow", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--device-instance", default="garnet",
                        help="IQM quantum computer, e.g. garnet or emerald.")
    parser.add_argument("--shots", type=int, default=100)
    parser.add_argument("--armed", action="store_true",
                        help="Start the trigger RUNNING: NiFi will submit a real "
                             "(billable) IQM job as soon as it boots. Off by default.")
    parser.add_argument("--quiesce", action="store_true")
    parser.add_argument("--remove", action="store_true")
    args = parser.parse_args()
    if args.quiesce:
        quiesce(args.flow)
    elif args.remove:
        remove_group(args.flow)
    else:
        add_group(args.flow, args.token_file, args.reports_dir,
                  args.device_instance, args.shots, args.armed)
