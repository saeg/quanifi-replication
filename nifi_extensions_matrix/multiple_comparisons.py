"""Shared multiple-comparison correction for QuantumDistributionOracle and the
experiment scripts (experiments/version_matrix_study.py,
experiments/mutation_study.py).

With K branches sharing a slot, the oracle (or a study script) runs one
pairwise test per pair: K(K-1)/2 tests. An uncorrected significance level
applied to every one of those tests makes a false "diverge" near-certain once
K is more than a handful of branches (20 branches -> 190 tests). The
functions here correct for that multiplicity across the pairs of one slot.

A ``None`` p-value means the pair had no raw counts to test (probabilities
only, no chi-squared gate available). ``None`` entries are excluded from the
correction's ``m`` (the number of real tests) and never reject, in every
function below -- they neither inflate the family size nor get flagged as
significant by omission.
"""

CORRECTIONS = ("none", "holm", "benjamini-hochberg")


def uncorrected(pvals, alpha):
    """Today's oracle rule: each pair tested independently at alpha (strict <)."""
    return [p is not None and p < alpha for p in pvals]


def holm(pvals, alpha):
    """Holm-Bonferroni step-down; returns reject decisions in input order.

    Uses <=, not <, matching the reference implementation this was copied
    from (experiments/version_matrix_study.py) -- the paper's numbers depend
    on that boundary behaviour, so it is preserved verbatim rather than
    unified with ``uncorrected``'s strict <.
    """
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


def benjamini_hochberg(pvals, alpha):
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


def normalize_method(value):
    """Normalise a raw property/argument value to a correction name.

    None or blank becomes "none" (legacy behaviour, no validation otherwise
    performed here -- callers that need a valid name should check membership
    in CORRECTIONS themselves, or call reject() which does).
    """
    if value is None:
        return "none"
    value = value.strip()
    return "none" if not value else value.lower()


def reject(pvals, alpha, method):
    """Dispatch to the correction named by ``method``.

    ``method`` is normalised the same way as ``normalize_method`` (None or
    blank means "none"). Raises ValueError for anything not in CORRECTIONS.
    """
    method = (method or "none").strip().lower()
    if method == "none":
        return uncorrected(pvals, alpha)
    if method == "holm":
        return holm(pvals, alpha)
    if method == "benjamini-hochberg":
        return benjamini_hochberg(pvals, alpha)
    raise ValueError(f"unknown correction {method!r}; expected one of {CORRECTIONS}")
