"""
Shared test infrastructure for Quanifi NiFi processor tests.

NiFi's Python API (nifiapi.*) is a JVM-backed package that only exists
inside a running NiFi process.  This module installs lightweight stubs
into sys.modules before any processor file is imported, so the processors
can be loaded and executed in plain pytest without a JVM.
"""

import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# nifiapi stubs
# ---------------------------------------------------------------------------

class _Logger:
    def warn(self, msg):  pass
    def info(self, msg):  pass
    def error(self, msg): pass
    def debug(self, msg): pass


class FlowFileTransform:
    def __init__(self):
        self.logger = _Logger()


class FlowFileTransformResult:
    def __init__(self, relationship, contents=b"", attributes=None):
        self.relationship = relationship
        self.contents = bytes(contents) if contents else b""
        self.attributes = attributes or {}


class PropertyDescriptor:
    def __init__(self, name, description="", required=False, default_value=None, **kwargs):
        self.name = name
        self.description = description
        self.default_value = default_value
        self.required = required
        # Retained so a test can assert the DECLARED scope, which is a
        # different claim from "the processor happened not to apply EL":
        # only the declaration stops NiFi itself from substituting an
        # incoming attribute into a campaign-control property.
        self.expression_language_scope = kwargs.get(
            "expression_language_scope", _ExpressionLanguageScope.NONE)
        self.allowable_values = kwargs.get("allowable_values")


class _StandardValidators:
    POSITIVE_INTEGER_VALIDATOR = None
    NON_NEGATIVE_INTEGER_VALIDATOR = None
    NON_EMPTY_VALIDATOR = None
    INTEGER_VALIDATOR = None
    NUMBER_VALIDATOR = None


class _ExpressionLanguageScope:
    # Distinct sentinels rather than None/None, so "declared NONE" and
    # "declared FLOWFILE_ATTRIBUTES" are actually distinguishable in a test.
    FLOWFILE_ATTRIBUTES = "FLOWFILE_ATTRIBUTES"
    NONE = "NONE"


class _JvmHolder:
    jvm = None


class Relationship:
    """Minimal NiFi Relationship stub (name + flags)."""
    def __init__(self, name, description="", auto_terminated=False):
        self.name = name
        self.description = description
        self.auto_terminated = auto_terminated


_nifiapi     = types.ModuleType("nifiapi")
_props_mod   = types.ModuleType("nifiapi.properties")
_fft_mod     = types.ModuleType("nifiapi.flowfiletransform")
_rel_mod     = types.ModuleType("nifiapi.relationship")
_jvm_mod     = types.ModuleType("nifiapi.__jvm__")

_props_mod.PropertyDescriptor      = PropertyDescriptor
_props_mod.StandardValidators      = _StandardValidators
_props_mod.ExpressionLanguageScope = _ExpressionLanguageScope
_fft_mod.FlowFileTransform         = FlowFileTransform
_fft_mod.FlowFileTransformResult   = FlowFileTransformResult
_rel_mod.Relationship              = Relationship
_jvm_mod.JvmHolder                 = _JvmHolder

sys.modules.setdefault("nifiapi",                   _nifiapi)
sys.modules.setdefault("nifiapi.properties",        _props_mod)
sys.modules.setdefault("nifiapi.flowfiletransform", _fft_mod)
sys.modules.setdefault("nifiapi.relationship",      _rel_mod)
sys.modules.setdefault("nifiapi.__jvm__",           _jvm_mod)

# ---------------------------------------------------------------------------
# Make nifi_extensions importable by name
# ---------------------------------------------------------------------------

EXTENSIONS_DIR = str(Path(__file__).parent.parent / "nifi_extensions")
if EXTENSIONS_DIR not in sys.path:
    sys.path.insert(0, EXTENSIONS_DIR)

# ---------------------------------------------------------------------------
# Helpers available to all test modules
# ---------------------------------------------------------------------------

import re

# Minimal NiFi Expression Language evaluator.  Supports the two forms the
# attribute-driven (data-driven / mutation) flows rely on:
#     ${name}                        -> attribute value, or "" if absent
#     ${name:replaceEmpty('fallbk')} -> attribute value, or the fallback if empty
# Any string without a ${...} token is returned unchanged, so non-EL property
# values (the vast majority) behave exactly as before.  This mirrors NiFi:
# EL is only resolved when evaluateAttributeExpressions(flowFile) is called.

