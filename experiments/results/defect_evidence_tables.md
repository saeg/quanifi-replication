# Defect-evidence tables (recomputed from archived hardware counts)

Recomputed directly from the archived counts under `experiments/results/`; see `experiments/defect_evidence_tables.py`. Probabilities are shown to 3 decimals here; the CSVs alongside this file keep full precision.

## Table 1 -- Two-route adder + Toffoli comparison (18 circuits, 512 shots)

Source: `experiments/results/open_quantum_route_comparison/adders/direct_iqm/adder-defect-direct-01a0ae3a-fc21-74c2-87c0-dbc99d301de8.json`, `experiments/results/open_quantum_route_comparison/adders/open_quantum/job_ids.json`, `experiments/results/open_quantum_route_comparison/adders/checks/controlled_reproduction.json`.

Bit order: All counts shown q0-left. Direct-IQM counts are Qiskit order (q0-right) and are reversed before scoring; the OQ archive is already q0-left (checked against its `bit_order` field).

| circuit | direct_p_correct | direct_top | oq_p_correct | oq_top | oq_p_top |
|---|---|---|---|---|---|
| cirq_qft-bw1-a0-b0-decomposed | 0.795 | 00 | 0.268 | 00 | 0.268 |
| cirq_qft-bw1-a0-b1-decomposed | 0.777 | 10 | 0.248 | 00 | 0.256 |
| cirq_qft-bw1-a1-b0-decomposed | 0.803 | 10 | 0.305 | 10 | 0.305 |
| cirq_qft-bw1-a1-b1-decomposed | 0.785 | 01 | 0.223 | 10 | 0.297 |
| pennylane_outadder-bw1-a0-b0-decomposed | 0.881 | 00 | 1.000 | 00 | 1.000 |
| pennylane_outadder-bw1-a0-b1-decomposed | 0.863 | 10 | 0.006 | 00 | 0.988 |
| pennylane_outadder-bw1-a1-b0-decomposed | 0.844 | 10 | 0.004 | 00 | 0.990 |
| pennylane_outadder-bw1-a1-b1-decomposed | 0.824 | 01 | 0.000 | 00 | 0.998 |
| qiskit_cdkm-bw1-a0-b0-decomposed | 0.859 | 00 | 0.932 | 00 | 0.932 |
| qiskit_cdkm-bw1-a0-b0-whole | 0.855 | 00 | 0.900 | 00 | 0.900 |
| qiskit_cdkm-bw1-a0-b1-decomposed | 0.834 | 10 | 0.928 | 10 | 0.928 |
| qiskit_cdkm-bw1-a0-b1-whole | 0.848 | 10 | 0.918 | 10 | 0.918 |
| qiskit_cdkm-bw1-a1-b0-decomposed | 0.801 | 10 | 0.061 | 11 | 0.859 |
| qiskit_cdkm-bw1-a1-b0-whole | 0.814 | 10 | 0.850 | 10 | 0.850 |
| qiskit_cdkm-bw1-a1-b1-decomposed | 0.738 | 01 | 0.883 | 01 | 0.883 |
| qiskit_cdkm-bw1-a1-b1-whole | 0.730 | 01 | 0.703 | 01 | 0.703 |
| toffoli-decomposed | 0.740 | 111 | 0.002 | 110 | 0.971 |
| toffoli-raw | 0.781 | 111 | 0.830 | 111 | 0.830 |

## Table 2 -- Two-route Grover comparison (16 circuits, 512 shots)

Source: `experiments/results/open_quantum_route_comparison/grover/direct_iqm/adder-defect-direct-01a0ae53-c9a4-7800-b179-18bf154fcc45.json`, `experiments/results/open_quantum_route_comparison/grover/open_quantum/job_ids.json`.

Bit order: All counts shown q0-left. Direct-IQM counts are Qiskit order (q0-right) and are reversed before scoring; the OQ archive is already q0-left. The marked state is the circuit name's last dash-separated token, already q0-left.

