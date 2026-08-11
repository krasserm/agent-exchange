"""E2E test package.

Exists so the modules here can import each other package-relative
(``from .helpers import ...``). Deliberately empty otherwise: the state-root
isolation these tests require now lives in ``tests/conftest.py``, which pytest
imports on every run rather than only when it collects this package.
"""