_EL_TOKEN = re.compile(r"\$\{([^}]*)\}")
_REPLACE_EMPTY = re.compile(r"""^\s*([A-Za-z0-9_.\-]+)\s*:\s*replaceEmpty\(\s*['"](.*)['"]\s*\)\s*$""")


def _resolve_token(expr, attrs):
    m = _REPLACE_EMPTY.match(expr)
    if m:
        name, fallback = m.group(1), m.group(2)
        val = attrs.get(name)
        return val if val not in (None, "") else fallback
    name = expr.strip()
    val = attrs.get(name)
    return "" if val is None else val


def evaluate_el(value, attrs):
    if not isinstance(value, str) or "${" not in value:
        return value
    return _EL_TOKEN.sub(lambda m: str(_resolve_token(m.group(1), attrs)), value)


class _PropChain:
    """Mimics .evaluateAttributeExpressions(ff).getValue() and .getValue() directly."""
    def __init__(self, value, attrs=None):
        self._value = value
        self._attrs = attrs

    def evaluateAttributeExpressions(self, flowFile=None):
        attrs = flowFile.getAttributes() if flowFile is not None else {}
        return _PropChain(evaluate_el(self._value, attrs), attrs)

    def getValue(self):
        return self._value


class MockContext:
    """Minimal NiFi ProcessContext stub.

    Pass property values as keyword args keyed by the PropertyDescriptor name::

        ctx = MockContext(**{"Shots": "512", "Output Format": "qasm3"})

    If a property is not supplied, the descriptor's ``default_value`` is used.
    """
    def __init__(self, **props):
        self._props = props

    def getProperty(self, descriptor):
        key = descriptor.name if hasattr(descriptor, "name") else str(descriptor)
        fallback = descriptor.default_value if hasattr(descriptor, "default_value") else None
        return _PropChain(self._props.get(key, fallback))


class MockFlowFile:
    """Minimal NiFi FlowFile stub."""
    def __init__(self, content=b"", attributes=None):
        self._content = content if isinstance(content, bytes) else content.encode()
        self._attrs   = attributes or {}

    def getContentsAsBytes(self):
        return bytearray(self._content)

    def getAttribute(self, key):
        return self._attrs.get(key)

    def getAttributes(self):
        return dict(self._attrs)


def result_to_flowfile(result: FlowFileTransformResult) -> MockFlowFile:
    """Convert the output of one processor into the input FlowFile for the next."""
    return MockFlowFile(content=result.contents, attributes=result.attributes)


def result_to_flowfile_merged(result: FlowFileTransformResult,
                              upstream: MockFlowFile) -> MockFlowFile:
    """Like result_to_flowfile, but with real NiFi semantics: a transform's
    returned attributes are MERGED onto the FlowFile's existing attributes —
    NiFi never drops an attribute a processor doesn't re-emit. Use this to test
    that builders blank stale presentation attrs in cross-framework chains."""
    merged = upstream.getAttributes()
    merged.update(result.attributes)
    return MockFlowFile(content=result.contents, attributes=merged)


# --- IBM quota guard -------------------------------------------------------
# QuantumIBMBatchSubmitter re-reads IBM usage immediately before SamplerV2.run()
# and is deliberately fail-closed: with no reachable usage ledger it refuses to
# submit. Stubs that replace `prepare`/`run_job` to stay off the network must
# therefore also replace `service_for`, or they exercise the refusal path rather
# than the submit path they mean to test.

class FakeUsageService:
    """The read-only slice of QiskitRuntimeService that the quota guard uses."""

    def __init__(self, consumed=100.0, limit=600.0, error=None):
        self.consumed = consumed
        self.limit = limit
        self.error = error
        self.calls = 0

    def usage(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        from datetime import datetime, timedelta, timezone  # noqa: PLC0415

        now = datetime.now(timezone.utc)
        return {
            "usage": {"seconds": self.consumed},
            "usage_limit": {"seconds": self.limit},
            "usage_period": {
                "start_time": now - timedelta(days=28),
                "end_time": now,
            },
        }


def with_fake_usage(cls, **kwargs):
    """Subclass a submitter so its quota read hits `FakeUsageService`."""
    service = FakeUsageService(**kwargs)

    class _Offline(cls):
        quota_service = service

        @staticmethod
        def service_for(token, instance):
            return service

    _Offline.__name__ = "Offline" + cls.__name__
    return _Offline
