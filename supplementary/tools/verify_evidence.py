#!/usr/bin/env python3
"""Independently check the captured NiFi outputs against the small problem instances."""
import itertools
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check():
    results = []
    for name, expected, verdict in [('dj-balanced', '101', 'balanced'),
                                    ('dj-constant', '000', 'constant'),
                                    ('bv-secret', '1011', None)]:
        data = json.loads((ROOT / 'evidence' / (name + '.json')).read_text())
        dist, attrs = data['distribution'], data['attributes']
        assert math.isclose(sum(dist.values()), 1.0, abs_tol=1e-8)
        assert dist.get(expected, 0) > 0.99, (name, dist)
        if verdict:
            assert attrs['dj.verdict'] == verdict
        else:
            assert attrs['bv.found_bitstring'] == expected and attrs['bv.match'] == 'true'
        results.append({'id': name, 'passed': True, 'check': f'Expected outcome {expected}',
                        'probability': dist[expected]})

    spec = json.loads((ROOT / 'flows/specifications.json').read_text())[2]
    props = spec['lanes'][0]['steps'][0]['properties']
    mu = json.loads(props['Expected Returns'])
    sigma = json.loads(props['Covariance Matrix'])
    risk, penalty, budget = float(props['Risk Factor']), float(props['Penalty Factor']), int(props['Budget'])
    states = []
    for x in itertools.product([0, 1], repeat=len(mu)):
        cost = risk * sum(x[i] * sigma[i][j] * x[j] for i in range(len(mu)) for j in range(len(mu)))
        cost -= sum(x[i] * mu[i] for i in range(len(mu)))
        cost += penalty * (sum(x) - budget) ** 2
        states.append({'bits': ''.join(map(str, x)), 'cost': cost, 'feasible': sum(x) == budget})
    optimum = min(states, key=lambda row: row['cost'])
    data = json.loads((ROOT / 'evidence/portfolio.json').read_text())
    dist, attrs = data['distribution'], data['attributes']
    assert all(len(bits) == len(mu) and set(bits) <= {'0', '1'} and p >= 0 for bits, p in dist.items())
    assert math.isclose(sum(dist.values()), 1.0, abs_tol=1e-8)
    best = min((row for row in states if dist.get(row['bits'], 0) > 0), key=lambda row: row['cost'])
    assert attrs['qaoa.best_measurement'] == best['bits']
    assert math.isclose(float(attrs['qaoa.best_value']), best['cost'], abs_tol=1e-7)
    assert math.isclose(float(attrs['qaoa.exact_minimum']), optimum['cost'], abs_tol=1e-7)
    energy = sum(row['cost'] * dist.get(row['bits'], 0) for row in states)
    assert math.isclose(float(attrs['qaoa.optimal_value']), energy, abs_tol=1e-7)
    feasible_mass = sum(dist.get(row['bits'], 0) for row in states if row['feasible'])
    results.append({'id': 'portfolio', 'passed': True,
                    'check': 'Enumerate all eight portfolios; verify reported best sample, exact minimum, and mean cost',
                    'exact_optimum': optimum, 'best_sample': best,
                    'optimum_probability': dist.get(optimum['bits'], 0),
                    'feasible_probability': feasible_mass, 'mean_cost': energy,
                    'states': states})
    reference = json.loads((ROOT / 'evidence/classiq-reference.json').read_text())
    edges = reference['edges']
    data = json.loads((ROOT / 'evidence/mis-classiq-reference.json').read_text())
    dist, attrs = data['distribution'], data['attributes']
    candidates = []
    for x in itertools.product([0, 1], repeat=len(reference['nodes'])):
        conflicts = sum(x[u] * x[v] for u, v in edges)
        candidates.append({'bits': ''.join(map(str, x)), 'size': sum(x),
                           'conflicts': conflicts, 'cost': -sum(x) + 2 * conflicts})
    independent = [row for row in candidates if row['conflicts'] == 0]
    max_size = max(row['size'] for row in independent)
    optima = [row['bits'] for row in independent if row['size'] == max_size]
    assert all(len(bits) == 8 and set(bits) <= {'0', '1'} and p >= 0 for bits, p in dist.items())
    assert math.isclose(sum(dist.values()), 1, abs_tol=1e-8)
    best_cost = min(row['cost'] for row in candidates if dist.get(row['bits'], 0) > 0)
    reported = next(row for row in candidates if row['bits'] == attrs['qaoa.best_measurement'])
    assert reported['cost'] == best_cost
    assert math.isclose(float(attrs['qaoa.best_value']), best_cost, abs_tol=1e-7)
    assert math.isclose(float(attrs['qaoa.exact_minimum']), -max_size, abs_tol=1e-7)
    mean = sum(row['cost'] * dist.get(row['bits'], 0) for row in candidates)
    assert math.isclose(float(attrs['qaoa.optimal_value']), mean, abs_tol=1e-7)
    results.append({'id': 'mis-classiq-reference', 'passed': True,
                    'check': 'Enumerate all 256 vertex subsets; check independence, maximum size, and reported objective values',
                    'maximum_size': max_size, 'optimal_bitstrings': optima,
                    'best_sample': reported, 'mean_cost': mean,
                    'optimal_probability': sum(dist.get(bits, 0) for bits in optima),
                    'independent_probability': sum(dist.get(row['bits'], 0) for row in independent),
                    'subsets': candidates})
    validation = json.loads((ROOT / 'evidence/validation.json').read_text())
    assert all(p['validationStatus'] == 'VALID' and p['state'] == 'STOPPED' for p in validation)
    report = {'passed': True, 'valid_stopped_processors': len(validation), 'checks': results}
    (ROOT / 'evidence/checks.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return report


if __name__ == '__main__':
    check()
