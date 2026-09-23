"""
Cost the arithmetic hardware batch against real devices. Submit nothing.

Runs the production submitter's `prepare()` against live backends: it resolves
the device, reads its coupling map and timing, pads the mixed-width batch,
searches a layout, transpiles, and estimates QPU usage. `run_job()` is never
called, so no job is created and nothing is billed.

This is the headless twin of setting `Submit Mode = preflight` on the canvas
group, and it answers the only question left before arming: does the run fit in
the allowance.

    ../.venv/bin/python arithmetic_preflight.py
    ../.venv/bin/python arithmetic_preflight.py --provider ibm --device ibm_brisbane
    ../.venv/bin/python arithmetic_preflight.py --suite exhaustive
"""

import argparse
import json
import os
import sys
from pathlib import Path

import _harness  # noqa: F401  (side effect: nifiapi stubs + extensions path)

from conftest import MockContext, MockFlowFile  # noqa: E402

import arithmetic_spec as aspec  # noqa: E402
from QuantumSuccessProbabilityOracle import required_shots  # noqa: E402

from QiskitQuantumArithmetic import QiskitQuantumArithmetic  # noqa: E402
from CirqQuantumArithmetic import CirqQuantumArithmetic  # noqa: E402
from PennylaneQuantumArithmetic import PennylaneQuantumArithmetic  # noqa: E402

# One adder per algorithm family; cdkm and ripple_carry are the same circuit.
BRANCHES = [
    ("qiskit/cdkm", QiskitQuantumArithmetic, "cdkm"),
    ("cirq/qft", CirqQuantumArithmetic, "qft"),
    ("pennylane/semiadder", PennylaneQuantumArithmetic, "semiadder"),
]

DEFAULT_DEVICES = {"ibm": "ibm_brisbane", "iqm": "garnet"}


def load_dotenv():
    """Merge the repo-root .env into os.environ without overriding real env vars."""
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load_cases(suite, bit_width, cases_file=None):
    """The cases a headless run covers, from a generated suite or a manifest.

    Both go through the parser and derivation the canvas processor uses, so a
    headless preflight and a canvas preflight cover the same inputs and can be
    compared. Returns ``(cases, provenance)``; the provenance dict is what a
    result archive records so a run can be traced back to its definition.
    """
    if cases_file:
        manifest = aspec.parse_manifest(
            Path(cases_file).expanduser().read_text(encoding="utf-8"))
        return manifest["cases"], {
            "source": "manifest",
            # The absolute path is deliberately not archived: the basename,
            # suite id and digests identify the definition, and the committed
            # path belongs in campaign documentation.
            "cases_file": Path(cases_file).name,
            "suite_id": manifest["suite_id"],
            "schema": manifest["schema"],
            "case_set_sha256": manifest["case_set_sha256"],
            "manifest_sha256": manifest["manifest_sha256"],
            "raw_sha256": manifest["raw_sha256"],
        }
    cases = aspec.suite(suite, bit_width)
    return cases, {
        "source": "generated-suite",
        "suite": suite,
        "bit_width": bit_width,
        "case_set_sha256": aspec.case_set_digest(
            cases[0]["operation"], cases[0]["bit_width"], aspec.case_pairs(cases)),
    }


def build_batch(suite, bit_width, cases=None):
    """Every circuit one run would submit, with its ground truth attached.

    ``cases`` overrides the generated suite, so an explicit manifest and a
    generated suite reach the builders through exactly the same path.
    """
    cases = aspec.suite(suite, bit_width) if cases is None else cases
    qasms, attributes = [], []
    for case in cases:
        for _, processor, implementation in BRANCHES:
            result = processor().transform(
                MockContext(**{"Operation": "add",
                               "Operand A": str(case["a"]),
                               "Operand B": str(case["b"]),
                               # From the case, not the argument: a
                               # manifest carries its own bit width.
                               "Bit Width": str(case["bit_width"]),
                               "Implementation": implementation,
                               "Signed": "false"}),
                MockFlowFile())
            if result.relationship != "success":
                raise SystemExit("builder failed: %s" % (result.attributes,))
            qasms.append(result.contents.decode("utf-8"))
            attributes.append(result.attributes)
    return cases, qasms, attributes


