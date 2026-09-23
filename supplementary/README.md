# Supplementary Quanifi flows for RQ1

Open **[index.html](index.html)** in a browser. The gallery is self-contained:
no server, account, NiFi installation, JavaScript framework, or CDN is needed.
The portfolio cost explorer uses optional local JavaScript; the complete cost
table and evidence remain readable without JavaScript.

## What was executed

These examples were configured and executed in an isolated Apache NiFi 2.11.0
instance on 14 September 2026. They use local Qrisp simulation, not hardware.

| Page | Executed cases | Evidence |
| --- | --- | --- |
| [Deutsch–Jozsa](deutsch-jozsa.html) | Balanced mask 101; constant-zero oracle | Expected outputs 101 and 000, each with probability 1 |
| [Bernstein–Vazirani](bernstein-vazirani.html) | Secret 1011 | Recovered 1011 with probability 1 |
| [Portfolio QAOA](portfolio-qaoa.html) | Three assets, budget 2, RX mixer, two layers | Exact optimum 011 sampled with probability 0.3095703125; feasible mass 0.9111328125 |
| [Maximum independent set](max-independent-set.html) | Exact eight-node graph from the pinned Classiq notebook; Qrisp adaptation | Maximum size 3; optimal-set probability 0.0205078125; independent-set probability 0.7568359375 |

All 17 processors passed NiFi validation. Each source emitted one FlowFile using
Run Once. Algorithm output was captured from its success queue, including
algorithm attributes, before PutFile consumed it. All five files written by
PutFile matched the captured content byte-for-byte. All processors were then
stopped. Screenshots were taken from that stopped, configured canvas; a red
square indicates stopped state. QrispQAOA logs an optimisation summary at WARN
level, which may appear as a bulletin despite a successful run.

`tools/verify_evidence.py` independently checks the saved results. For portfolio QAOA it
evaluates the classical cost of all eight possible portfolios, then checks
the best sampled bitstring, the exact minimum, and the mean objective reported
by the processor. Finding an optimal sample does not mean that every sample
is optimal. For MIS it checks all 256 vertex subsets and enumerates all six
maximum independent sets of size three. These are small demonstrations, not performance benchmarks.

The QAOA example uses an RX mixer and a budget penalty. It does not claim an
XY mixer with feasible-state initialisation or a Classiq execution. The MIS example adapts the Classiq notebook’s exact graph
and problem using Qrisp mean-cost QAOA instead of Classiq CVaR-QAOA. Its graph,
source revision, and original settings are in `evidence/classiq-reference.json`. The older
introductory portfolio guide describes a different configuration.

## Files

- `flows/specifications.json`: input cases and configured algorithm properties.
- `flows/*.json`: actual NiFi process-group exports, with their sink directory
  changed to the portable relative path `./supplementary-results`.
- `processors/`: snapshots of the five executed Quanifi processors and their
  shared Hamiltonian helper. These copies are byte-identical to the source
  used by the isolated NiFi instance.
- `evidence/`: recorded FlowFile outputs, algorithm attributes, validation,
  component IDs, dependency versions, hashes, and independent checks.
- `assets/*.png`: real NiFi screenshots, not reconstructed mockups.
- `tools/`: API deployment, execution, capture, verification, and HTML generation.

The parameter values shown in HTML come from the creation specification.
The downloaded NiFi exports contain the processor configurations and connections.
The original paper's Grover results and processor snapshot are not changed by
these additional examples.

## Check the recorded evidence without NiFi

From the replication repository root, using Python 3.11 or later:

```sh
python3 supplementary/tools/verify_evidence.py
python3 supplementary/tools/build_html.py
```

The optional `python3 supplementary/tools/check_html.py` command checks all five
HTML pages in Chrome at desktop and phone widths with external HTTP blocked,
and exercises both interactive examples. It requires Chrome but no Python dependencies.

No third-party Python packages are needed for those commands. The recorded
environment is in [evidence/environment.json](evidence/environment.json),
including the source hashes and the installed versions of all quantum dependencies.
The import checks in `evidence/import-checks.json` verify that the portable exports
can be reimported, validate, and remain stopped. In the export format,
`scheduledState: ENABLED` means enabled for scheduling, not actively running.

