"""Outward connector adapters.

`slack` is the first surface that can reach a real third-party endpoint. Every
other connector in this repository stays what the README already says it is: a
deterministic local adapter, not a claim of access to a vendor tenant.
"""
from __future__ import annotations

__all__ = ["slack"]
