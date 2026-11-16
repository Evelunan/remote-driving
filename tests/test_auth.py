import pytest
from rd_core.auth import AuthorizationError, AuthProvider
from rd_core.configuration import ConfigError
from rd_core.settings import AuthSettings, PrincipalSettings


def test_authenticated_identity_role_and_resource_scopes_are_server_owned():
    settings = AuthSettings(
        (
            PrincipalSettings(
                "operator", "driving", "OPERATOR_TOKEN", (42,), ("camera-42",)
            ),
            PrincipalSettings("viewer", "viewer", "VIEWER_TOKEN", (), ("camera-42",)),
        )
    )
    provider = AuthProvider(
        settings, environ={"OPERATOR_TOKEN": "a" * 40, "VIEWER_TOKEN": "b" * 40}
    )
    principal = provider.authenticate("a" * 40)
    principal.check_session(42, role="driving")
    principal.check_room("camera-42")
    with pytest.raises(AuthorizationError):
        principal.check_session(42, role="vehicle")
    with pytest.raises(AuthorizationError):
        principal.check_session(99)
    with pytest.raises(AuthorizationError):
        principal.check_room("camera-99")
    viewer = provider.authenticate("b" * 40)
    viewer.check_room("camera-42")
    with pytest.raises(AuthorizationError):
        viewer.check_session(42, role="driving")


def test_invalid_and_expired_tokens_are_indistinguishable_and_not_logged():
    secret = "c" * 40
    provider = AuthProvider(
        AuthSettings(
            (
                PrincipalSettings(
                    "car",
                    "vehicle",
                    "CAR_TOKEN",
                    (42,),
                    ("42",),
                    "2026-01-01T00:00:00+00:00",
                ),
            )
        ),
        environ={"CAR_TOKEN": secret},
    )
    assert provider.authenticate(secret, now=0).id == "car"
    errors = []
    for token in (secret, "d" * 40, "short"):
        with pytest.raises(AuthorizationError) as error:
            provider.authenticate(token, now=2_000_000_000)
        errors.append(str(error.value))
        assert secret not in str(error.value)
    assert len(set(errors)) == 1
    assert secret not in repr(provider._credentials)


def test_duplicate_or_missing_credentials_fail_before_listening():
    settings = AuthSettings(
        (
            PrincipalSettings("first", "vehicle", "FIRST"),
            PrincipalSettings("second", "driving", "SECOND"),
        )
    )
    with pytest.raises(ConfigError, match="missing credential"):
        AuthProvider(settings, environ={})
    with pytest.raises(ConfigError, match="distinct"):
        AuthProvider(settings, environ={"FIRST": "x" * 40, "SECOND": "x" * 40})
