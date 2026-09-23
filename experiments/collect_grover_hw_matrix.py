"""Collect Grover hardware-matrix results from the NiFi canvas into the archive.

The canvas does not archive results itself: NiFi writes one manifest per job
under its own home directory as ``<job>.json`` (plus a ``<job>.qijob.qpy``
handle for Quantum Inspire batches split across several provider jobs), and
polled provider documents are never written to disk at all -- they only ever
exist as FlowFile content in transit. `experiments/grover_hw_matrix_analysis.py`
reads archived ``**/*.manifest.json`` and ``**/*.polled.json`` pairs, joined by
``job_id``, from ``experiments/results/grover_hw_matrix/``.

This tool closes that gap for any job id (or NiFi ``batch_label``) without a
one-off script or a hard-coded job id (the previous approach,
``tools/monitor_iqm_job.py``):

  1. Copies the NiFi manifest(s) byte-for-byte into the archive.
  2. Fetches results FROM THE PROVIDER and produces the polled document by
     driving the real canvas poller processors -- ``QuantumIBMBatchPoller``,
     ``QuantumIQMBatchPoller``, ``QuantumInspireBatchPoller`` -- through their
     own ``transform()`` in-process, the same way ``experiments/archive_canvas_run.py``
     and the test suite do (via ``_harness`` / ``tests/conftest.py``'s
     ``nifiapi`` stubs). Counts merging, bit-order labelling, timing and
     calibration-set extraction are therefore never re-implemented here --
     this script only supplies the manifest FlowFile and the property values a
     canvas processor would have.
  3. Validates the written document against its manifest and fails loudly on
     any mismatch.

Usage::

    ../.venv/bin/python collect_grover_hw_matrix.py \\
        --provider ibm --job-ids daimhhr9k43c73ag8js0

    ../.venv/bin/python collect_grover_hw_matrix.py \\
        --provider quantum-inspire --batch-label run-1789266821137

    ../.venv/bin/python collect_grover_hw_matrix.py \\
        --provider iqm --job-ids 01a096a0-7e89-7f32-ac36-6dae3a09a75a --wait

Run from the ``experiments/`` directory (or with it on ``sys.path``) so the
sibling ``_harness`` module -- which installs the ``nifiapi`` stubs and puts
``nifi_extensions/`` on ``sys.path`` -- can be imported the same way
``grover_hw_matrix_analysis.py`` and ``archive_canvas_run.py`` already do.
"""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import _harness  # noqa: F401  (side effect: nifiapi stubs + extensions path)

from conftest import MockContext, MockFlowFile  # noqa: E402

from arithmetic_preflight import load_dotenv  # noqa: E402

#: Where the NiFi 2.9.0 canvas (the "grover_hw_matrix" flow's home) writes its
#: own manifests. Not a quanifi path -- NiFi's own working directory.
DEFAULT_NIFI_MANIFESTS_ROOT = Path(
    "experiments/results/grover_hw_matrix/manifests")

DEFAULT_ARCHIVE = Path("experiments/results/grover_hw_matrix")

PROVIDERS = ("ibm", "iqm", "quantum-inspire")


# ---------------------------------------------------------------------------
# Selecting job ids
# ---------------------------------------------------------------------------

