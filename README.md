# Quanifi replication package

Experiment code, frozen processor fork, tests, figures, and archived evidence
for the Quanifi N×M-version execution study.

It is intentionally separate from the reusable Quanifi framework, which is
included here as the `framework/` submodule, and from the Adder, DDT-mining,
Generation-2, pilot, and exploratory experiments.

## Layout

- `experiments/`: study scripts and archived results
- `nifi_extensions_matrix/`: processor snapshot used by the version matrix
- `framework/`: the Quanifi framework, as a submodule
- `tests/`: focused checks for the replication code
- `figures/`: editable and rendered version-matrix diagrams
- `supplementary/`: offline HTML gallery of four configured and executed RQ1
  examples, including the local Qrisp adaptation of the Classiq independent-set
  reference graph; actual NiFi screenshots, flow exports, outputs, and checks

Clone with the framework in place:

```sh
git clone --recurse-submodules https://github.com/saeg/quanifi-replication.git
```

Open [`supplementary/index.html`](supplementary/index.html) in a browser to inspect
the additional flows without installing NiFi. Reproduction instructions and
the distinction between Classiq's reference and the local adaptation are in
[`supplementary/README.md`](supplementary/README.md).

No credentials, local NiFi state, internal AI plans, OpenSpec artifacts, or
ad-hoc explanatory reports belong in this repository.

## Defect evidence

The per-job counts behind the three defects reported in the paper:

- [`open_quantum_route_comparison/`](experiments/results/open_quantum_route_comparison/README.md):
  adders, Toffoli and Grover sent to IQM `garnet` directly and through Open
  Quantum on 17 September (Table III and the Grover route comparison), and the
  same files again on 25 September
- [`open_quantum_cepheus_discovery/`](experiments/results/open_quantum_cepheus_discovery/README.md):
  the Rigetti Cepheus adder runs and the Toffoli probe in which the Open Quantum
  defect was first seen, and their 25 September rerun
- [`quantum_inspire_rx/`](experiments/results/quantum_inspire_rx/README.md):
  the Tuna-17 Rx sign probes (20 and 25 September), the native-gate and Cirq
  follow-up probes, the 12–13 September diagnostic batches recovered from the
  Quantum Inspire API, and the adapter's advertised gate list

Recompute every table from the archived counts, without provider access:

```sh
.venv/bin/python experiments/defect_evidence_tables.py
```

It writes `experiments/results/defect_evidence_tables.md` and one CSV per table.

## Hardware mutation analysis

The [hardware mutation results](experiments/results/grover_hw_mutation_analysis/README.md)
report control-versus-mutant detection and comparisons with the other builders
from the archived IBM and IQM jobs. The analysis keeps each job separate and
records the source file hashes. Reproduce it without provider access:

```sh
.venv/bin/python experiments/grover_hw_mutation_analysis.py
```

## Tests

```sh
just test
```
