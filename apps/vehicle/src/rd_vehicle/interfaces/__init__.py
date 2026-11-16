"""Resolve interfaces without loading camera dependencies in control hosts."""

from importlib import import_module

__all__ = ["ICamera", "IController"]


def __getattr__(name):
    modules = {"ICamera": ".icamera", "IController": ".icontroller"}
    if name not in modules:
        raise AttributeError(name)
    return getattr(import_module(modules[name], __name__), name)
