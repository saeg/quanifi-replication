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
