"""The four Grover builders of the builder x engine matrix.

These cover the two new build-only builders (`QrispGroverCircuit`,
`PennylaneGroverCircuit`) and the properties the matrix study depends on for
all four builders: a shared wire format, canonical bit order, declared builder
identity, and a circuit that every counts engine can execute.

Processors are imported from `nifi_extensions_matrix/`, the forked extension
directory the NiFi 2.10.0 instance loads.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from conftest import MockContext, MockFlowFile, result_to_flowfile

FORK = Path(__file__).resolve().parent.parent / "nifi_extensions_matrix"

# The builders are loaded from the fork **by file path**, under distinct module
# names, rather than by putting the fork on sys.path. Putting it first would
# make every test file imported afterwards silently pick up fork copies of
# shared processors; putting it last would resolve the two modified builders to
# their upstream versions instead. Loading by path avoids both. None of the four
# builders imports a sibling module, so no package context is needed.
#
# Engines are unmodified in the fork, so they come from the normal path that
# conftest sets up.


def _load_from_fork(module):
    key = "forkext_" + module
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, FORK / f"{module}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return sys.modules[key]

# (marked state, iterations). Note that `11` and `0110` are both unchanged by
# reversal, so neither can expose a bit-order fault; `110` and `0111` can.
CASES = [("11", 1), ("110", 2), ("0110", 2), ("0111", 2)]

BUILDERS = {
    "qiskit": ("QiskitGroverCircuit", "QiskitGroverCircuit"),
    "cirq": ("CirqGroverCircuit", "CirqGroverCircuit"),
    "qrisp": ("QrispGroverCircuit", "QrispGroverCircuit"),
    "pennylane": ("PennylaneGroverCircuit", "PennylaneGroverCircuit"),
}

ENGINES = {
    "aer": ("QiskitAerSimulator", "QiskitAerSimulator"),
    "cirq": ("CirqSimulator", "CirqSimulator"),
    "qrisp": ("QrispSimulator", "QrispSimulator"),
    "pennylane": ("PennylaneSimulator", "PennylaneSimulator"),
    "braket": ("BraketSimulator", "BraketSimulator"),
    "qsharp": ("QSharpSimulator", "QSharpSimulator"),
}


def _load_builder(spec):
    module, cls = spec
    return getattr(_load_from_fork(module), cls)


def _load_engine(spec):
    module, cls = spec
    return getattr(__import__(module), cls)


def build(builder, marked, iterations):
    cls = _load_builder(BUILDERS[builder])
    return cls().transform(
        MockContext(**{
            "Marked State": marked,
            "Num Iterations": str(iterations),
            "Output Format": "qasm2",
        }),
        MockFlowFile(),
    )


def sample(engine, result, shots=1024, seed="11"):
    cls = _load_engine(ENGINES[engine])
    return cls().transform(
        MockContext(**{"Shots": str(shots), "Random Seed": seed}),
        result_to_flowfile(result),
    )


def top_outcome(sim_result):
    counts = json.loads(sim_result.contents.decode())
    return max(counts.items(), key=lambda kv: kv[1])[0]


@pytest.mark.parametrize("builder", sorted(BUILDERS))
@pytest.mark.parametrize("marked,iterations", CASES)
def test_builder_emits_the_shared_wire_format(builder, marked, iterations):
    res = build(builder, marked, iterations)
    assert res.relationship == "success", res.attributes.get("grover.error")
    assert res.attributes["circuit.format"] == "qasm2"
    body = res.contents.decode()
    # Cirq prefixes a provenance comment before the header, so look for the
    # version line rather than requiring it at byte zero.
    assert "OPENQASM 2" in body.split("qreg")[0]
    # Measurement belongs to the engine, so the builder must not add one.
    assert "measure" not in body


@pytest.mark.parametrize("builder", sorted(BUILDERS))
def test_builder_declares_its_identity(builder):
    res = build(builder, "110", 2)
    assert res.attributes["builder.framework"] == builder
    assert res.attributes["builder.component"] in ("QiskitGrover", "CirqGrover", "QrispGrover", "PennylaneGrover")
    assert res.attributes["circuit.bit_order"] == "canonical"
    for key in ("circuit.num_qubits", "circuit.depth", "circuit.gate_count"):
        assert res.attributes[key], f"{builder} did not publish {key}"


@pytest.mark.parametrize("builder", sorted(BUILDERS))
def test_emitted_qasm_parses_in_a_foreign_parser(builder):
    """A wire format only interoperates if another framework's parser accepts it."""
    from qiskit import qasm2

    res = build(builder, "110", 2)
    parsed = qasm2.loads(res.contents.decode(),
                         custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
    assert parsed.num_qubits >= 3


@pytest.mark.parametrize("builder", sorted(BUILDERS))
@pytest.mark.parametrize("engine", sorted(ENGINES))
def test_every_builder_runs_on_every_engine(builder, engine):
    """The matrix cell itself: any builder's circuit, any engine, right answer."""
    res = build(builder, "110", 2)
    sim = sample(engine, res)
    assert sim.relationship == "success", sim.attributes.get("sim.error")
    assert "sim.component" in sim.attributes
    assert top_outcome(sim) == "110"


@pytest.mark.parametrize("builder", sorted(BUILDERS))
@pytest.mark.parametrize("marked,iterations", [("110", 2), ("0111", 2)])
def test_non_palindromic_state_is_not_returned_reversed(builder, marked, iterations):
    """The bit-order regression.

    An unnormalised Qrisp export returns `011` when `110` is requested. A state
    that differs from its own reverse is the only kind that can catch this.
    """
    assert marked != marked[::-1]
    res = build(builder, marked, iterations)
    assert top_outcome(sample("aer", res)) == marked


def test_pennylane_declares_the_global_phase_it_dropped():
    """PennyLane emits an OpenQASM 3 `gphase` that OpenQASM 2.0 does not define.

    Removing it is sound for a directly sampled circuit, but it must be visible
    that it happened: the same primitive is behind a phase defect this project
    has already hit once.
    """
    res = build("pennylane", "110", 2)
    assert res.attributes["circuit.global_phase_dropped"] == "true"
    assert "gphase" not in res.contents.decode()


def test_builders_are_structurally_independent():
    """Four builders must be four implementations, not four copies of one.

    If two builders emitted the same circuit, agreement between them would be
    vacuous and the builder axis would carry no information.
    """
    import hashlib

    digests = {}
    for builder in BUILDERS:
        res = build(builder, "110", 2)
        digests[builder] = hashlib.sha256(res.contents).hexdigest()
    assert len(set(digests.values())) == len(digests), (
        "two builders emitted an identical circuit: " + repr(digests))


def test_marked_state_must_be_a_bitstring():
    for builder in ("qrisp", "pennylane"):
        cls = _load_builder(BUILDERS[builder])
        res = cls().transform(
            MockContext(**{"Marked State": "1x0", "Num Iterations": "1",
                           "Output Format": "qasm2"}),
            MockFlowFile(),
        )
        assert res.relationship == "failure"
        assert "bitstring" in res.attributes["grover.error"]