## Import and run in NiFi

This path requires Apache NiFi 2.11.0, Java 21, Python 3.11, and network access
for initial installation of the Python processor dependencies. It is optional
for reviewing the evidence. See the official [NiFi user guide](https://nifi.apache.org/nifi-docs/user-guide.html)
and [administration guide](https://nifi.apache.org/nifi-docs/administration-guide.html).

1. Use a separate local NiFi installation. Before starting it, configure an
   additional Python extension source directory in `conf/nifi.properties`:

   ```properties
   nifi.python.command=/absolute/path/to/python3.11
   nifi.python.extensions.source.directory.supplementary=/absolute/path/to/supplementary/processors
   ```

   Do not also load another copy of these processor classes from a second
   extension directory. NiFi creates per-processor dependency environments.
   The recorded environment used Qrisp 0.9.5 and Qiskit 2.4.2. The processor
   declarations allow some dependency ranges; consult the environment record
   when reproducing an exact software setup.

2. Start NiFi, sign in locally, and import one of the process-group JSON files
   from `flows/` using the canvas's process-group upload/import action.
3. Wait until all Python processors validate. Initial dependency downloads can
   take several minutes. Confirm the configured properties against the HTML page.
4. Set each PutFile output directory to a writable location. The export defaults
   to `./supplementary-results`, relative to the NiFi working directory.
5. Start PutFile and the algorithm/encoder processors. Keep GenerateFlowFile
   stopped, then use **Run Once** on each source. It emits one input per click.
6. Read the saved JSON files. For a failed algorithm, inspect the connected
   failure queue and the processor's bulletin. Stop the downstream processors
   when finished.

The supplied exports do not require controller services, cloud credentials,
or access to a quantum device. The MIS source contains the explicit JSON edge list; the other sources contain
an empty JSON object, with problem inputs configured as properties. No setup steps need the
original author's filesystem paths.

## Create fresh flows and capture a new run through the API

Use an authenticated NiFi JWT stored in a local file outside this repository.
The scripts never put credentials into the artifact. Adjust the URL for your
installation. `--insecure` is only for a local self-signed development certificate;
omit it when the server certificate is trusted.

```sh
python3 supplementary/tools/nifi_examples.py create \
  --url https://localhost:8450 --token-file /path/to/private-token \
  --insecure --run-dir /tmp/quanifi-review-run \
  --output-dir /tmp/quanifi-review-results

python3 supplementary/tools/nifi_examples.py run \
  --url https://localhost:8450 --token-file /path/to/private-token \
  --insecure --run-dir /tmp/quanifi-review-run
```

The separate run directory preserves this artifact's recorded outputs. `create`
refuses to overwrite an existing deployment record. It creates stopped process
groups; `run` operates only on their recorded IDs, validates the processors,
uses one-shot sources, saves queue evidence, drains successful outputs to PutFile,
and stops the processors. Inspect a failed run before retrying; the script does
not silently erase failure queues.

QAOA's random seed is best-effort, so new distributions can differ. Evaluate
new outputs against the objective, not byte-for-byte against the archived
probabilities. The reported mean cost is an expectation over sampled outputs;
it is different from the best sample's cost.

## Recapture the documented canvases

With the original recorded groups still present and stopped:

```sh
NIFI_URL=https://localhost:8450 \
python3 supplementary/tools/nifi_screenshot.py --token-file /path/to/private-token
```

The capture utility uses Google Chrome at its standard macOS installation path
and an isolated temporary browser profile on an automatically selected local debugging port. It verifies
that NiFi processor elements rendered before saving an image. It does not
change processor configuration or run state. Edit the browser path for another
operating system.

## Scope of the RQ1 claim

The examples demonstrate component composition for two textbook algorithms and
two hybrid optimisation tasks. Circuit construction and simulation are encapsulated
inside the algorithm processors. In the QAOA flow the Hamiltonian encoder is a
separate component, while the classical/quantum optimisation loop is internal to
QrispQAOA. This evidence does not measure ease of use, development time, quantum
advantage, or correctness for every possible input.
