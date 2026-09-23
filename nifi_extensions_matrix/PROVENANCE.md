# Provenance

This directory is a full copy of `nifi_extensions/` taken for the
**builder x engine matrix** and **fault localisation** studies.

- Source: `nifi_extensions/`
- Source commit: `ca6876e572f1c3b37658969fc56c0e761b969ad2`
- Copied: 2026-09-05

It exists so that the NiFi 2.10.0 instance can load new and modified
processors while the deployed NiFi 2.9.0 instance keeps loading
`nifi_extensions/` unchanged. The two instances register different extension
directories, so no processor class name can collide between them.

**This fork is disposable.** It is retired when the study is finished. Genuine
bug fixes discovered here are backported to `nifi_extensions/` as additive
patches and listed in `DIVERGED.md`.

Every file here is byte-identical to its counterpart in `nifi_extensions/`
**as it stands in the working tree**, except those declared in `DIVERGED.md`.
`tests/test_extension_fork_drift.py` enforces that against the working tree, not
against the commit.

That distinction matters here: at copy time the following files were modified
but uncommitted (in-progress hardware work), so the copy captured their
working-tree state rather than their state at the commit above:

- `nifi_extensions/QuantumIBMBatchSubmitter.py`
- `nifi_extensions/batch_prep.py`
