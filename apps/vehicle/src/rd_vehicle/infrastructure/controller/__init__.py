"""Device dependencies are imported only when the selected adapter is requested."""

from importlib import import_module

__all__ = ["MockController", "RCLpyCmdVelController", "SerialController"]


def __getattr__(name):
    modules = {
        "SerialController": ".serial_controller",
        "RCLpyCmdVelController": ".ros2_cmd_vel_controller",
        "MockController": ".mock_controller",
    }
    if name not in modules:
        raise AttributeError(name)
    return getattr(import_module(modules[name], __name__), name)
