"""Hermes plugin entry point. The code lives in ``paseo_bridge/`` so it can be tested without Hermes."""
from .paseo_bridge.plugin import register

__all__ = ["register"]
