"""Hermes plugin entry point. The code lives in ``paseo_bridge/`` so it can be tested without Hermes."""
try:
    from .paseo_bridge.plugin import register
except ImportError:  # imported as a top-level module (pytest collecting the repo root), not by Hermes
    from paseo_bridge.plugin import register

__all__ = ["register"]
