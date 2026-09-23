import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


def _parse_geometry(spec):
    """Parse a geometry spec like ``H 0 0 0; H 0 0 0.735`` into the
    OpenFermion form ``[("H", (0.0, 0.0, 0.0)), ("H", (0.0, 0.0, 0.735))]``.

    Atoms are separated by ``;`` or newlines; within an atom, the element
    symbol and the three Cartesian coordinates (angstrom) are separated by
    whitespace and/or commas.
    """
    atoms = []
    for chunk in spec.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p for p in chunk.replace(",", " ").split() if p]
        if len(parts) != 4:
            raise ValueError(
                "each atom must be 'Symbol x y z', got {!r}".format(chunk))
        symbol = parts[0].capitalize()
        if not symbol.isalpha() or len(symbol) > 2:
            raise ValueError("invalid element symbol {!r}".format(parts[0]))
        try:
            coords = tuple(float(p) for p in parts[1:])
        except ValueError:
            raise ValueError(
                "coordinates must be numbers, got {!r}".format(chunk))
        atoms.append((symbol, coords))
    if not atoms:
        raise ValueError("geometry is empty")
    return atoms


def _formula(atoms):
    """Condensed formula in order of first appearance, e.g. H2, HLi, OH2."""
    counts = {}
    order = []
    for symbol, _ in atoms:
        if symbol not in counts:
            order.append(symbol)
        counts[symbol] = counts.get(symbol, 0) + 1
    return "".join(s + (str(counts[s]) if counts[s] > 1 else "") for s in order)