| circuit | marked_state | direct_p_correct | direct_top | direct_p_top | oq_p_correct | oq_top | oq_p_top |
|---|---|---|---|---|---|---|---|
| cirq-grover-hw-000-10 | 10 | 0.932 | 10 | 0.932 | 0.016 | 00 | 0.982 |
| cirq-grover-hw-001-11 | 11 | 0.943 | 11 | 0.943 | 0.000 | 00 | 0.990 |
| cirq-grover-hw-002-0111 | 0111 | 0.098 | 0111 | 0.098 | 0.008 | 0000 | 0.227 |
| cirq-grover-hw-003-0110 | 0110 | 0.076 | 1110 | 0.082 | 0.160 | 0100 | 0.215 |
| pennylane-grover-hw-000-10 | 10 | 0.912 | 10 | 0.912 | 0.955 | 10 | 0.955 |
| pennylane-grover-hw-001-11 | 11 | 0.945 | 11 | 0.945 | 0.912 | 11 | 0.912 |
| pennylane-grover-hw-002-0111 | 0111 | 0.123 | 0111 | 0.123 | 0.053 | 1001 | 0.092 |
| pennylane-grover-hw-003-0110 | 0110 | 0.129 | 0110 | 0.129 | 0.051 | 0011 | 0.090 |
| qiskit-grover-hw-000-10 | 10 | 0.930 | 10 | 0.930 | 0.959 | 10 | 0.959 |
| qiskit-grover-hw-001-11 | 11 | 0.930 | 11 | 0.930 | 0.936 | 11 | 0.936 |
| qiskit-grover-hw-002-0111 | 0111 | 0.188 | 0111 | 0.188 | 0.066 | 1010 | 0.094 |
| qiskit-grover-hw-003-0110 | 0110 | 0.145 | 0110 | 0.145 | 0.070 | 0010 | 0.088 |
| readout-grover-hw-000-10 | 10 | 0.945 | 10 | 0.945 | 0.980 | 10 | 0.980 |
| readout-grover-hw-001-11 | 11 | 0.957 | 11 | 0.957 | 0.924 | 11 | 0.924 |
| readout-grover-hw-002-0111 | 0111 | 0.922 | 0111 | 0.922 | 0.932 | 0111 | 0.932 |
| readout-grover-hw-003-0110 | 0110 | 0.936 | 0110 | 0.936 | 0.920 | 0110 | 0.920 |

## Table 3 -- Cepheus adder discovery run via Open Quantum (Rigetti); qualification (21), repeat_probe (12), layout_probe (9 of 12 planned)

Source: `experiments/results/open_quantum_cepheus_discovery/qualification_run/plan.json`, `experiments/results/open_quantum_cepheus_discovery/qualification_run/collected/oq-qualification.json`, `experiments/results/open_quantum_cepheus_discovery/repeat_probe/plan.json`, `experiments/results/open_quantum_cepheus_discovery/repeat_probe/collected.json`, `experiments/results/open_quantum_cepheus_discovery/layout_probe/plan.json`, `experiments/results/open_quantum_cepheus_discovery/layout_probe/collected.json`.

