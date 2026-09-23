set shell := ["bash", "-cu"]

python := ".venv/bin/python"

test:
    PYTHONPATH=nifi_extensions_matrix:experiments:tools {{python}} -m pytest --tb=short -q tests
