"""Role components load independently; control imports no media engine."""

from importlib import import_module

__all__ = ["MultiCameraManager", "VehicleControlClient"]


def __getattr__(name):
    modules = {
        "VehicleControlClient": ".control_client",
        "MultiCameraManager": ".multi_camera_manager",
    }
    if name not in modules:
        raise AttributeError(name)
    return getattr(import_module(modules[name], __name__), name)