def discover_job_ids_by_batch_label(nifi_manifests_dir, substr):
    """job_ids whose manifest carries ``substr`` in its ``batch_label``/``label``.

    Only Quantum Inspire manifests carry ``batch_label`` today (one NiFi batch
    that needed more circuits than the device's per-batch limit is split into
    several provider jobs, each a manifest "chunk" sharing one ``batch_label``).
    IBM/IQM manifests carry neither field -- select those by ``--job-ids``.
    """
    matches = []
    for path in sorted(Path(nifi_manifests_dir).glob("*.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        label_value = manifest.get("batch_label")
        if label_value is None:
            label_value = manifest.get("label")
        if label_value is None:
            continue
        if substr in str(label_value):
            matches.append(str(manifest.get("job_id") or path.stem))
    if not matches:
        raise SystemExit(
            "no manifest under {} carries a batch_label/label containing {!r}"
            .format(nifi_manifests_dir, substr))
    return matches


# ---------------------------------------------------------------------------
# Validation -- pure, no network, exercised directly by the test suite
# ---------------------------------------------------------------------------

def validate_polled(manifest, polled):
    """Every mismatch between a manifest and its polled document, or ``[]``.

    Checked, in the order the task requires:
      * entry count equals the manifest's entry count;
      * entry labels match the manifest labels, in order;
      * every entry has non-empty counts summing to the manifest's shots;
      * ``bit_order`` is present.
    """
    errors = []
    manifest_entries = manifest.get("entries") or []
    polled_entries = polled.get("entries") or []

    if len(polled_entries) != len(manifest_entries):
        errors.append(
            "entry count mismatch: manifest has {}, polled document has {}"
            .format(len(manifest_entries), len(polled_entries)))
    else:
        for index, (m_entry, p_entry) in enumerate(zip(manifest_entries, polled_entries)):
            if m_entry.get("label") != p_entry.get("label"):
                errors.append(
                    "label mismatch at index {}: manifest={!r} polled={!r}"
                    .format(index, m_entry.get("label"), p_entry.get("label")))

    shots = manifest.get("shots")
    for p_entry in polled_entries:
        label = p_entry.get("label")
        counts = p_entry.get("counts")
        if not counts:
            errors.append("entry {!r} has empty counts".format(label))
            continue
        total = sum(counts.values())
        if shots is not None and total != shots:
            errors.append(
                "entry {!r} counts sum to {}, manifest shots is {}"
                .format(label, total, shots))

    if not polled.get("bit_order"):
        errors.append("polled document is missing bit_order")

    return errors


def is_valid_existing(manifest, polled_path):
    """True iff ``polled_path`` already exists and validates against ``manifest``."""
    if not polled_path.exists():
        return False, ["no existing polled document"]
    try:
        polled = json.loads(polled_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, ["could not parse existing polled document: {}".format(exc)]
    errors = validate_polled(manifest, polled)
    return (not errors), errors


# ---------------------------------------------------------------------------
# Driving the real canvas pollers
# ---------------------------------------------------------------------------

def _poll_once(provider, job_id, manifest_bytes, qpy_path, poll_timeout, poll_interval):
    """One ``transform()`` call against the real poller, freshly authenticated.

    Every poller re-derives its provider client/session/token INSIDE
    ``transform()`` from the property values and the environment, so a fresh
    call -- not a fresh Python object -- is what re-authenticates; a new
    poller instance is still created each time for cleanliness, not because
    it is required for that.
    """
    props = {"Job ID": job_id}
    if poll_timeout is not None:
        props["Poll Timeout Seconds"] = str(poll_timeout)
    if poll_interval is not None:
        props["Poll Interval Seconds"] = str(poll_interval)

    if provider == "ibm":
        from QuantumIBMBatchPoller import QuantumIBMBatchPoller  # noqa: PLC0415
        # The "Instance" property's OWN default is the non-empty string
        # "test", which would otherwise beat _secret()'s environment fallback
        # and silently ignore the real IBM_QUANTUM_INSTANCE in .env.
        props["Instance"] = ""
        poller = QuantumIBMBatchPoller()
    elif provider == "iqm":
        from QuantumIQMBatchPoller import QuantumIQMBatchPoller  # noqa: PLC0415
        poller = QuantumIQMBatchPoller()
    elif provider == "quantum-inspire":
        from QuantumInspireBatchPoller import QuantumInspireBatchPoller  # noqa: PLC0415
        # The manifest's own 'provider_handle_path' is a path relative to
        # wherever NiFi's working directory was when it wrote the manifest,
        # which does not resolve here -- always point at the copy this tool
        # just archived instead.
        props["Job Handle Path"] = str(qpy_path.resolve())
        poller = QuantumInspireBatchPoller()
    else:
        raise ValueError("unknown provider {!r}".format(provider))

    context = MockContext(**props)
    flow_file = MockFlowFile(content=manifest_bytes)
    return poller.transform(context, flow_file)


def poll_until_terminal(provider, job_id, manifest_path, qpy_path,
                        poll_timeout, poll_interval, wait, wait_interval):
    """Poll (via the real poller) until a non-pending relationship comes back.

    Without ``--wait``, one non-terminal read is reported and returned as-is.
    With ``--wait``, this loops, sleeping ``wait_interval`` seconds between
    calls -- each call re-authenticates from scratch (see ``_poll_once``),
    which matters because Quantum Inspire and IQM tokens expire after an hour
    and a long-lived client/session would fail mid-wait with AuthorisationError.
    """
    manifest_bytes = manifest_path.read_bytes()
    while True:
        result = _poll_once(provider, job_id, manifest_bytes, qpy_path,
                            poll_timeout, poll_interval)
        if result.relationship != "pending":
            return result
        status = result.attributes.get("batch.status", "unknown")
        print("[{}] {}: pending (status={}){}".format(
            provider, job_id, status, " -- waiting" if wait else ""))
        if not wait:
            return result
        time.sleep(wait_interval)


# ---------------------------------------------------------------------------
# One job, start to finish
# ---------------------------------------------------------------------------

def collect_one(provider, job_id, nifi_manifests_dir, archive_dir,
                poll_timeout, poll_interval, wait, wait_interval, force):
    """Archive + fetch + validate one job. Returns ``(ok, summary_line)``.

    Nothing is written for a job that is not finished (without ``--wait``) or
    that fails validation -- an invalid or partial document left on disk would
    be indistinguishable from a good one to a later idempotent run.
    """
    manifest_src = Path(nifi_manifests_dir) / "{}.json".format(job_id)
    if not manifest_src.exists():
        return False, "[{}] {}: NiFi manifest not found at {}".format(
            provider, job_id, manifest_src)

    manifest_dst_dir = Path(archive_dir) / "manifests" / provider
    manifest_dst_dir.mkdir(parents=True, exist_ok=True)
    manifest_dst = manifest_dst_dir / "{}.manifest.json".format(job_id)
    shutil.copyfile(manifest_src, manifest_dst)

    qpy_dst = None
    if provider == "quantum-inspire":
        qpy_src = Path(nifi_manifests_dir) / "{}.qijob.qpy".format(job_id)
        if not qpy_src.exists():
            return False, "[{}] {}: QPY job handle not found at {}".format(
                provider, job_id, qpy_src)
        qpy_dst = manifest_dst_dir / "{}.qijob.qpy".format(job_id)
        shutil.copyfile(qpy_src, qpy_dst)

    manifest = json.loads(manifest_dst.read_text(encoding="utf-8"))

    polled_dir = Path(archive_dir) / "polled" / provider
    polled_dir.mkdir(parents=True, exist_ok=True)
    polled_path = polled_dir / "{}.polled.json".format(job_id)

    if not force:
        valid, errors = is_valid_existing(manifest, polled_path)
        if valid:
            entries = json.loads(polled_path.read_text(encoding="utf-8")).get("entries") or []
            return True, "[{}] {}: SKIPPED (already archived and valid, {} entries)".format(
                provider, job_id, len(entries))

    result = poll_until_terminal(provider, job_id, manifest_dst, qpy_dst,
                                 poll_timeout, poll_interval, wait, wait_interval)

    if result.relationship == "pending":
        status = result.attributes.get("batch.status", "unknown")
        return False, "[{}] {}: PENDING (status={}) -- not written".format(
            provider, job_id, status)
    if result.relationship != "success":
        status = result.attributes.get("batch.status", "")
        error = result.attributes.get("batch.error", "unknown error")
        return False, "[{}] {}: FAILED (status={}) {}".format(
            provider, job_id, status, error)

    polled_path.write_bytes(result.contents)
    polled = json.loads(result.contents.decode("utf-8"))
    errors = validate_polled(manifest, polled)
    if errors:
        polled_path.unlink(missing_ok=True)
        return False, "[{}] {}: VALIDATION FAILED: {}".format(
            provider, job_id, "; ".join(errors))

    entries = polled.get("entries") or []
    return True, "[{}] {}: OK -- {} entries, shots={}, bit_order={}".format(
        provider, job_id, len(entries), manifest.get("shots"), polled.get("bit_order"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", required=True, choices=PROVIDERS)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--job-ids", nargs="+", metavar="ID",
                          help="explicit provider job id(s) to collect")
    selector.add_argument("--batch-label", metavar="SUBSTR",
                          help="collect every manifest whose batch_label/label "
                               "contains this substring")
    parser.add_argument("--nifi-manifests", default=None,
                        help="directory of NiFi's own <job>.json manifests "
                             "(default: {}/<provider>)".format(DEFAULT_NIFI_MANIFESTS_ROOT))
    parser.add_argument("--archive", default=str(DEFAULT_ARCHIVE),
                        help="archive root (default: %(default)s)")
    parser.add_argument("--wait", action="store_true",
                        help="keep polling, re-authenticating every poll, "
                             "until every job is finished")
    parser.add_argument("--wait-interval", type=float, default=60.0,
                        help="seconds between re-authenticated polls in "
                             "--wait mode (default: %(default)s)")
    parser.add_argument("--poll-timeout", type=int, default=None,
                        help="override the poller's 'Poll Timeout Seconds' "
                             "property (default: the poller's own default)")
    parser.add_argument("--poll-interval", type=int, default=None,
                        help="override the poller's 'Poll Interval Seconds' "
                             "property (default: the poller's own default)")
    parser.add_argument("--force", action="store_true",
                        help="re-fetch even if a valid polled document "
                             "already exists")
    args = parser.parse_args(argv)

    load_dotenv()

    nifi_manifests_dir = Path(args.nifi_manifests) if args.nifi_manifests else (
        DEFAULT_NIFI_MANIFESTS_ROOT / args.provider)

    if args.job_ids:
        job_ids = list(args.job_ids)
    else:
        job_ids = discover_job_ids_by_batch_label(nifi_manifests_dir, args.batch_label)
        print("[{}] batch-label {!r} matched {} job(s): {}".format(
            args.provider, args.batch_label, len(job_ids), ", ".join(job_ids)))

    all_ok = True
    for job_id in job_ids:
        ok, message = collect_one(
            args.provider, job_id, nifi_manifests_dir, args.archive,
            args.poll_timeout, args.poll_interval, args.wait, args.wait_interval,
            args.force)
        print(message)
        all_ok = all_ok and ok

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
