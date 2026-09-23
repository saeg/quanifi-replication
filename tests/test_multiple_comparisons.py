"""Tests for the shared multiple-comparison-correction helper.

multiple_comparisons.py replaces two functions that used to live independently
in experiments/version_matrix_study.py (holm_bonferroni) and
experiments/mutation_study.py (benjamini_hochberg). The reference
implementations below are pasted verbatim from those scripts (copied before
the functions were deleted there) as an equivalence guard: if the shared
module and these references ever disagree, something drifted.
"""
import random

import pytest

from multiple_comparisons import CORRECTIONS, benjamini_hochberg, holm, reject, uncorrected


# ---------------------------------------------------------------------------
# Reference implementations, pasted verbatim from the scripts they used to
# live in, BEFORE those functions were deleted and replaced with an import
# from this shared module.
# ---------------------------------------------------------------------------

def _holm_reference(pvals, alpha):
    """Holm step-down; returns reject decisions in input order."""
    order = sorted(range(len(pvals)), key=lambda i: (pvals[i] is None, pvals[i]))
    m = sum(1 for p in pvals if p is not None)
    reject = [False] * len(pvals)
    rank = 0
    for i in order:
        if pvals[i] is None:
            continue
        if pvals[i] <= alpha / (m - rank):
            reject[i] = True
            rank += 1
        else:
            break
    return reject


def _bh_reference(pvals, alpha):
    """BH step-up: reject-decisions in input order (None p-values never reject)."""
    idx = [i for i, p in enumerate(pvals) if p is not None]
    m = len(idx)
    reject = [False] * len(pvals)
    if not m:
        return reject
    ordered = sorted(idx, key=lambda i: pvals[i])
    cutoff = -1
    for rank, i in enumerate(ordered, start=1):
        if pvals[i] <= alpha * rank / m:
            cutoff = rank
    for rank, i in enumerate(ordered, start=1):
        if rank <= cutoff:
            reject[i] = True
    return reject


# ---------------------------------------------------------------------------

class TestHolm:
    def test_known_vector(self):
        assert holm([0.2, 0.036, 0.001, 0.03], 0.05) == [False, False, True, False]

    def test_none_pvalues_never_reject(self):
        assert holm([None, 0.01, None], 0.05) == [False, True, False]

    def test_all_none(self):
        assert holm([None, None], 0.05) == [False, False]

    def test_empty(self):
        assert holm([], 0.05) == []

    def test_uses_le_not_lt(self):
        """Holm's boundary is <=, matching the paper's numbers -- do not
        unify this with uncorrected()'s strict <."""
        assert holm([0.025, 0.05], 0.05) == [True, True]
        assert uncorrected([0.025, 0.05], 0.05) == [True, False]


class TestBenjaminiHochberg:
    def test_known_vector(self):
        assert benjamini_hochberg([0.2, 0.036, 0.001, 0.03], 0.05) == [False, True, True, True]

    def test_none_pvalues_never_reject(self):
        assert benjamini_hochberg([None, 0.01, None], 0.05) == [False, True, False]

    def test_empty(self):
        assert benjamini_hochberg([], 0.05) == []


class TestReject:
    def test_none_matches_uncorrected(self):
        pvals = [0.2, 0.036, 0.001, 0.03]
        assert reject(pvals, 0.05, "none") == uncorrected(pvals, 0.05)

    def test_none_method_defaults_to_none_correction(self):
        pvals = [0.2, 0.01]
        assert reject(pvals, 0.05, None) == uncorrected(pvals, 0.05)

    def test_unknown_method_raises(self):
        with pytest.raises(ValueError):
            reject([0.1, 0.2], 0.05, "bogus")


class TestEquivalenceGuard:
    """500 seeded random p-value vectors, shared module vs. pasted-verbatim
    reference implementations. Any drift between multiple_comparisons.py and
    the reference bodies above must fail this test."""

    def test_holm_matches_reference(self):
        rng = random.Random(0)
        for _ in range(500):
            n = rng.randint(1, 40)
            pool = [round(rng.random(), 4) for _ in range(max(1, n // 4))]
            pvals = []
            for _ in range(n):
                if rng.random() < 0.2:
                    pvals.append(None)
                else:
                    pvals.append(rng.choice(pool))
            alpha = rng.choice([0.01, 0.05, 0.1])
            assert holm(pvals, alpha) == _holm_reference(pvals, alpha)

    def test_bh_matches_reference(self):
        rng = random.Random(0)
        for _ in range(500):
            n = rng.randint(1, 40)
            pool = [round(rng.random(), 4) for _ in range(max(1, n // 4))]
            pvals = []
            for _ in range(n):
                if rng.random() < 0.2:
                    pvals.append(None)
                else:
                    pvals.append(rng.choice(pool))
            alpha = rng.choice([0.01, 0.05, 0.1])
            assert benjamini_hochberg(pvals, alpha) == _bh_reference(pvals, alpha)


def test_corrections_tuple():
    assert CORRECTIONS == ("none", "holm", "benjamini-hochberg")
