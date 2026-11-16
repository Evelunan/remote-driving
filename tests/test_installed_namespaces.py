import os
import subprocess
import sys


def test_applications_coexist_without_import_side_effects(tmp_path):
    code = """
import logging, os, sys
# Native OpenCV itself adjusts dynamic-library and Qt paths. Check application
# side effects after loading that dependency, separately from native packaging.
import cv2
before = dict(os.environ)
handlers = list(logging.getLogger().handlers)
import rd_driving.main_window
import rd_vehicle.main
import rd_server.main
assert before == dict(os.environ)
assert handlers == list(logging.getLogger().handlers)
assert not {'config', 'common', 'logger', 'infrastructure', 'interfaces'} & set(sys.modules)
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())
