"""Strict immutable configuration, without network/device-library imports."""

import dataclasses
import json
import math
import os
import types
from collections.abc import Mapping
from itertools import pairwise
from pathlib import Path
from typing import Literal, Union, get_args, get_origin, get_type_hints

import yaml


class ConfigError(ValueError):
    pass


class _Loader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        # Configuration is a small tree, not an alias-expanded object graph.
        if self.check_event(yaml.AliasEvent):
            raise ConfigError("YAML aliases are not supported")
        self._depth = getattr(self, "_depth", 0) + 1
        self._nodes = getattr(self, "_nodes", 0) + 1
        if self._depth > 32 or self._nodes > 10000:
            raise ConfigError("configuration is too complex")
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ConfigError("configuration keys must be unique strings")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _parse(text):
    try:
        return yaml.load(text, Loader=_Loader)
    except (yaml.YAMLError, TypeError, ValueError):
        # YAML diagnostics can contain a secret from the offending source line.
        raise ConfigError("invalid YAML or duplicate configuration key") from None


def _decode(schema, value, path, base):
    origin, arguments = get_origin(schema), get_args(schema)
    if origin in (Union, types.UnionType):
        for choice in arguments:
            try:
                return _decode(choice, value, path, base)
            except ConfigError:
                pass
        raise ConfigError(f"{path}: incompatible value type")
    if dataclasses.is_dataclass(schema):
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: expected a mapping")
        fields = {field.name: field for field in dataclasses.fields(schema)}
        for key in value:
            if key not in fields:
                raise ConfigError(f"{path}.{key}: unknown field")
        hints = get_type_hints(schema)
        decoded = {
            key: _decode(hints[key], item, f"{path}.{key}", base)
            for key, item in value.items()
        }
        try:
            return schema(**decoded)
        except (TypeError, ValueError) as error:
            raise ConfigError(f"{path}: {error}") from None
    if origin is Literal:
        if not any(
            type(value) is type(choice) and value == choice for choice in arguments
        ):
            raise ConfigError(f"{path}: unsupported option")
        return value
    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"{path}: expected an array")
        return tuple(
            _decode(arguments[0], item, f"{path}[{index}]", base)
            for index, item in enumerate(value)
        )
    if schema is Path:
        if not isinstance(value, (str, Path)) or not str(value):
            raise ConfigError(f"{path}: expected a filesystem path")
        candidate = Path(value).expanduser()
        return (
            (base / candidate).resolve()
            if not candidate.is_absolute()
            else candidate.resolve()
        )
    if schema is float:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ConfigError(f"{path}: expected a finite number")
        return float(value)
    if schema in (str, int, bool, type(None)):
        if type(value) is not schema:
            raise ConfigError(f"{path}: expected {schema.__name__}")
        return value
    raise ConfigError(f"{path}: unsupported configuration type")


def _leaves(value, prefix=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, f"{prefix}.{key}" if prefix else key)
    else:
        yield prefix


def _merge(target, overlay, origins, source, prefix=""):
    for key, value in overlay.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value, origins, source, path)
        else:
            target[key] = value
            for leaf in list(origins):
                if leaf == path or leaf.startswith(path + "."):
                    del origins[leaf]
            for leaf in _leaves(value, path):
                origins[leaf] = source


def _overlay(values):
    result = {}
    paths = sorted(values)
    if any(right.startswith(left + ".") for left, right in pairwise(paths)):
        raise ConfigError("conflicting configuration overrides")
    for path, value in values.items():
        keys = path.split(".")
        if any(not key or not key.isidentifier() for key in keys):
            raise ConfigError("invalid configuration field path")
        branch = result
        for key in keys[:-1]:
            child = branch.setdefault(key, {})
            if not isinstance(child, dict):
                raise ConfigError(f"{path}: conflicting overrides")
            branch = child
        branch[keys[-1]] = value
    return result


@dataclasses.dataclass(frozen=True)
class LoadedConfig[T]:
    value: T
    sources: Mapping[str, str]

    def as_json(self):
        # Models contain secret references, never the resolved secret values.
        return json.dumps(
            {"config": dataclasses.asdict(self.value), "sources": dict(self.sources)},
            default=str,
            ensure_ascii=False,
            indent=2,
        )


def load_config[T](
    schema: type[T],
    role: str,
    *,
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> LoadedConfig[T]:
    environment = os.environ if environ is None else environ
    selected = path if path is not None else environment.get("RD_CONFIG")
    selected = Path(selected).expanduser().resolve() if selected else None
    base = selected.parent if selected else Path.cwd()
    raw = dataclasses.asdict(schema())
    sources = {leaf: "default" for leaf in _leaves(raw)}
    if selected is not None:
        try:
            if selected.stat().st_size > 1_048_576:
                raise ConfigError("configuration file exceeds 1 MiB")
            text = selected.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise ConfigError("cannot read UTF-8 configuration file") from None
        contents = _parse(text)
        if not isinstance(contents, dict):
            raise ConfigError("configuration file must be a mapping")
        _merge(raw, contents, sources, "file")
    prefix = "RD_" + role.upper() + "__"
    env_values = {
        key[len(prefix) :].lower().replace("__", "."): _parse(value)
        for key, value in environment.items()
        if key.startswith(prefix)
    }
    _merge(raw, _overlay(env_values), sources, "environment")
    _merge(raw, _overlay(overrides or {}), sources, "cli")
    result = _decode(schema, raw, role, base)
    validate = getattr(result, "validate", None)
    if validate is not None:
        try:
            validate()
        except ValueError as error:
            raise ConfigError(f"{role}: {error}") from None
    return LoadedConfig(result, types.MappingProxyType(sources))


def cli_overrides(values: list[str] | None) -> dict:
    result = {}
    for value in values or ():
        path, separator, raw = value.partition("=")
        if not separator or path in result:
            raise ConfigError("--set requires unique field=value entries")
        result[path] = _parse(raw)
    return result
