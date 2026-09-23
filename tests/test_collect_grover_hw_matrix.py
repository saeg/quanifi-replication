"""Validation logic for the Grover hardware-matrix collector.

No network: these exercise ``validate_polled`` / ``is_valid_existing`` /
``collect_one``'s idempotent-skip path directly against stubbed manifest and
polled documents, never against a real provider or the NiFi canvas.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "experiments"))

import collect_grover_hw_matrix as collector  # noqa: E402


def _manifest(labels, shots=1024):
    return {
        "job_id": "job-1", "device": "garnet", "shots": shots,
        "entries": [{"label": label, "kind": "control"} for label in labels],
    }


def _polled(labels, shots=1024, bit_order="q0_left", counts_per_label=None):
    counts_per_label = counts_per_label or {}
    entries = []
    for label in labels:
        counts = counts_per_label.get(label, {"00": shots})
        entries.append({"label": label, "kind": "control", "counts": counts})
    doc = {"job_id": "job-1", "entries": entries}
    if bit_order is not None:
        doc["bit_order"] = bit_order
    return doc


LABELS = ["Cirq@grover-hw-000", "Qiskit@grover-hw-001", "PennyLane@grover-hw-002"]


class TestValidatePolled:

    def test_a_matching_document_is_valid(self):
        manifest = _manifest(LABELS)
        polled = _polled(LABELS)
        assert collector.validate_polled(manifest, polled) == []

    def test_entry_count_mismatch_fails(self):
        manifest = _manifest(LABELS)
        polled = _polled(LABELS[:-1])
        errors = collector.validate_polled(manifest, polled)
        assert any("entry count mismatch" in e for e in errors)

    def test_label_order_mismatch_fails(self):
        manifest = _manifest(LABELS)
        swapped = [LABELS[1], LABELS[0], LABELS[2]]
        polled = _polled(swapped)
        errors = collector.validate_polled(manifest, polled)
        assert any("label mismatch" in e for e in errors)

    def test_counts_not_summing_to_shots_fails(self):
        manifest = _manifest(LABELS, shots=1024)
        polled = _polled(LABELS, counts_per_label={LABELS[0]: {"00": 100, "01": 5}})
        errors = collector.validate_polled(manifest, polled)
        assert any("counts sum to" in e for e in errors)

    def test_empty_counts_fails(self):
        manifest = _manifest(LABELS)
        polled = _polled(LABELS, counts_per_label={LABELS[1]: {}})
        errors = collector.validate_polled(manifest, polled)
        assert any("empty counts" in e for e in errors)

    def test_missing_bit_order_fails(self):
        manifest = _manifest(LABELS)
        polled = _polled(LABELS, bit_order=None)
        errors = collector.validate_polled(manifest, polled)
        assert any("bit_order" in e for e in errors)

    def test_multiple_problems_are_all_reported(self):
        manifest = _manifest(LABELS, shots=1024)
        polled = _polled(LABELS[:-1], bit_order=None,
                         counts_per_label={LABELS[0]: {"00": 1}})
        errors = collector.validate_polled(manifest, polled)
        assert any("entry count mismatch" in e for e in errors)
        assert any("counts sum to" in e for e in errors)
        assert any("bit_order" in e for e in errors)


class TestIsValidExisting:

    def test_missing_file_is_not_valid(self, tmp_path):
        manifest = _manifest(LABELS)
        polled_path = tmp_path / "job-1.polled.json"
        valid, errors = collector.is_valid_existing(manifest, polled_path)
        assert valid is False
        assert errors

    def test_valid_existing_file_is_valid(self, tmp_path):
        manifest = _manifest(LABELS)
        polled_path = tmp_path / "job-1.polled.json"
        polled_path.write_text(json.dumps(_polled(LABELS)))
        valid, errors = collector.is_valid_existing(manifest, polled_path)
        assert valid is True
        assert errors == []

    def test_invalid_existing_file_is_not_valid(self, tmp_path):
        manifest = _manifest(LABELS)
        polled_path = tmp_path / "job-1.polled.json"
        polled_path.write_text(json.dumps(_polled(LABELS[:-1])))
        valid, errors = collector.is_valid_existing(manifest, polled_path)
        assert valid is False
        assert any("entry count mismatch" in e for e in errors)

    def test_unparseable_existing_file_is_not_valid(self, tmp_path):
        manifest = _manifest(LABELS)
        polled_path = tmp_path / "job-1.polled.json"
        polled_path.write_text("{not json")
        valid, errors = collector.is_valid_existing(manifest, polled_path)
        assert valid is False
        assert errors


class TestCollectOneSkipsWithoutForce:
    """`collect_one` must skip re-fetching (no network call attempted) when a
    valid polled document is already archived, and only when --force is NOT
    passed. Its provider-fetch path is monkeypatched out so a stray call
    would be a test failure -- not silently absorbed."""

    def _make_manifest_and_archive(self, tmp_path, nifi_dir, provider="ibm",
                                    job_id="job-1", labels=LABELS, shots=1024):
        nifi_dir.mkdir(parents=True, exist_ok=True)
        manifest = _manifest(labels, shots=shots)
        manifest["job_id"] = job_id
        (nifi_dir / "{}.json".format(job_id)).write_text(json.dumps(manifest))

        archive_dir = tmp_path / "archive"
        manifest_dst_dir = archive_dir / "manifests" / provider
        manifest_dst_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dst_dir / "{}.manifest.json".format(job_id)).write_text(
            json.dumps(manifest))

        polled_dir = archive_dir / "polled" / provider
        polled_dir.mkdir(parents=True, exist_ok=True)
        (polled_dir / "{}.polled.json".format(job_id)).write_text(
            json.dumps(_polled(labels, shots=shots)))
        return archive_dir

    def test_skips_when_already_valid(self, tmp_path, monkeypatch):
        nifi_dir = tmp_path / "nifi"
        archive_dir = self._make_manifest_and_archive(tmp_path, nifi_dir)

        def _boom(*args, **kwargs):
            raise AssertionError("poll_until_terminal must not be called when "
                                 "a valid polled document already exists")
        monkeypatch.setattr(collector, "poll_until_terminal", _boom)

        ok, message = collector.collect_one(
            "ibm", "job-1", nifi_dir, archive_dir,
            poll_timeout=None, poll_interval=None, wait=False,
            wait_interval=60.0, force=False)
        assert ok is True
        assert "SKIPPED" in message

    def test_force_refetches_even_when_valid(self, tmp_path, monkeypatch):
        nifi_dir = tmp_path / "nifi"
        archive_dir = self._make_manifest_and_archive(tmp_path, nifi_dir)

        calls = []

        class _Result:
            relationship = "success"
            attributes = {}
            contents = json.dumps(_polled(LABELS)).encode("utf-8")

        def _fake_poll(*args, **kwargs):
            calls.append(args)
            return _Result()
        monkeypatch.setattr(collector, "poll_until_terminal", _fake_poll)

        ok, message = collector.collect_one(
            "ibm", "job-1", nifi_dir, archive_dir,
            poll_timeout=None, poll_interval=None, wait=False,
            wait_interval=60.0, force=True)
        assert ok is True
        assert len(calls) == 1
        assert "OK" in message

    def test_invalid_existing_document_triggers_refetch(self, tmp_path, monkeypatch):
        nifi_dir = tmp_path / "nifi"
        archive_dir = self._make_manifest_and_archive(tmp_path, nifi_dir)
        # Corrupt the archived polled document so it no longer validates.
        polled_path = archive_dir / "polled" / "ibm" / "job-1.polled.json"
        polled_path.write_text(json.dumps(_polled(LABELS[:-1])))

        calls = []

        class _Result:
            relationship = "success"
            attributes = {}
            contents = json.dumps(_polled(LABELS)).encode("utf-8")

        def _fake_poll(*args, **kwargs):
            calls.append(args)
            return _Result()
        monkeypatch.setattr(collector, "poll_until_terminal", _fake_poll)

        ok, message = collector.collect_one(
            "ibm", "job-1", nifi_dir, archive_dir,
            poll_timeout=None, poll_interval=None, wait=False,
            wait_interval=60.0, force=False)
        assert ok is True
        assert len(calls) == 1
        assert "OK" in message


class TestCollectOneMissingManifest:

    def test_missing_nifi_manifest_fails_without_writing(self, tmp_path):
        nifi_dir = tmp_path / "nifi"
        nifi_dir.mkdir(parents=True, exist_ok=True)
        archive_dir = tmp_path / "archive"
        ok, message = collector.collect_one(
            "ibm", "no-such-job", nifi_dir, archive_dir,
            poll_timeout=None, poll_interval=None, wait=False,
            wait_interval=60.0, force=False)
        assert ok is False
        assert "not found" in message
        assert not (archive_dir / "manifests").exists()


class TestDiscoverJobIdsByBatchLabel:

    def test_matches_batch_label_substring(self, tmp_path):
        (tmp_path / "835593.json").write_text(json.dumps(
            {"job_id": "835593", "batch_label": "run-1789266821137"}))
        (tmp_path / "835019.json").write_text(json.dumps(
            {"job_id": "835019", "batch_label": "run-1789223493974"}))
        matches = collector.discover_job_ids_by_batch_label(tmp_path, "1789266821137")
        assert matches == ["835593"]

    def test_no_match_raises(self, tmp_path):
        (tmp_path / "835019.json").write_text(json.dumps(
            {"job_id": "835019", "batch_label": "run-1789223493974"}))
        try:
            collector.discover_job_ids_by_batch_label(tmp_path, "nope")
            assert False, "expected SystemExit"
        except SystemExit:
            pass

    def test_manifests_without_batch_label_are_ignored(self, tmp_path):
        (tmp_path / "daimhhr9k43c73ag8js0.json").write_text(json.dumps(
            {"job_id": "daimhhr9k43c73ag8js0", "device": "ibm_kingston"}))
        try:
            collector.discover_job_ids_by_batch_label(tmp_path, "anything")
            assert False, "expected SystemExit"
        except SystemExit:
            pass
