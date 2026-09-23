import contextlib
import io
import json

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QrispShor(FlowFileTransform):
    """
    Factors an integer with Shor's algorithm via Qrisp's native
    shors_alg — the full period-finding pipeline (modular arithmetic,
    QFT, continued fractions) behind a single "Number To Factor" property.

    Qrisp is the only integrated framework that ships Shor as a one-call
    high-level routine, which is exactly Quanifi's reuse story: the most
    famous quantum algorithm becomes a drag-and-drop box. Practical sizes
    on the local simulator are small (15, 21, 33...); the point is the
    protocol, not RSA.

    No report.type is set: the default report card lists the shor.*
    attributes (number, factors), which is the headline for this demo.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Runs Shor's factoring algorithm using Qrisp's native shors_alg. "
            "Configure Number To Factor (small odd composites, e.g. 15 or 21); "
            "emits shor.factor_1 / shor.factor_2. Educational scale: the local "
            "statevector simulator limits N to small numbers."
        )
        tags = ["quantum", "qrisp", "shor", "factoring", "period-finding", "education"]
        dependencies = ["qrisp==0.9.5", "qiskit>=2.0.0,<2.5"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.number = PropertyDescriptor(
            name="Number To Factor",
            description=(
                "Composite integer N >= 4 to factor. Small numbers only (the "
                "circuit needs ~2*log2(N) qubits on the local simulator)."
            ),
            required=True,
            default_value="15",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.number]

    def getPropertyDescriptors(self):
        return self.descriptors

    @staticmethod
    def _is_prime(n):
        if n < 2:
            return False
        d = 2
        while d * d <= n:
            if n % d == 0:
                return False
            d += 1
        return True

    def transform(self, context, flowFile):
        raw = (
            context.getProperty(self.number)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )
        try:
            n = int(raw)
        except (TypeError, ValueError):
            n = -1
        if n < 4:
            msg = "Number To Factor must be an integer >= 4, got {!r}".format(raw)
            self.logger.error("QrispShor: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"", attributes={"shor.error": msg},
            )
        if self._is_prime(n):
            msg = "{} is prime; Shor's algorithm factors composites".format(n)
            self.logger.error("QrispShor: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"", attributes={"shor.error": msg},
            )

        from qrisp.shor import shors_alg

        try:
            # qrisp prints simulator progress bars; keep them out of NiFi logs
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                factor = int(shors_alg(n))
        except Exception as exc:
            self.logger.error("QrispShor failed for N={}: {}".format(n, exc))
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"shor.error": "factoring failed: {}".format(exc)},
            )

        if factor in (1, n) or n % factor != 0:
            msg = "shors_alg returned a non-factor {} for N={}".format(factor, n)
            self.logger.error("QrispShor: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"", attributes={"shor.error": msg},
            )

        p, q = sorted((factor, n // factor))
        self.logger.warn("QrispShor: {} = {} x {}".format(n, p, q))

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps({"number": n, "factors": [p, q]}, indent=2).encode("utf-8"),
            attributes={
                "shor.number": str(n),
                "shor.factor_1": str(p),
                "shor.factor_2": str(q),
                "shor.framework": "qrisp",
            },
        )