def preflight_ibm(qasms, shots, device, seeds, attributes=None,
                  excluded_physical_qubits=(), prior_layouts=()):
    from QuantumIBMBatchSubmitter import QuantumIBMBatchSubmitter

    token = os.environ.get("IBM_QUANTUM_TOKEN", "").strip()
    if not token:
        raise SystemExit("IBM_QUANTUM_TOKEN not set")
    instance = os.environ.get("IBM_QUANTUM_INSTANCE", "").strip()
    # Caps deliberately wide: preflight is here to REPORT the cost, not to
    # refuse it. The caps that matter are the ones set on the canvas.
    return QuantumIBMBatchSubmitter().prepare(
        qasms, shots, device, token, instance, seeds,
        max_pending=10 ** 6, max_usage=10 ** 9, metadata=attributes,
        excluded_physical_qubits=excluded_physical_qubits,
        prior_layouts=prior_layouts)


def preflight_iqm(qasms, shots, device, seeds, attributes=None,
                  excluded_physical_qubits=(), prior_layouts=()):
    from QuantumIQMBatchSubmitter import QuantumIQMBatchSubmitter

    token = os.environ.get("IQM_TOKEN", "").strip()
    if not token and Path("token_iqm_neilsonramalho.txt").is_file():
        token = Path("token_iqm_neilsonramalho.txt").read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit("IQM_TOKEN not set")
    server = os.environ.get("IQM_SERVER_URL", "https://resonance.iqm.tech")
    return QuantumIQMBatchSubmitter().prepare(
        qasms, shots, device, server, token, seeds, max_usage=10 ** 9,
        metadata=attributes, excluded_physical_qubits=excluded_physical_qubits,
        prior_layouts=prior_layouts)


