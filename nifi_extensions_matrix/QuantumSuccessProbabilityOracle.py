import json
import math
import os
import sys

# NiFi loads each processor in its own module context without the extensions
# directory on sys.path, so the sibling-module import below fails without this.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder

import arithmetic_spec as aspec


# ---------------------------------------------------------------------------
# Statistics, in pure Python
# ---------------------------------------------------------------------------
# NiFi installs a processor's dependencies per processor, so this module stays
# free of scipy/numpy. Everything below is standard and testable against
# published reference values.

def _norm_sf(z):
    """Upper tail of the standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _norm_ppf(p):
    """Inverse standard normal CDF (Acklam's rational approximation).

    Accurate to about 1.15e-9 over the open unit interval, which is far beyond
    what a shot-count decision needs.
    """
    if not 0.0 < p < 1.0:
        raise ValueError("probability must be in (0, 1), got %r" % (p,))

    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)

    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q / \
           (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1)


def wilson_interval(successes, n, confidence=0.95):
    """Wilson score interval for a binomial proportion.

    Preferred to the Wald interval because success probabilities on hardware
    sit near the boundary, where Wald produces intervals that leave the unit
    interval and undercover badly.
    """
    if n <= 0:
        return (0.0, 1.0)
    z = _norm_ppf(1 - (1 - confidence) / 2.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def two_proportion_test(s1, n1, s2, n2, alternative="two-sided"):
    """Pooled z-test for equality of two independent proportions.

    Returns ``(z, p_value)``, or ``(None, None)`` when either arm is empty.

    ``alternative`` selects the hypothesis actually being tested:

      ``two-sided``  (default, and what this function always did) -- any
                     difference counts.
      ``worse``      one-sided: only arm 1 outperforming arm 2 counts, i.e.
                     the mutant doing *worse* than its control. This is the
                     alternative a mutation-kill decision actually has in mind.

    The distinction matters for how a result is described, not only for its
    arithmetic. The kill rule downstream used to run this two-sided test at
    alpha and then discard the wrong-direction half, which is a
    direction-filtered two-sided test -- effectively one-sided at alpha/2, and
    conservative -- while the surrounding prose called it one-sided. Passing
    ``alternative="worse"`` makes the code and the description agree.
    """
    if n1 <= 0 or n2 <= 0:
        return (None, None)
    p1, p2 = s1 / n1, s2 / n2
    pooled = (s1 + s2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0.0:
        # both arms identical and degenerate (all successes or all failures)
        return (0.0, 1.0)
    z = (p1 - p2) / se
    if alternative == "worse":
        # Upper tail only: arm 1 (control) exceeding arm 2 (mutant).
        return (z, _norm_sf(z))
    if alternative != "two-sided":
        raise ValueError("unknown alternative %r" % (alternative,))
    return (z, 2.0 * _norm_sf(abs(z)))


def required_shots(p_ref, min_diff, alpha=0.05, power=0.8):
    """Shots per arm needed to detect ``min_diff`` at ``alpha`` and ``power``.

    The standard two-proportion normal approximation:

        n = 2 * p_bar * (1 - p_bar) * (z_{alpha/2} + z_{power})^2 / diff^2

    where ``p_bar`` is the mean of the two proportions being compared, taken
    here as ``p_ref`` and ``p_ref - min_diff``.

    Worked example. Suppose a control run on hardware succeeds about 90% of the
    time, and a mutant is worth catching if it drops that to 80% or below. Then
    ``p_ref = 0.90``, ``min_diff = 0.10``, ``p_bar = 0.85``, and at the
    conventional ``alpha = 0.05`` two-sided and ``power = 0.80``::

        z_{alpha/2} = 1.95996,  z_{power} = 0.84162
        n = 2 * 0.85 * 0.15 * (1.95996 + 0.84162)^2 / 0.10^2
          = 2 * 0.1275 * 7.849 / 0.01
          = 200.1  ->  201 shots per arm

    The result is a ceiling, not a rounding: 200 shots would leave the study
    fractionally under 80% power, so the honest number is 201.

    Two consequences worth internalising before choosing a shot budget. The
    dependence on ``min_diff`` is quadratic, so halving the effect you want to
    catch quadruples the cost. And the dependence on ``p_ref`` is weak, so a
    noisier device does not change the budget nearly as much as a stricter
    detection target does.

    This is the *per-arm* count. A paired comparison of a control against a
    mutant needs it twice.
    """
    if not 0.0 < min_diff <= 1.0:
        raise ValueError("min_diff must be in (0, 1]")
    p2 = max(0.0, min(1.0, p_ref - min_diff))
    p_bar = (p_ref + p2) / 2.0
    z_a = _norm_ppf(1 - alpha / 2.0)
    z_b = _norm_ppf(power)
    return math.ceil(2 * p_bar * (1 - p_bar) * (z_a + z_b) ** 2 / min_diff ** 2)


def detectable_difference(n, p_ref, alpha=0.05, power=0.8):
    """Smallest difference in success probability detectable with ``n`` shots."""
    if n <= 0:
        return 1.0
    lo, hi = 1e-6, 1.0
    for _ in range(80):                      # bisection; required_shots is monotone
        mid = (lo + hi) / 2.0
        if required_shots(p_ref, mid, alpha, power) > n:
            lo = mid
        else:
            hi = mid
    return hi


# ---------------------------------------------------------------------------

class QuantumSuccessProbabilityOracle(FlowFileTransform):
    """
    Scores a run against a single known-correct outcome.

    The distributional oracles in this project compare two empirical
    distributions to each other, which is the right tool when the specification
    does not fix one answer. A reversible arithmetic circuit does fix one
    answer, and that changes the statistics for the better: the observable
    collapses to a single proportion, correctness is defined against classical
    ground truth rather than against framework agreement, and the shot budget
    follows from a textbook two-proportion power calculation rather than from
    an empirically calibrated distance threshold.

    Scoring
    -------
    Reads measurement counts and the expected outcome (from the Expected
    Outcome property, or from ``arithmetic.expected_bitstring`` /
    ``arithmetic.expected_result_bits`` on the FlowFile). Reports the number of
    successes, the success probability, and a Wilson score interval.

    When ``arithmetic.result_qubits`` is present, counts are first marginalised
    onto the result register, so an implementation that leaves different values
    in its ancillas is not penalised for it.

    Verdicts
    --------
    In ``paired`` mode two runs sharing a Comparison Label are compared with a
    two-sided two-proportion test. The verdict ladder deliberately requires
    both statistical significance and a minimum effect:

      ``agree``                  the test does not reject
      ``negligible-difference``  the test rejects, but the gap is under
                                 Minimum Difference
      ``diverge``                the test rejects and the gap is large enough
                                 to matter
      ``no-test``                a run carried no usable shot counts

    Every input to that decision is emitted, so a verdict can be recomputed
    from the record without re-running anything.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Scores measurement counts against a single known-correct outcome: "
            "success probability with a Wilson confidence interval, and (in paired "
            "mode) a two-proportion test between two runs sharing a Comparison "
            "Label. For programs whose specification fixes exactly one answer, such "
            "as reversible arithmetic."
        )
        tags = ["quantum", "oracle", "testing", "success-probability", "wilson",
                "two-proportion", "arithmetic", "deterministic"]
        dependencies = []

    VERDICTS = ("agree", "negligible-difference", "diverge", "no-test")

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.expected_outcome = PropertyDescriptor(
            name="Expected Outcome",
            description=(
                "The single correct measurement outcome, as a bitstring in "
                "q0-left order. Leave empty to read it from the FlowFile "
                "attribute arithmetic.expected_result_bits, falling back to "
                "arithmetic.expected_bitstring."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.mode = PropertyDescriptor(
            name="Mode",
            description=(
                "single scores each run on its own. paired holds the first run "
                "of a Comparison Label and emits a verdict when the second "
                "arrives."
            ),
            required=True,
            default_value="single",
            allowable_values=["single", "paired"],
        )
        self.alternative = PropertyDescriptor(
            name="Alternative",
            description=(
                "Which alternative hypothesis the paired test uses. "
                "two-sided: any difference counts (the historical behaviour). "
                "worse: one-sided, only the mutant performing WORSE than its "
                "control counts -- the hypothesis a mutation kill actually has "
                "in mind, and the one to preregister for a confirmatory run. "
                "A mutant that beats its control is never a kill either way; "
                "under two-sided it is merely filtered out afterwards, which "
                "makes the effective test one-sided at alpha/2."
            ),
            required=True,
            default_value="two-sided",
            allowable_values=["two-sided", "worse"],
        )
        self.confidence = PropertyDescriptor(
            name="Confidence Level",
            description="Confidence level for the Wilson interval, e.g. 0.95.",
            required=True,
            default_value="0.95",
            validators=[StandardValidators.NUMBER_VALIDATOR],
        )
        self.alpha = PropertyDescriptor(
            name="Alpha",
            description="Significance level for the two-proportion test.",
            required=True,
            default_value="0.05",
            validators=[StandardValidators.NUMBER_VALIDATOR],
        )
        self.min_difference = PropertyDescriptor(
            name="Minimum Difference",
            description=(
                "Smallest absolute difference in success probability that counts "
                "as a real divergence. A statistically significant gap below this "
                "is reported as negligible-difference."
            ),
            required=True,
            default_value="0.05",
            validators=[StandardValidators.NUMBER_VALIDATOR],
        )
        self.label = PropertyDescriptor(
            name="Comparison Label",
            description="Pairing slot for paired mode.",
            required=True,
            default_value="default",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.state_dir = PropertyDescriptor(
            name="State Directory",
            description="Directory holding the first run of each pairing slot.",
            required=True,
            default_value="/tmp/quanifi_success_oracle",
        )

        self.descriptors = [
            self.expected_outcome, self.mode, self.confidence, self.alpha,
            self.min_difference, self.label, self.state_dir, self.alternative,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    # -- helpers -------------------------------------------------------------

    def _fail(self, message):
        self.logger.error(message)
        return FlowFileTransformResult(
            relationship="failure",
            attributes={"oracle.error": message},
        )

    @staticmethod
    def _score(counts, expected):
        shots = sum(counts.values())
        successes = int(counts.get(expected, 0))
        return successes, shots

    # -- transform -----------------------------------------------------------

    def transform(self, context, flowFile):
        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        attrs_in = flowFile.getAttributes()

        try:
            counts = json.loads(flowFile.getContentsAsBytes().decode("utf-8"))
        except Exception as exc:
            return self._fail(
                "QuantumSuccessProbabilityOracle: content is not a counts JSON object ({})"
                .format(exc))

        if not isinstance(counts, dict) or not counts:
            return self._fail(
                "QuantumSuccessProbabilityOracle: expected a non-empty counts object")

        if not all(isinstance(v, int) and not isinstance(v, bool) for v in counts.values()):
            return self._fail(
                "QuantumSuccessProbabilityOracle: counts must be integers; an "
                "analytic distribution carries no shot information, so no test is "
                "possible")

        # Ground truth: property first, then the arithmetic contract.
        expected = (get(self.expected_outcome) or "").strip()
        result_qubits = attrs_in.get(aspec.ATTR_RESULT_QUBITS)

        if not expected:
            expected = (attrs_in.get(aspec.ATTR_RESULT_BITS) or "").strip()
            if expected and result_qubits:
                idx = [int(i) for i in result_qubits.split(",") if i != ""]
                try:
                    counts = aspec.marginalize(counts, idx)
                except aspec.ArithmeticSpecError as exc:
                    return self._fail(
                        "QuantumSuccessProbabilityOracle: {}".format(exc))
        if not expected:
            expected = (attrs_in.get(aspec.ATTR_EXPECTED_BITS) or "").strip()

        if not expected:
            return self._fail(
                "QuantumSuccessProbabilityOracle: no ground truth available; set the "
                "Expected Outcome property or supply arithmetic.expected_result_bits "
                "or arithmetic.expected_bitstring on the FlowFile")

        try:
            confidence = float(get(self.confidence))
            alpha = float(get(self.alpha))
            min_diff = float(get(self.min_difference))
        except (TypeError, ValueError) as exc:
            return self._fail(
                "QuantumSuccessProbabilityOracle: non-numeric statistical setting ({})"
                .format(exc))

        successes, shots = self._score(counts, expected)
        p_hat = successes / shots if shots else 0.0
        lo, hi = wilson_interval(successes, shots, confidence)

        record = {
            "expected_outcome":    expected,
            "successes":           successes,
            "shots":               shots,
            "success_probability": round(p_hat, 6),
            "ci_low":              round(lo, 6),
            "ci_high":             round(hi, 6),
            "confidence":          confidence,
            "framework":           attrs_in.get("arithmetic.framework", ""),
            "implementation":      attrs_in.get("arithmetic.implementation", ""),
        }

        out_attrs = {
            "oracle.expected_outcome":    expected,
            "oracle.successes":           str(successes),
            "oracle.shots":               str(shots),
            "oracle.success_probability": "{:.6f}".format(p_hat),
            "oracle.ci_low":              "{:.6f}".format(lo),
            "oracle.ci_high":             "{:.6f}".format(hi),
            "oracle.confidence":          str(confidence),
            "oracle.detectable_difference":
                "{:.6f}".format(detectable_difference(shots, p_hat, alpha)),
        }

        if (get(self.mode) or "single").strip().lower() == "single":
            out_attrs["oracle.mode"] = "single"
            return FlowFileTransformResult(
                relationship="success",
                contents=json.dumps(record, indent=2).encode("utf-8"),
                attributes=out_attrs,
            )

        # -- paired mode -----------------------------------------------------
        label = get(self.label) or "default"
        state_dir = get(self.state_dir)
        os.makedirs(state_dir, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        state_path = os.path.join(state_dir, "%s.json" % safe)

        state = {}
        if os.path.exists(state_path):
            try:
                with open(state_path) as f:
                    state = json.load(f)
            except Exception:
                state = {}

        if "entry_a" not in state:
            state["entry_a"] = record
            with open(state_path, "w") as f:
                json.dump(state, f)
            self.logger.warn(
                "QuantumSuccessProbabilityOracle [%s]: stored first run, waiting "
                "for the second." % label)
            out_attrs["oracle.mode"] = "paired"
            out_attrs["oracle.verdict"] = "pending"
            return FlowFileTransformResult(
                relationship="failure",
                contents=json.dumps(record, indent=2).encode("utf-8"),
                attributes=out_attrs,
            )

        a = state["entry_a"]
        try:
            os.remove(state_path)
        except OSError:
            pass

        alternative = (get(self.alternative) or "two-sided").strip().lower()
        # Order matters for the one-sided test: arm A is the reference (the
        # control), arm B the run being judged, so "worse" means B below A.
        z, p_value = two_proportion_test(a["successes"], a["shots"],
                                         successes, shots,
                                         alternative=alternative)
        diff = abs(a["success_probability"] - p_hat)

        if p_value is None:
            verdict = "no-test"
        elif p_value >= alpha:
            verdict = "agree"
        elif diff < min_diff:
            verdict = "negligible-difference"
        else:
            verdict = "diverge"

        comparison = {
            "label":          label,
            "verdict":        verdict,
            "entry_a":        a,
            "entry_b":        record,
            "difference":     round(diff, 6),
            "z":              None if z is None else round(z, 6),
            "p_value":        None if p_value is None else round(p_value, 8),
            "alpha":          alpha,
            "min_difference": min_diff,
            "alternative":    alternative,
        }

        out_attrs.update({
            "oracle.mode":             "paired",
            "oracle.verdict":          verdict,
            "oracle.difference":       "{:.6f}".format(diff),
            "oracle.p_value":          "" if p_value is None else "{:.8f}".format(p_value),
            "oracle.z":                "" if z is None else "{:.6f}".format(z),
            "oracle.alpha":            str(alpha),
            "oracle.min_difference":   str(min_diff),
            "oracle.alternative":      alternative,
            "oracle.reference_success_probability":
                "{:.6f}".format(a["success_probability"]),
            "oracle.reference_shots":  str(a["shots"]),
        })

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(comparison, indent=2).encode("utf-8"),
            attributes=out_attrs,
        )
