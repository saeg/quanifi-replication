OPENQASM 2.0;
include "qelib1.inc";
qreg q[1];
creg c[1];
rx(pi/2) q[0];
rz(pi/2) q[0];
h q[0];
measure q[0] -> c[0];