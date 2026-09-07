"""LiteLLM 服务入口与 OpenCode Go Responses 兼容层。

等价于 console script `litellm`，但可被 venv python 直接执行（不依赖 uv
trampoline，项目目录移动后仍可用）。OpenCode Go 对最终 outbound Responses
tool schema 要求所有 ``properties`` key 都出现在 ``required`` 中；这里在
LiteLLM 完成 provider 转换后再做一次仅限 OpenCode Go 的校正。
"""

from typing import Any

import litellm
from litellm.llms.openai.responses.transformation import OpenAIResponsesAPIConfig


def normalize_opencode_tool_schemas(data: dict[str, Any]) -> int:
    """Add every schema property name to ``required`` in-place.

    This runs after LiteLLM's OpenAI Responses transformation, so it also
    covers schemas that LiteLLM reconstructs after the inbound proxy pass.
    Only dictionaries containing JSON Schema ``properties`` are touched.
    """
    fixed = 0

    def visit(value: Any) -> None:
        nonlocal fixed
        if isinstance(value, list):
            for child in value:
                visit(child)
            return
        if not isinstance(value, dict):
            return

        properties = value.get("properties")
        if isinstance(properties, dict):
            required = value.get("required")
            normalized = (
                [name for name in required if isinstance(name, str)]
                if isinstance(required, list)
                else []
            )
            for name in properties:
                if name not in normalized:
                    normalized.append(name)
            if normalized != required:
                value["required"] = normalized
                fixed += 1

        for child in value.values():
            visit(child)

    visit(data)
    return fixed


def _custom_tool_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": "Raw input for this tool.",
            }
        },
        "required": ["input"],
        "additionalProperties": False,
    }


def normalize_opencode_custom_tools(data: dict[str, Any]) -> int:
    """Bridge custom declarations to function declarations for OpenCode Go."""
    converted = 0

    def visit(value: Any) -> None:
        nonlocal converted
        if isinstance(value, list):
            for child in value:
                visit(child)
            return
        if not isinstance(value, dict):
            return
        for key, child in list(value.items()):
            if key == "tools" and isinstance(child, list):
                kept = []
                for item in child:
                    if isinstance(item, dict) and item.get("type") == "custom":
                        name = item.get("name")
                        if not isinstance(name, str) or not name:
                            kept.append(item)
                            continue
                        description = item.get("description")
                        if not isinstance(description, str) or not description.strip():
                            description = f"{name} tool"
                        kept.append({
                            "type": "function",
                            "name": name,
                            "description": description,
                            "parameters": _custom_tool_parameters(),
                        })
                        converted += 1
                        continue
                    kept.append(item)
                    visit(item)
                value[key] = kept
            else:
                visit(child)

    visit(data)
    return converted


_original_transform_responses_api_request = OpenAIResponsesAPIConfig.transform_responses_api_request


def _transform_responses_api_request_with_opencode_compat(
    self: OpenAIResponsesAPIConfig,
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    data = _original_transform_responses_api_request(self, *args, **kwargs)
    model = kwargs.get("model")
    litellm_params = kwargs.get("litellm_params")
    api_base = getattr(litellm_params, "api_base", None)
    if isinstance(model, str) and isinstance(api_base, str) and "opencode.ai/zen/go" in api_base:
        converted = normalize_opencode_custom_tools(data)
        fixed = normalize_opencode_tool_schemas(data)
        if converted or fixed:
            litellm.verbose_logger.info(
                "OpenCode Go outbound tool compatibility: bridged_custom=%d fixed_required=%d",
                converted,
                fixed,
            )
    return data


OpenAIResponsesAPIConfig.transform_responses_api_request = _transform_responses_api_request_with_opencode_compat  # type: ignore[method-assign]


if __name__ == "__main__":
    litellm.run_server()