def report(provider, device, plan, cases, shots, control_p, min_diff):
    needed = required_shots(control_p, min_diff)
    lines = [
        "",
        "=" * 68,
        "PREFLIGHT  %s / %s   (nothing submitted)" % (provider.upper(), device),
        "=" * 68,
        "  test cases              %d" % len(cases),
        "  circuits in the job     %d   (%d cases x %d branches)"
        % (len(plan["widths"]), len(cases), len(BRANCHES)),
        "  circuit widths          %s"
        % ",".join(str(w) for w in sorted(set(plan["widths"]))),
        "  padded width            %d" % plan["padded_width"],
        "  device qubits           %s" % (plan["capacity"] or "unknown"),
        "  layout                  %s" % (plan["layout"],),
        "  two-qubit gates (max)   %d" % plan["two_qubit_gates"],
        "  shots per circuit       %d" % shots,
        "  estimated QPU usage     %.3f s   (FLOOR: excludes reset, readout, queue)"
        % plan["estimated_usage"],
        "  required shots per arm  %d   (p_ref=%.2f, delta=%.2f)"
        % (needed, control_p, min_diff),
        "  shots sufficient        %s" % ("yes" if shots >= needed else "NO"),
    ]
    if plan.get("pending") is not None:
        lines.append("  queue depth             %s" % plan["pending"])
    fits = plan["capacity"] and plan["padded_width"] <= plan["capacity"]
    lines.append("  fits on this device     %s" % ("yes" if fits else "NO"))
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--provider", choices=["ibm", "iqm", "both"], default="both")
    p.add_argument("--device", default=None)
    # Mutually exclusive: two definitions of what to run is one too many, and
    # argparse only complains when both are given explicitly, so --suite keeps
    # its default for every existing invocation.
    cases_from = p.add_mutually_exclusive_group()
    cases_from.add_argument("--suite", default="boundary",
                            choices=["boundary", "exhaustive"])
    cases_from.add_argument("--cases-file", default=None,
                            help="a versioned arithmetic case manifest to run "
                                 "instead of a generated suite "
                                 "(experiments/test_cases/*.json), parsed by the "
                                 "same arithmetic_spec parser the canvas "
                                 "processor uses")
    p.add_argument("--bit-width", type=int, default=2,
                    help="ignored with --cases-file: a manifest carries its own")
    p.add_argument("--shots", type=int, default=512)
    p.add_argument("--layout-seeds", type=int, default=4)
    p.add_argument("--excluded-physical-qubits", default="",
                   help="IBM only: comma-separated qubits that no candidate "
                        "may touch (the v4 clean-screen rule excludes q16)")
    p.add_argument("--avoid-layout", action="append", default=[],
                   help="IBM only: prior failed comma-separated layout; repeat "
                        "to make the five screened regions genuinely different")
    p.add_argument("--control-success", type=float, default=0.43)
    p.add_argument("--min-difference", type=float, default=0.10)
    p.add_argument("--out", default=None)
    args = p.parse_args()

    load_dotenv()
    cases, provenance = load_cases(args.suite, args.bit_width, args.cases_file)
    _, qasms, attributes = build_batch(args.suite, args.bit_width, cases)
    if args.cases_file:
        print("built %d circuits (%d cases x %d branches) from %s"
              % (len(qasms), len(cases), len(BRANCHES), provenance["cases_file"]))
        print("  suite id : %s" % provenance["suite_id"])
        print("  case set : %s" % provenance["case_set_sha256"])
        print("  manifest : %s" % provenance["manifest_sha256"])
    else:
        print("built %d circuits (%d cases x %d branches), suite=%s width=%d"
              % (len(qasms), len(cases), len(BRANCHES), args.suite, args.bit_width))
    scorable = sum(1 for a in attributes if a.get(aspec.ATTR_RESULT_BITS))
    print("ground truth present on %d/%d circuits" % (scorable, len(qasms)))

    providers = ["ibm", "iqm"] if args.provider == "both" else [args.provider]
    results = {}
    for provider in providers:
        device = args.device or DEFAULT_DEVICES[provider]
        runner = preflight_ibm if provider == "ibm" else preflight_iqm
        try:
            excluded = [int(q) for q in args.excluded_physical_qubits.split(",")
                        if q.strip()]
            prior = [[int(q) for q in value.split(",") if q.strip()]
                     for value in args.avoid_layout]
            plan = runner(qasms, args.shots, device, args.layout_seeds,
                          attributes, excluded, prior)
        except Exception as exc:  # noqa: BLE001 - report, never raise past here

            print("\n%s / %s: preflight unavailable -- %s"
                  % (provider.upper(), device, exc))
            results[provider] = {"device": device, "error": str(exc)}
            continue
        print(report(provider, device, plan, cases, args.shots,
                     args.control_success, args.min_difference))
        results[provider] = {
            "device": device, "circuits": len(plan["widths"]),
            "widths": plan["widths"], "padded_width": plan["padded_width"],
            "device_qubits": plan["capacity"], "layout": list(plan["layout"]),
            "two_qubit_gates": plan["two_qubit_gates"],
            "layout_evidence": plan.get("layout_evidence", {}),
            "estimated_usage_seconds": plan["estimated_usage"],
            "shots": args.shots,
            "required_shots_per_arm": required_shots(args.control_success,
                                                     args.min_difference),
        }

    if args.out:
        # The case-set identity travels with the costing, so a preflight record
        # can be matched against the campaign it was costing.
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps({"cases": provenance, "providers": results},
                       indent=2, default=str))
        print("\nwrote %s" % args.out)
    print("\nNo job was submitted. Arming is a separate, deliberate step.")
    return results


if __name__ == "__main__":
    sys.exit(0 if main() else 0)