Bit order: Counts are q0_left (per each collected entry's `bit_order` field). `decoded_dominant`/`p_decoded_correct` decode little-endian over `arithmetic.result_qubits`, i.e. value = sum(bit[q_k] << k).

| campaign | label | version | case_id | expected_full | expected_full_count | expected_full_p | p_decoded_correct | dominant | dominant_count | dominant_p | decoded_dominant | expected_result | decoded_correct |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| qualification | cdkm@0+0 | cdkm | 0+0 | 000000 | 406 | 0.793 | 0.840 | 000000 | 406 | 0.793 | 0 | 0 | True |
| qualification | qft@0+0 | qft | 0+0 | 00000 | 355 | 0.693 | 0.725 | 00000 | 355 | 0.693 | 0 | 0 | True |
| qualification | semiadder@0+0 | semiadder | 0+0 | 0000000 | 21 | 0.041 | 0.129 | 0001010 | 22 | 0.043 | 2 | 0 | False |
| qualification | cdkm@1+1 | cdkm | 1+1 | 100100 | 360 | 0.703 | 0.811 | 100100 | 360 | 0.703 | 2 | 2 | True |
| qualification | semiadder@1+1 | semiadder | 1+1 | 1001000 | 13 | 0.025 | 0.184 | 1011010 | 14 | 0.027 | 3 | 2 | False |
| qualification | cdkm@0+3 | cdkm | 0+3 | 001100 | 328 | 0.641 | 0.705 | 001100 | 328 | 0.641 | 3 | 3 | True |
| qualification | qft@0+3 | qft | 0+3 | 00110 | 6 | 0.012 | 0.018 | 00000 | 375 | 0.732 | 0 | 3 | False |
| qualification | semiadder@0+3 | semiadder | 0+3 | 0011000 | 16 | 0.031 | 0.154 | 0000000 | 25 | 0.049 | 0 | 3 | False |
| qualification | qft@1+1 | qft | 1+1 | 10010 | 1 | 0.002 | 0.051 | 00000 | 385 | 0.752 | 0 | 2 | False |
| qualification | cdkm@1+3 | cdkm | 1+3 | 100010 | 21 | 0.041 | 0.059 | 100000 | 387 | 0.756 | 0 | 4 | False |
| qualification | qft@1+3 | qft | 1+3 | 10001 | 0 | 0.000 | 0.047 | 00000 | 371 | 0.725 | 0 | 4 | False |
| qualification | semiadder@1+3 | semiadder | 1+3 | 1000100 | 12 | 0.023 | 0.121 | 1001000 | 18 | 0.035 | 2 | 4 | False |
| qualification | cdkm@3+0 | cdkm | 3+0 | 111100 | 1 | 0.002 | 0.002 | 111010 | 230 | 0.449 | 5 | 3 | False |
| qualification | qft@3+0 | qft | 3+0 | 11110 | 0 | 0.000 | 0.023 | 00000 | 359 | 0.701 | 0 | 3 | False |
| qualification | semiadder@3+0 | semiadder | 3+0 | 1111000 | 12 | 0.023 | 0.133 | 1101000 | 19 | 0.037 | 2 | 3 | False |
| qualification | cdkm@3+1 | cdkm | 3+1 | 110010 | 241 | 0.471 | 0.596 | 110010 | 241 | 0.471 | 4 | 4 | True |
| qualification | qft@3+1 | qft | 3+1 | 11001 | 0 | 0.000 | 0.035 | 00000 | 373 | 0.729 | 0 | 4 | False |
| qualification | semiadder@3+1 | semiadder | 3+1 | 1100100 | 8 | 0.016 | 0.100 | 1000000 | 16 | 0.031 | 0 | 4 | False |
| qualification | cdkm@3+3 | cdkm | 3+3 | 110110 | 225 | 0.439 | 0.555 | 110110 | 225 | 0.439 | 6 | 6 | True |
| qualification | qft@3+3 | qft | 3+3 | 11011 | 0 | 0.000 | 0.006 | 00000 | 373 | 0.729 | 0 | 6 | False |
| qualification | semiadder@3+3 | semiadder | 3+3 | 1101100 | 12 | 0.023 | 0.131 | 1101000 | 24 | 0.047 | 2 | 6 | False |
| repeat_probe | cdkm@3+0@run1 | cdkm | 3+0 | 111100 | 3 | 0.006 | 0.008 | 111010 | 248 | 0.484 | 5 | 3 | False |
| repeat_probe | cdkm@3+0@run2 | cdkm | 3+0 | 111100 | 2 | 0.004 | 0.008 | 111010 | 223 | 0.436 | 5 | 3 | False |
| repeat_probe | cdkm@3+0@run3 | cdkm | 3+0 | 111100 | 0 | 0.000 | 0.000 | 111010 | 238 | 0.465 | 5 | 3 | False |
| repeat_probe | cdkm@3+0@run4 | cdkm | 3+0 | 111100 | 2 | 0.004 | 0.004 | 111010 | 243 | 0.475 | 5 | 3 | False |
| repeat_probe | cdkm@1+3@run1 | cdkm | 1+3 | 100010 | 27 | 0.053 | 0.072 | 100000 | 383 | 0.748 | 0 | 4 | False |
| repeat_probe | cdkm@1+3@run2 | cdkm | 1+3 | 100010 | 22 | 0.043 | 0.057 | 100000 | 406 | 0.793 | 0 | 4 | False |
| repeat_probe | cdkm@1+3@run3 | cdkm | 1+3 | 100010 | 20 | 0.039 | 0.061 | 100000 | 389 | 0.760 | 0 | 4 | False |
| repeat_probe | cdkm@1+3@run4 | cdkm | 1+3 | 100010 | 23 | 0.045 | 0.068 | 100000 | 394 | 0.770 | 0 | 4 | False |
| repeat_probe | cdkm@0+3@run1 | cdkm | 0+3 | 001100 | 360 | 0.703 | 0.752 | 001100 | 360 | 0.703 | 3 | 3 | True |
| repeat_probe | cdkm@0+3@run2 | cdkm | 0+3 | 001100 | 345 | 0.674 | 0.729 | 001100 | 345 | 0.674 | 3 | 3 | True |
| repeat_probe | cdkm@0+3@run3 | cdkm | 0+3 | 001100 | 366 | 0.715 | 0.768 | 001100 | 366 | 0.715 | 3 | 3 | True |
| repeat_probe | cdkm@0+3@run4 | cdkm | 0+3 | 001100 | 353 | 0.689 | 0.738 | 001100 | 353 | 0.689 | 3 | 3 | True |
| layout_probe | cdkm@3+0@region1 | cdkm | 3+0 | 111100 | 8 | 0.016 | 0.131 | 011001 | 16 | 0.031 | 1 | 3 | False |
| layout_probe | cdkm@1+3@region1 | cdkm | 1+3 | 100010 | 12 | 0.023 | 0.146 | 011000 | 17 | 0.033 | 1 | 4 | False |
| layout_probe | cdkm@0+3@region1 | cdkm | 0+3 | 001100 | 13 | 0.025 | 0.129 | 101000 | 19 | 0.037 | 1 | 3 | False |
| layout_probe | cdkm@3+0@region2 | cdkm | 3+0 | 111100 | 12 | 0.023 | 0.146 | 101101 | 16 | 0.031 | 3 | 3 | True |
| layout_probe | cdkm@1+3@region2 | cdkm | 1+3 | 100010 | 6 | 0.012 | 0.107 | 011111 | 13 | 0.025 | 7 | 4 | False |
| layout_probe | cdkm@0+3@region2 | cdkm | 0+3 | 001100 | 15 | 0.029 | 0.170 | 001000 | 30 | 0.059 | 1 | 3 | False |
| layout_probe | cdkm@3+0@region3 | cdkm | 3+0 | 111100 | 12 | 0.023 | 0.125 | 011001 | 21 | 0.041 | 1 | 3 | False |
| layout_probe | cdkm@1+3@region3 | cdkm | 1+3 | 100010 | 9 | 0.018 | 0.109 | 001000 | 20 | 0.039 | 1 | 4 | False |
| layout_probe | cdkm@0+3@region3 | cdkm | 0+3 | 001100 | 17 | 0.033 | 0.117 | 000100 | 57 | 0.111 | 2 | 3 | False |

## Table 4 -- Toffoli probe (Rigetti Cepheus via Open Quantum + IQM direct)

Source: `experiments/results/open_quantum_cepheus_discovery/toffoli_probe/results/raw_counts.json`, `experiments/results/open_quantum_cepheus_discovery/toffoli_probe/results/jobs_summary.csv`.

Bit order: raw_counts.json stores `raw_counts_littleendian`; the top outcome and its probability are reversed to q0-left here and cross-checked against jobs_summary.csv's `top_q0_left`/`p_top` columns (raises on disagreement).

| name | job_id | expected | top_q0_left | p_top |
|---|---|---|---|---|
| A_toffoli_raw_ccx | 9866be2e-34f1-4ab7-9aa7-e3d6f4a31c18 | 111 | 111 | 0.592 |
| B_toffoli_decomposed | 0ba08611-799a-4403-9605-5322155fccd8 | 111 | 110 | 0.873 |
| C1_single_x | 66a21605-7150-41e6-976c-635911ed753a | 1 | 10 | 0.893 |
| C2_rz_pi_sandwich | a6e0af03-a9a2-4433-989e-edccbefa2e21 | 1 | 10 | 0.855 |
| C3_hh_control | 6ff426af-1566-4f3b-ac45-f1c03e8ac76b | 0 | 00 | 0.986 |
| D1_bitorder_100000 | 2c3ae2f1-5235-40e8-a5b0-2064cbadca98 | 100000 | 100000 | 0.678 |
| D2_bitorder_110000 | 0983cd07-ca91-409b-9825-6bdcbcb0e6f4 | 110000 | 110000 | 0.729 |
| D3_bitorder_001010 | 001e5d05-189e-4366-9bb0-b09152afc939 | 001010 | 001010 | 0.852 |
| D4_bitorder_000110 | 33ccf03c-08d5-4dcd-aba9-e230ea2b7418 | 000110 | 000110 | 0.809 |
| E1_route_q0_q5 | 198ea128-0b30-43e4-bd43-882cffc55a36 | 100001 | 100001 | 0.662 |
| E2_route_chain | 1171b1db-0659-43cf-b1ab-b4b613dbbee9 | 100101 | 100101 | 0.744 |
| E3_route_fanout | 1156cd1b-8593-445a-b71f-6dec20829dee | 101011 | 101011 | 0.625 |
| F1_iqm_raw_ccx | 21d1cc6c-fd9d-43d4-9918-883aa7cdbd8d | 111 | 111 | 0.857 |
| F2_iqm_decomposed | 409789b6-7b4a-47e7-ad94-17b3072e0261 | 111 | 110 | 0.959 |

## Table 5 -- Quantum Inspire Rx-sign probe

Source: `experiments/results/quantum_inspire_rx/probe-843300.json`.

Bit order: Single-qubit outcomes ('0'/'1'); P(standard)/P(negated) are recomputed from `counts` against each row's `expected_standard_rx`/`expected_negated_rx` fields.

| circuit | count_0 | count_1 | p_standard | p_negated |
|---|---|---|---|---|
| rx_sign_probe | 67 | 957 | 0.065 | 0.935 |
| rx_sign_probe_neg | 922 | 102 | 0.100 | 0.900 |
| ry_sign_control | 924 | 100 | 0.902 | 0.098 |
| rx_pi_control | 49 | 975 | 0.952 | 0.952 |
| idle_control | 950 | 74 | 0.928 | 0.928 |

## Table 6 -- Two-route adder + Toffoli comparison, 17 Sep vs 25 Sep rerun (same 18 circuits, same submission code)

Source: `experiments/results/open_quantum_route_comparison/adders/direct_iqm/adder-defect-direct-01a0ae3a-fc21-74c2-87c0-dbc99d301de8.json`, `experiments/results/open_quantum_route_comparison/adders/open_quantum/job_ids.json`, `experiments/results/open_quantum_route_comparison/rerun_2026-09-25/adders/direct_iqm/adder-defect-direct-01a0da40-2323-7075-a91c-21587d178506.json`, `experiments/results/open_quantum_route_comparison/rerun_2026-09-25/adders/open_quantum/job_ids.json`.

Bit order: Same convention as Table 1 for both runs (direct-IQM reversed q0-right -> q0-left; OQ archives already q0-left).

| circuit | direct_p_correct_17sep | oq_p_correct_17sep | oq_top_17sep | oq_p_top_17sep | direct_p_correct_25sep | oq_p_correct_25sep | oq_top_25sep | oq_p_top_25sep |
|---|---|---|---|---|---|---|---|---|
| cirq_qft-bw1-a0-b0-decomposed | 0.795 | 0.268 | 00 | 0.268 | 0.848 | 0.236 | 10 | 0.281 |
| cirq_qft-bw1-a0-b1-decomposed | 0.777 | 0.248 | 00 | 0.256 | 0.820 | 0.213 | 00 | 0.281 |
| cirq_qft-bw1-a1-b0-decomposed | 0.803 | 0.305 | 10 | 0.305 | 0.803 | 0.258 | 00 | 0.285 |
| cirq_qft-bw1-a1-b1-decomposed | 0.785 | 0.223 | 10 | 0.297 | 0.805 | 0.248 | 10 | 0.271 |
| pennylane_outadder-bw1-a0-b0-decomposed | 0.881 | 1.000 | 00 | 1.000 | 0.896 | 0.994 | 00 | 0.994 |
| pennylane_outadder-bw1-a0-b1-decomposed | 0.863 | 0.006 | 00 | 0.988 | 0.820 | 0.006 | 00 | 0.986 |
| pennylane_outadder-bw1-a1-b0-decomposed | 0.844 | 0.004 | 00 | 0.990 | 0.799 | 0.006 | 00 | 0.990 |
| pennylane_outadder-bw1-a1-b1-decomposed | 0.824 | 0.000 | 00 | 0.998 | 0.848 | 0.012 | 00 | 0.984 |
| qiskit_cdkm-bw1-a0-b0-decomposed | 0.859 | 0.932 | 00 | 0.932 | 0.896 | 0.955 | 00 | 0.955 |
| qiskit_cdkm-bw1-a0-b0-whole | 0.855 | 0.900 | 00 | 0.900 | 0.883 | 0.930 | 00 | 0.930 |
| qiskit_cdkm-bw1-a0-b1-decomposed | 0.834 | 0.928 | 10 | 0.928 | 0.848 | 0.939 | 10 | 0.939 |
| qiskit_cdkm-bw1-a0-b1-whole | 0.848 | 0.918 | 10 | 0.918 | 0.867 | 0.879 | 10 | 0.879 |
| qiskit_cdkm-bw1-a1-b0-decomposed | 0.801 | 0.061 | 11 | 0.859 | 0.811 | 0.035 | 11 | 0.842 |
| qiskit_cdkm-bw1-a1-b0-whole | 0.814 | 0.850 | 10 | 0.850 | 0.820 | 0.850 | 10 | 0.850 |
| qiskit_cdkm-bw1-a1-b1-decomposed | 0.738 | 0.883 | 01 | 0.883 | 0.754 | 0.902 | 01 | 0.902 |
| qiskit_cdkm-bw1-a1-b1-whole | 0.730 | 0.703 | 01 | 0.703 | 0.766 | 0.740 | 01 | 0.740 |
| toffoli-decomposed | 0.740 | 0.002 | 110 | 0.971 | 0.826 | 0.006 | 110 | 0.922 |
| toffoli-raw | 0.781 | 0.830 | 111 | 0.830 | 0.846 | 0.840 | 111 | 0.840 |

## Table 7 -- Two-route Grover comparison, 17 Sep vs 25 Sep rerun (same 16 circuits, same submission code)

Source: `experiments/results/open_quantum_route_comparison/grover/direct_iqm/adder-defect-direct-01a0ae53-c9a4-7800-b179-18bf154fcc45.json`, `experiments/results/open_quantum_route_comparison/grover/open_quantum/job_ids.json`, `experiments/results/open_quantum_route_comparison/rerun_2026-09-25/grover/direct_iqm/adder-defect-direct-01a0da40-744c-7570-9ec6-a1b5c154848f.json`, `experiments/results/open_quantum_route_comparison/rerun_2026-09-25/grover/open_quantum/job_ids.json`.

Bit order: Same convention as Table 2 for both runs (direct-IQM reversed q0-right -> q0-left; OQ archives already q0-left; marked state is the circuit name's last dash-separated token).

| circuit | marked_state | direct_p_correct_17sep | oq_p_correct_17sep | oq_top_17sep | oq_p_top_17sep | direct_p_correct_25sep | oq_p_correct_25sep | oq_top_25sep | oq_p_top_25sep |
|---|---|---|---|---|---|---|---|---|---|
| cirq-grover-hw-000-10 | 10 | 0.932 | 0.016 | 00 | 0.982 | 0.943 | 0.010 | 00 | 0.979 |
| cirq-grover-hw-001-11 | 11 | 0.943 | 0.000 | 00 | 0.990 | 0.957 | 0.000 | 00 | 0.988 |
| cirq-grover-hw-002-0111 | 0111 | 0.098 | 0.008 | 0000 | 0.227 | 0.129 | 0.018 | 0000 | 0.318 |
| cirq-grover-hw-003-0110 | 0110 | 0.076 | 0.160 | 0100 | 0.215 | 0.137 | 0.092 | 0000 | 0.301 |
| pennylane-grover-hw-000-10 | 10 | 0.912 | 0.955 | 10 | 0.955 | 0.953 | 0.936 | 10 | 0.936 |
| pennylane-grover-hw-001-11 | 11 | 0.945 | 0.912 | 11 | 0.912 | 0.945 | 0.889 | 11 | 0.889 |
| pennylane-grover-hw-002-0111 | 0111 | 0.123 | 0.053 | 1001 | 0.092 | 0.078 | 0.057 | 0000 | 0.094 |
| pennylane-grover-hw-003-0110 | 0110 | 0.129 | 0.051 | 0011 | 0.090 | 0.119 | 0.061 | 0010 | 0.084 |
| qiskit-grover-hw-000-10 | 10 | 0.930 | 0.959 | 10 | 0.959 | 0.957 | 0.930 | 10 | 0.930 |
| qiskit-grover-hw-001-11 | 11 | 0.930 | 0.936 | 11 | 0.936 | 0.945 | 0.891 | 11 | 0.891 |
| qiskit-grover-hw-002-0111 | 0111 | 0.188 | 0.066 | 1010 | 0.094 | 0.262 | 0.047 | 0011 | 0.088 |
| qiskit-grover-hw-003-0110 | 0110 | 0.145 | 0.070 | 0010 | 0.088 | 0.346 | 0.064 | 0011 | 0.086 |
| readout-grover-hw-000-10 | 10 | 0.945 | 0.980 | 10 | 0.980 | 0.959 | 0.979 | 10 | 0.979 |
| readout-grover-hw-001-11 | 11 | 0.957 | 0.924 | 11 | 0.924 | 0.951 | 0.967 | 11 | 0.967 |
| readout-grover-hw-002-0111 | 0111 | 0.922 | 0.932 | 0111 | 0.932 | 0.930 | 0.906 | 0111 | 0.906 |
| readout-grover-hw-003-0110 | 0110 | 0.936 | 0.920 | 0110 | 0.920 | 0.938 | 0.961 | 0110 | 0.961 |

## Table 8 -- Cepheus rerun (25 Sep): CDKM adder cases + Toffoli jobs A/B, next to the original runs (before 7 Sep)

Source: `experiments/results/open_quantum_cepheus_discovery/repeat_probe/plan.json`, `experiments/results/open_quantum_cepheus_discovery/repeat_probe/collected.json`, `experiments/results/open_quantum_cepheus_discovery/toffoli_probe/results/raw_counts.json`, `experiments/results/open_quantum_cepheus_discovery/toffoli_probe/results/jobs_summary.csv`, `experiments/results/open_quantum_cepheus_discovery/rerun_2026-09-25/open_quantum/job_ids.json`.

Bit order: Counts are q0_left throughout (checked against each source's own `bit_order` field; toffoli_probe's raw_counts_littleendian is reversed, as in Table 4). `decoded_top` decodes little-endian over `arithmetic.result_qubits` for the CDKM cases only (as in Table 3); Toffoli rows leave it blank since the full 3-qubit outcome is the whole answer. The original-run CDKM rows are repeat_probe's 4 runs per case; the original-run Toffoli rows use toffoli_probe's raw_counts.json (jobs_summary.csv, Table 4's source, does not carry per-outcome counts). The original runs predate 7 Sep 2026.

| circuit_case | era | expected_full | expected_full_count | top | p_top | decoded_top |
|---|---|---|---|---|---|---|
| cdkm@3+0 | original_run1 | 111100 | 3 | 111010 | 0.484 | 5 |
| cdkm@3+0 | original_run2 | 111100 | 2 | 111010 | 0.436 | 5 |
| cdkm@3+0 | original_run3 | 111100 | 0 | 111010 | 0.465 | 5 |
| cdkm@3+0 | original_run4 | 111100 | 2 | 111010 | 0.475 | 5 |
| cdkm@3+0 | 25sep | 111100 | 0 | 111010 | 0.654 | 5 |
| cdkm@1+3 | original_run1 | 100010 | 27 | 100000 | 0.748 | 0 |
| cdkm@1+3 | original_run2 | 100010 | 22 | 100000 | 0.793 | 0 |
| cdkm@1+3 | original_run3 | 100010 | 20 | 100000 | 0.760 | 0 |
| cdkm@1+3 | original_run4 | 100010 | 23 | 100000 | 0.770 | 0 |
| cdkm@1+3 | 25sep | 100010 | 17 | 100000 | 0.711 | 0 |
| A_toffoli_raw_ccx | original | 111 | 303 | 111 | 0.592 |  |
| A_toffoli_raw_ccx | 25sep | 111 | 304 | 111 | 0.594 |  |
| B_toffoli_decomposed | original | 111 | 19 | 110 | 0.873 |  |
| B_toffoli_decomposed | 25sep | 111 | 18 | 110 | 0.867 |  |

## Table 9 -- Quantum Inspire Rx-sign probe, 20 Sep (843300) vs 25 Sep (848824)

Source: `experiments/results/quantum_inspire_rx/probe-843300.json`, `experiments/results/quantum_inspire_rx/probe-848824.json`.

Bit order: Same convention as Table 5 for both runs.

| circuit | count_0_843300 | count_1_843300 | p_standard_843300 | p_negated_843300 | count_0_848824 | count_1_848824 | p_standard_848824 | p_negated_848824 |
|---|---|---|---|---|---|---|---|---|
| rx_sign_probe | 67 | 957 | 0.065 | 0.935 | 19 | 1005 | 0.019 | 0.981 |
| rx_sign_probe_neg | 922 | 102 | 0.100 | 0.900 | 1014 | 10 | 0.010 | 0.990 |
| ry_sign_control | 924 | 100 | 0.902 | 0.098 | 1015 | 9 | 0.991 | 0.009 |
| rx_pi_control | 49 | 975 | 0.952 | 0.952 | 15 | 1009 | 0.985 | 0.985 |
| idle_control | 950 | 74 | 0.928 | 0.928 | 1018 | 6 | 0.994 | 0.994 |

## Table 10 -- Quantum Inspire native-pulse probe (25 Sep follow-up)

Source: `experiments/results/quantum_inspire_rx/followup_2026-09-25/native-1474757.json`.

Bit order: Same convention as Table 5 (single-qubit outcomes).

| circuit | count_0 | count_1 | p_standard | p_negated |
|---|---|---|---|---|
| rx_pos | 20 | 1004 | 0.020 | 0.980 |
| rx_neg | 1015 | 9 | 0.009 | 0.991 |
| x90 | 17 | 1007 | 0.017 | 0.983 |
| mx90 | 1015 | 9 | 0.009 | 0.991 |

## Table 11 -- Quantum Inspire Cirq-vs-Qiskit Rx diagnostic (25 Sep follow-up)

Source: `experiments/results/quantum_inspire_rx/followup_2026-09-25/cirq-diagnostic-848830.json`, `experiments/results/quantum_inspire_rx/followup_2026-09-25/cirq-diagnostic-848831.json`.

Bit order: 2-qubit outcomes from each result's `counts` field (already q0-left; each file also carries a `raw_counts_q0_right` field, not used here). `expected_standard_rx`/`expected_negated_rx` are per-job, relative to that job's marked state.

| job_id | marked_state | circuit | top | p_top | p_standard | p_negated |
|---|---|---|---|---|---|---|
| 848830 | 10 | cirq_10 | 01 | 0.939 | 0.009 | 0.939 |
| 848830 | 10 | cirq_10_negrx | 10 | 0.962 | 0.007 | 0.962 |
| 848830 | 10 | qiskit_10 | 10 | 0.932 | 0.932 | 0.932 |
| 848831 | 11 | cirq_11 | 00 | 0.943 | 0.011 | 0.943 |
| 848831 | 11 | cirq_11_negrx | 11 | 0.923 | 0.014 | 0.923 |
| 848831 | 11 | qiskit_11 | 11 | 0.933 | 0.933 | 0.933 |

## Table 12 -- Recovered 12-13 Sep 2026 Tuna-17 diagnostic batches (read back from the Quantum Inspire API)

Source: `experiments/results/quantum_inspire_rx/recovered_2026-09-12-13/batch-835027.json`, `experiments/results/quantum_inspire_rx/recovered_2026-09-12-13/batch-835548.json`, `experiments/results/quantum_inspire_rx/recovered_2026-09-12-13/batch-835555.json`, `experiments/results/quantum_inspire_rx/recovered_2026-09-12-13/batch-835559.json`, `experiments/results/quantum_inspire_rx/recovered_2026-09-12-13/batch-835568.json`.

Bit order: `top` is `counts_as_returned` as-is (API convention: b[0] rightmost, verified against job 1441798's `X q[1]` readout baseline: ideal and returned top both '01'). `ideal`/`ideal_p` come from a stdlib statevector simulation of each job's own cQASM (see `simulate_ideal`), formatted the same way. `role` is only filled in where the coordinator specified it; everything else is "not recorded" rather than guessed. `verdict` compares `top` to that job's own `ideal` -- for a job with negated rotation angles, its own ideal is generally not the same bitstring as the un-negated version's ideal (see the report's notes on job 1442931). `ideal_rx_negated` re-simulates the same job with every Rx angle negated and everything else unchanged; `matches_rx_negated` is whether `top` equals that; `rx_sign_sensitive` is whether negating Rx would even change the ideal outcome for this particular job (it does not for e.g. two stacked Rx(pi/2) gates, which compose to Rx(pi) either way up to an unobservable global phase).

| batch_job_id | batch_created_on | job_id | role | gates | shots | top | p_top | ideal | ideal_p | verdict | ideal_rx_negated | matches_rx_negated | rx_sign_sensitive |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 835027 | 2026-09-12 14:57 UTC | 1441797 | not recorded | Rx:2 Ry:4 Rz:6 CR:2 | 1024 | 10 | 0.948 | 01 | 1.000 | complement of ideal | 10 | yes | yes |
| 835027 | 2026-09-12 14:57 UTC | 1441798 | readout baseline X q[1] | X:1 | 1024 | 01 | 0.945 | 01 | 1.000 | matches ideal | 01 | yes | no |
| 835027 | 2026-09-12 14:57 UTC | 1441799 | not recorded | H:1 Ry:3 Rz:3 CNOT:2 | 1024 | 01 | 0.945 | 01 | 1.000 | matches ideal | 01 | yes | no |
| 835027 | 2026-09-12 14:57 UTC | 1441800 | not recorded | H:1 Z:1 Ry:4 CNOT:1 CZ:1 | 1024 | 01 | 0.938 | 01 | 1.000 | matches ideal | 01 | yes | no |
| 835027 | 2026-09-12 14:57 UTC | 1441801 | not recorded | H:1 Ry:3 Rz:3 CNOT:2 | 1024 | 01 | 0.948 | 01 | 1.000 | matches ideal | 01 | yes | no |
| 835548 | 2026-09-13 01:53 UTC | 1442878 | not recorded | Rx:2 Ry:4 Rz:6 CZ:2 | 1024 | 00 | 0.956 | 11 | 1.000 | complement of ideal | 00 | yes | yes |
| 835548 | 2026-09-13 01:53 UTC | 1442879 | not recorded | Rx:2 Ry:4 Rz:6 CR:2 | 1024 | 10 | 0.945 | 01 | 1.000 | complement of ideal | 10 | yes | yes |
| 835548 | 2026-09-13 01:53 UTC | 1442880 | not recorded | Rx:2 Ry:4 Rz:6 CZ:2 | 1024 | 10 | 0.926 | 01 | 1.000 | complement of ideal | 10 | yes | yes |
| 835548 | 2026-09-13 01:53 UTC | 1442881 | not recorded | Rx:2 Ry:4 Rz:6 CR:2 | 1024 | 00 | 0.940 | 11 | 1.000 | complement of ideal | 00 | yes | yes |
| 835555 | 2026-09-13 01:58 UTC | 1442888 | not recorded | Rx:2 | 1024 | 1 | 0.954 | 1 | 1.000 | matches ideal | 1 | yes | no |
| 835555 | 2026-09-13 01:58 UTC | 1442889 | not recorded | Rx:1 | 1024 | 1 | 0.973 | 1 | 1.000 | matches ideal | 1 | yes | no |
| 835555 | 2026-09-13 01:58 UTC | 1442890 | not recorded | Ry:1 | 1024 | 1 | 0.966 | 1 | 1.000 | matches ideal | 1 | yes | no |
| 835555 | 2026-09-13 01:58 UTC | 1442891 | not recorded | X:1 | 1024 | 1 | 0.975 | 1 | 1.000 | matches ideal | 1 | yes | no |
| 835555 | 2026-09-13 01:58 UTC | 1442892 | not recorded | Ry:2 | 1024 | 1 | 0.959 | 1 | 1.000 | matches ideal | 1 | yes | no |
| 835559 | 2026-09-13 02:00 UTC | 1442896 | not recorded | Rx:2 Ry:4 Rz:6 CR:2 | 1024 | 00 | 0.928 | 11 | 1.000 | complement of ideal | 00 | yes | yes |
| 835559 | 2026-09-13 02:00 UTC | 1442897 | not recorded | Rx:2 Ry:4 Rz:4 CR:2 | 1024 | 00 | 0.946 | 11 | 1.000 | complement of ideal | 00 | yes | yes |
| 835559 | 2026-09-13 02:00 UTC | 1442898 | not recorded | H:1 Ry:3 Rz:1 CNOT:2 | 1024 | 11 | 0.929 | 11 | 1.000 | matches ideal | 11 | yes | no |
| 835568 | 2026-09-13 02:03 UTC | 1442927 | Cirq, marked 11, as sent (contains Rx) | Rx:2 Ry:4 Rz:6 CR:2 | 1024 | 00 | 0.944 | 11 | 1.000 | complement of ideal | 00 | yes | yes |
| 835568 | 2026-09-13 02:03 UTC | 1442928 | Qiskit, marked 11 (no Rx) | H:1 Ry:3 Rz:1 CNOT:2 | 1024 | 11 | 0.885 | 11 | 1.000 | matches ideal | 11 | yes | no |
| 835568 | 2026-09-13 02:03 UTC | 1442929 | probe Ry(pi/2) Rz(pi/2) Rx(pi/2) | Rx:1 Ry:1 Rz:1 | 1024 | 1 | 0.932 | 0 | 1.000 | complement of ideal | 1 | yes | yes |
| 835568 | 2026-09-13 02:03 UTC | 1442930 | probe Rx(pi/2) Rz(pi/2) Ry(pi/2) | Rx:1 Ry:1 Rz:1 | 1024 | 0 | 0.986 | 1 | 1.000 | complement of ideal | 0 | yes | yes |
| 835568 | 2026-09-13 02:03 UTC | 1442931 | same program with both Rx angles negated | Rx:2 Ry:4 Rz:6 CR:2 | 1024 | 11 | 0.930 | 00 | 1.000 | complement of ideal | 11 | yes | yes |

Of the 11 jobs whose ideal outcome depends on the sign of Rx, 11 returned the Rx-negated outcome; of the 11 jobs whose outcome does not depend on it, 11 matched the ideal.

22 of all 22 jobs returned the Rx-negated ideal outcome (`top == ideal_rx_negated`).

Job 1442931's program already has both Rx angles negated relative to 1442927, so its own ideal is '00'; the returned '11' is 1442927's (the original Cirq circuit's) marked state -- what a device that negates Rx produces from the negated program.

