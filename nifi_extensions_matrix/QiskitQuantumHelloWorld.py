from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QiskitQuantumHelloWorld(FlowFileTransform):
    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = "Runs a Qiskit GHZ-state circuit and writes the measurement bitstring to the FlowFile content"
        tags = ["quantum", "qiskit", "simulation"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-aer>=0.13.0"]

    def __init__(self, **kwargs):
        # NiFi passes jvm= so the processor can access JVM objects (validators, etc.)
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()
        self.qubit_count = PropertyDescriptor(
            name="Qubit Count",
            description="Number of qubits in the GHZ circuit",
            required=True,
            default_value="2",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.qubit_count]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from qiskit import QuantumCircuit
        from qiskit_aer import AerSimulator

        num_qubits = int(
            context.getProperty(self.qubit_count)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        qc = QuantumCircuit(num_qubits)
        qc.h(0)
        for i in range(1, num_qubits):
            qc.cx(0, i)
        qc.measure_all()

        backend = AerSimulator()
        result = backend.run(qc, shots=1).result()
        bitstring = list(result.get_counts().keys())[0]

        return FlowFileTransformResult(
            relationship="success",
            contents=bitstring.encode("utf-8"),
            attributes={"quantum.bitstring": bitstring, "quantum.qubits": str(num_qubits)},
        )