class MoleculeHamiltonian(FlowFileTransform):
    """
    Builds a molecular qubit Hamiltonian from a molecule geometry: pyscf
    computes the electronic-structure integrals, OpenFermion produces the
    fermionic Hamiltonian and maps it to qubits via the Jordan-Wigner
    transform.

    This is a *problem-definition* stage like the per-framework
    ``<Framework>Hamiltonian`` processors, but the problem is specified as
    chemistry (geometry, basis set) instead of an explicit Pauli sum. The
    output uses the same framework-neutral wire format
    (``hamiltonian.format = sparse_pauli_op_json``), so it can feed
    QiskitVQE, CirqVQE, or QrispVQE interchangeably:

        MoleculeHamiltonian -> <Framework>Ansatz -> <Framework>VQE -> QuanifiReport

    Geometry and basis accept NiFi Expression Language, so a fan-out of
    FlowFiles with e.g. a ``bond.distance`` attribute drives a molecular
    energy curve: set Molecule Geometry to ``H 0 0 0; H 0 0 ${bond.distance}``.

    When Compute Reference Energies is true, the Hartree-Fock and FCI (exact
    diagonalization) energies are attached as ``hamiltonian.hf_energy`` /
    ``hamiltonian.fci_energy``, giving downstream reports the classical
    reference to compare the VQE result against. FCI cost grows exponentially
    with system size — fine for H2/LiH/H2O-scale demos, disable for anything
    bigger.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Builds a molecular qubit Hamiltonian from a geometry such as "
            "'H 0 0 0; H 0 0 0.735' using pyscf + OpenFermion (Jordan-Wigner). "
            "Emits the framework-neutral operator JSON with hamiltonian.format = "
            "'sparse_pauli_op_json' so any *VQE solver can consume it, plus "
            "chemistry attributes (formula, bond distance, HF/FCI reference "
            "energies). No quantum execution happens here."
        )
        tags = ["quantum", "chemistry", "vqe", "hamiltonian", "molecule",
                "pyscf", "openfermion"]
        dependencies = ["pyscf>=2.3", "openfermion>=1.6", "openfermionpyscf>=0.5"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.geometry = PropertyDescriptor(
            name="Molecule Geometry",
            description=(
                "Atoms as 'Symbol x y z' (angstrom), separated by ';' or "
                "newlines, e.g. 'H 0 0 0; H 0 0 0.735'. Supports Expression "
                "Language: 'H 0 0 0; H 0 0 ${bond.distance}' lets a fan-out "
                "of FlowFiles sweep a bond length for an energy curve."
            ),
            required=True,
            default_value="H 0 0 0; H 0 0 0.735",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.basis = PropertyDescriptor(
            name="Basis Set",
            description="Gaussian basis set passed to pyscf, e.g. sto-3g, 6-31g.",
            required=True,
            default_value="sto-3g",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.charge = PropertyDescriptor(
            name="Charge",
            description="Total molecular charge (0 for a neutral molecule).",
            required=True,
            default_value="0",
            validators=[StandardValidators.INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.multiplicity = PropertyDescriptor(
            name="Multiplicity",
            description="Spin multiplicity 2S+1 (1 = singlet).",
            required=True,
            default_value="1",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.reference_energies = PropertyDescriptor(
            name="Compute Reference Energies",
            description=(
                "Also compute the classical Hartree-Fock and FCI (exact) "
                "energies and attach them as hamiltonian.hf_energy / "
                "hamiltonian.fci_energy for downstream comparison. FCI scales "
                "exponentially — keep enabled only for small molecules."
            ),
            required=True,
            default_value="true",
            allowable_values=["true", "false"],
        )
        self.descriptors = [
            self.geometry,
            self.basis,
            self.charge,
            self.multiplicity,
            self.reference_energies,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def _fail(self, flowFile, msg):
        self.logger.error("MoleculeHamiltonian: " + msg)
        return FlowFileTransformResult(
            relationship="failure",
            contents=bytes(flowFile.getContentsAsBytes() or b""),
            attributes={"hamiltonian.error": msg},
        )

    def transform(self, context, flowFile):
        from pauli_dsl import terms_to_wire

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        geometry_spec = get(self.geometry)
        basis = get(self.basis).strip()
        want_reference = get(self.reference_energies).lower() == "true"
        try:
            charge = int(get(self.charge))
            multiplicity = int(get(self.multiplicity))
        except (TypeError, ValueError) as exc:
            return self._fail(flowFile, "bad numeric property value: {}".format(exc))

        try:
            atoms = _parse_geometry(geometry_spec)
        except ValueError as exc:
            return self._fail(flowFile, "bad Molecule Geometry: {}".format(exc))

        from openfermion.chem import MolecularData
        from openfermion.transforms import get_fermion_operator, jordan_wigner
        from openfermion.utils import count_qubits
        from openfermionpyscf import run_pyscf

        try:
            molecule = MolecularData(atoms, basis, multiplicity, charge)
            molecule = run_pyscf(molecule, run_scf=True, run_fci=want_reference)
            qubit_op = jordan_wigner(
                get_fermion_operator(molecule.get_molecular_hamiltonian()))
            qubit_op.compress()
        except Exception as exc:
            return self._fail(flowFile, (
                "electronic structure failed for geometry '{}' basis '{}': {}"
                .format(geometry_spec, basis, exc)))

        # OpenFermion terms { ((idx, 'X'), ...): coeff } -> neutral
        # (pauli, indices, coeff) form. JW of a Hermitian operator has real
        # coefficients; imaginary parts are numerical noise.
        num_qubits = count_qubits(qubit_op)
        terms = []
        for factors, coeff in sorted(qubit_op.terms.items()):
            paulis = "".join(p for _, p in factors)
            indices = [int(i) for i, _ in factors]
            terms.append((paulis, indices, float(coeff.real)))

        wire = terms_to_wire(terms, num_qubits)
        content = json.dumps(wire, indent=2).encode("utf-8")

        formula = _formula(atoms)
        attributes = {
            "hamiltonian.format": "sparse_pauli_op_json",
            "hamiltonian.num_qubits": str(num_qubits),
            "hamiltonian.num_terms": str(len(terms)),
            "hamiltonian.framework": "agnostic",
            "hamiltonian.molecule": formula,
            "hamiltonian.geometry": "; ".join(
                "{} {:g} {:g} {:g}".format(s, *c) for s, c in atoms),
            "hamiltonian.basis": basis,
            "hamiltonian.charge": str(charge),
            "hamiltonian.multiplicity": str(multiplicity),
            "hamiltonian.nuclear_repulsion": "{:.10f}".format(
                float(molecule.nuclear_repulsion)),
        }
        if len(atoms) == 2:
            attributes["hamiltonian.bond_distance"] = "{:.6f}".format(math.dist(
                atoms[0][1], atoms[1][1]))
        if molecule.hf_energy is not None:
            attributes["hamiltonian.hf_energy"] = "{:.10f}".format(
                float(molecule.hf_energy))
        if want_reference and molecule.fci_energy is not None:
            attributes["hamiltonian.fci_energy"] = "{:.10f}".format(
                float(molecule.fci_energy))

        self.logger.warn(
            "MoleculeHamiltonian ({}, {}): {} qubits, {} terms".format(
                formula, basis, num_qubits, len(terms)))

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attributes,
        )
