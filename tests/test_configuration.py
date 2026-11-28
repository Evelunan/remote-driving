import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from rd_core.configuration import ConfigError, cli_overrides, load_config
from rd_core.settings import DrivingSettings, ServerSettings, VehicleSettings

ROOT = Path(__file__).resolve().parents[1]


def test_precedence_sources_and_independent_instances(tmp_path):
    path = tmp_path / "vehicle.yaml"
    path.write_text("motion:\n  max_linear_mps: 1.0\n  max_angular_radps: 0.6\n")
    first = load_config(
        VehicleSettings,
        "vehicle",
        path=path,
        environ={"RD_VEHICLE__MOTION__MAX_LINEAR_MPS": "2.0"},
        overrides={"motion.max_linear_mps": 3.0},
    )
    second = load_config(VehicleSettings, "vehicle", path=path, environ={})
    assert first.value.motion.max_linear_mps == 3
    assert second.value.motion.max_linear_mps == 1
    assert first.sources["motion.max_linear_mps"] == "cli"
    assert first.sources["motion.max_angular_radps"] == "file"
    assert first.sources["control.send_hz"] == "default"
    with pytest.raises(FrozenInstanceError):
        first.value.motion.max_linear_mps = 4
    with pytest.raises(TypeError):
        first.sources["motion.max_linear_mps"] = "wrong"


def test_vehicle_lan_routes_ros2_commands_to_remote_topic():
    loaded = load_config(
        VehicleSettings,
        "vehicle",
        path=ROOT / "configs/vehicle.lan.yaml",
        environ={},
    )
    assert loaded.value.ros2.cmd_vel_topic == "/cmd_vel_remote"
    assert loaded.sources["ros2.cmd_vel_topic"] == "file"


def test_environment_only_applies_to_its_role_and_paths_follow_config(tmp_path):
    path = tmp_path / "driving.yaml"
    path.write_text("network:\n  quic_ca_file: tls/ca.pem\n")
    loaded = load_config(
        DrivingSettings,
        "driving",
        path=path,
        environ={"RD_VEHICLE__UNKNOWN": "[", "RD_DRIVING__CONTROL__SEND_HZ": "40"},
    )
    assert loaded.value.network.quic_ca_file == tmp_path / "tls/ca.pem"
    assert loaded.value.control.send_hz == 40
    assert loaded.sources["control.send_hz"] == "environment"


def test_arrays_replace_and_are_deeply_immutable(tmp_path):
    path = tmp_path / "vehicle.yaml"
    path.write_text(
        "video:\n  primary_view: back\ncameras:\n  - name: back\n    source: /dev/v4l/by-id/test\n    fps: 20\n"
    )
    loaded = load_config(VehicleSettings, "vehicle", path=path, environ={})
    assert [item.name for item in loaded.value.cameras] == ["back"]
    assert isinstance(loaded.value.cameras, tuple)
    assert loaded.value.cameras[0].source == "/dev/v4l/by-id/test"
    with pytest.raises(FrozenInstanceError):
        loaded.value.cameras[0].fps = 30


@pytest.mark.parametrize(
    "contents,match",
    [
        ("motion:\n  typo: 5", "motion.typo"),
        ("control:\n  send_hz: false", "control.send_hz"),
        ("motion:\n  max_linear_mps: .nan", "max_linear_mps"),
        ("motion:\n  max_linear_mps: -1", "max_linear_mps"),
        ("network:\n  quic_port: 70000", "port"),
        ("cameras: [{name: front}, {name: front}]", "camera names"),
        ("cameras: [{width: 641}]", "dimensions"),
        ("controller: unknown", "controller"),
        ("controller: none\ncontroller: ros2", "duplicate"),
        ("ros2:\n  cmd_vel_topic: ''", "cmd_vel_topic"),
        ("ros2:\n  cmd_vel_topic: cmd_vel_remote", "cmd_vel_topic"),
        ("ros2:\n  cmd_vel_topic: /cmd-vel", "cmd_vel_topic"),
        ("ros2:\n  cmd_vel_topic: /cmd//vel", "cmd_vel_topic"),
    ],
)
def test_invalid_configuration_fails_with_field_path(tmp_path, contents, match):
    path = tmp_path / "invalid.yaml"
    path.write_text(contents)
    with pytest.raises(ConfigError, match=match):
        load_config(VehicleSettings, "vehicle", path=path, environ={})


def test_inactive_module_needs_no_tls_pair_but_unknown_fields_are_rejected():
    loaded = load_config(
        ServerSettings,
        "server",
        environ={},
        overrides={
            "services": ["signaling"],
            "allow_legacy_development": True,
            "relay.tls.cert": "unused.pem",
        },
    )
    assert loaded.value.relay.tls.key is None
    with pytest.raises(ConfigError, match="relay.typo"):
        load_config(
            ServerSettings,
            "server",
            environ={},
            overrides={
                "services": ["signaling"],
                "allow_legacy_development": True,
                "relay.typo": 1,
            },
        )


def test_secret_references_not_values_appear_in_dump_or_errors(tmp_path):
    secret = "secret-value-that-must-not-appear-anywhere"
    loaded = load_config(DrivingSettings, "driving", environ={"RD_TOKEN": secret})
    dumped = loaded.as_json()
    assert secret not in dumped
    assert json.loads(dumped)["config"]["credentials"]["token_env"] == "RD_TOKEN"
    path = tmp_path / "bad.yaml"
    path.write_text("credentials: [" + secret)
    with pytest.raises(ConfigError) as error:
        load_config(DrivingSettings, "driving", path=path, environ={})
    assert secret not in str(error.value)


def test_cli_set_has_strict_duplicate_handling():
    assert cli_overrides(["control.send_hz=25", "ui.debug=false"]) == {
        "control.send_hz": 25,
        "ui.debug": False,
    }
    with pytest.raises(ConfigError):
        cli_overrides(["ui.debug=false", "ui.debug=true"])


@pytest.mark.parametrize(
    "text", ["cameras: &a [*a]", "x: " + "[" * 40 + "1" + "]" * 40]
)
def test_yaml_aliases_and_excessive_nesting_are_rejected(tmp_path, text):
    path = tmp_path / "complex.yaml"
    path.write_text(text)
    with pytest.raises(ConfigError):
        load_config(VehicleSettings, "vehicle", path=path, environ={})


@pytest.mark.parametrize(
    "overrides", [{"ui": {}, "ui.debug": True}, {"ui.debug": True, "ui": {}}]
)
def test_parent_child_overrides_are_rejected_in_either_order(overrides):
    with pytest.raises(ConfigError, match="conflicting"):
        load_config(DrivingSettings, "driving", overrides=overrides, environ={})
