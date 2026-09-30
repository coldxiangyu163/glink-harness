"""ACP session selection: advertised values only, verified agent state only."""

from __future__ import annotations

import json
from typing import Any, Mapping

from .acp_rpc import REQUEST_TIMEOUT_SECONDS, AcpProtocolError, AcpRpcClient

_CONFIG_OPTIONS_LIMIT = 20
_CONFIG_VALUES_LIMIT = 100
_CONFIG_TEXT_LIMIT = 300


def project_config_options(options: Any) -> list[dict[str, Any]]:
    """Bound the advertised matrix carried into session_ready."""
    if not isinstance(options, list):
        return []
    projected: list[dict[str, Any]] = []
    for option in options[:_CONFIG_OPTIONS_LIMIT]:
        if not isinstance(option, Mapping):
            continue
        entry = {
            key: option[key]
            for key in ("id", "name", "type", "category", "currentValue")
            if key in option
        }
        if isinstance(option.get("description"), str):
            entry["description"] = option["description"][:_CONFIG_TEXT_LIMIT]
        values = option.get("options")
        if isinstance(values, list):
            entry["options"] = [
                value for value in values[:_CONFIG_VALUES_LIMIT]
                if isinstance(value, Mapping)
            ]
        projected.append(entry)
    return projected


def select_values(option: Mapping[str, Any]) -> tuple[str, ...]:
    """Selectable opaque ids, from either flat or grouped ACP options."""
    values: list[str] = []
    entries = option.get("options")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            grouped = entry.get("options")
            if isinstance(grouped, list):
                for inner in grouped:
                    if isinstance(inner, Mapping) and isinstance(inner.get("value"), str):
                        values.append(inner["value"])
            elif isinstance(entry.get("value"), str):
                values.append(entry["value"])
    return tuple(values)


def _control_options(options: Any) -> list[dict[str, Any]]:
    """The complete wire matrix, never a display projection."""
    return [entry for entry in options if isinstance(entry, dict)] if isinstance(options, list) else []


def _option(options: list[dict[str, Any]], config_id: str) -> dict[str, Any]:
    matches = [entry for entry in options if entry.get("id") == config_id]
    if len(matches) > 1:
        raise AcpProtocolError(f"ACP 配置选项 {config_id!r} 非唯一（重复 id）；拒绝自选首项")
    if matches:
        option = matches[0]
        if option.get("type") != "select":
            raise AcpProtocolError(
                f"ACP 配置选项 {config_id!r} type={option.get('type')!r}；仅支持显式 select"
            )
        return option
    raise AcpProtocolError(
        f"ACP agent 未提供配置选项 {config_id!r}；"
        "包清单 capabilities.session_config 不能被静默忽略"
    )


def _validate_select_value(option: Mapping[str, Any], config_id: str, value: str) -> None:
    values = select_values(option)
    if value not in values:
        raise AcpProtocolError(
            f"ACP 配置选项 {config_id!r} 不接受取值 {value!r}；"
            f"可选：{', '.join(values)}（协议要求先按 agent 声明取值）"
        )


def _declared_value(declaration: str, encoding: str, option: Mapping[str, Any]) -> str:
    if encoding == "value":
        return declaration
    if encoding != "dsh-model":
        raise AcpProtocolError(f"未知 ACP session_config encoding：{encoding!r}")
    # dsh modelValue = JSON.stringify([provider, model]). Match the advertised
    # opaque value; never split the model id (OpenRouter ids contain slashes).
    choices: dict[str, tuple[str, str]] = {}
    for value in select_values(option):
        try:
            route = json.loads(value)
        except (ValueError, TypeError):
            continue
        if isinstance(route, list) and len(route) == 2 and all(
            isinstance(part, str) and part for part in route
        ):
            choices[value] = (route[0], route[1])
    # A bare exact model id has priority over the explicit provider/model form.
    matches = [value for value, (_provider, model) in choices.items() if model == declaration]
    if not matches:
        matches = [value for value, (provider, model) in choices.items()
                   if f"{provider}/{model}" == declaration]
    if len(matches) != 1:
        raise AcpProtocolError(
            f"ACP 配置选项 {option.get('id')!r} 的 OPENAI_MODEL={declaration!r} "
            f"匹配 {len(matches)} 个广告路由；必须唯一匹配（可用 provider/model 消歧）"
        )
    return matches[0]


def apply_session_config(
    client: AcpRpcClient,
    session_id: str,
    declarations: tuple[tuple[str, str, str], ...],
    advertised: Any,
) -> list[dict[str, Any]]:
    """Validate full wire state; bound only the final display projection."""
    options = _control_options(advertised)
    expected: dict[str, str] = {}
    # Reasoning choices depend on the selected model, not TOML insertion order.
    # Stable sorting preserves other policy order while moving effort after model.
    ordered = sorted(declarations, key=lambda entry: entry[0] == "reasoning_effort")
    for config_id, declaration, encoding in ordered:
        option = _option(options, config_id)
        value = _declared_value(declaration, encoding, option)
        _validate_select_value(option, config_id, value)
        expected[config_id] = value
        if option.get("currentValue") != value:
            updated = client.request(
                "session/set_config_option",
                {"sessionId": session_id, "configId": config_id, "value": value},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            options = _control_options(updated.get("configOptions"))
        # Every mutation returns the complete state. A later selection must not
        # silently undo an earlier one (model and reasoning can be dependent).
        for applied_id, applied_value in expected.items():
            applied_option = _option(options, applied_id)
            _validate_select_value(applied_option, applied_id, applied_value)
            actual = applied_option.get("currentValue")
            if actual != applied_value:
                raise AcpProtocolError(
                    f"ACP 配置选项 {applied_id!r} currentValue={actual!r} "
                    f"与声明值 {applied_value!r} 不一致；拒绝静默回退"
                )
    return project_config_options(options)
