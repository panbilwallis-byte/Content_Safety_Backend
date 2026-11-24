"""Read Anthropic Messages traces without replacing their recorded source."""
from app.services.agent_trace_generation import dumps


CODE = "anthropic_messages"
LABEL = "Anthropic Messages"
NATIVE_BLOCKS = {"tool_use", "tool_result", "thinking", "redacted_thinking"}
EXAMPLE = {
    "trajectory_id": "claude-example", "provider": "anthropic",
    "system": "Summarize public material.",
    "messages": [
        {"role": "user", "content": "Find the public report."},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"query": "public report"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "The report is available."},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "The public report is available."}]},
    ],
    "usage": {"input_tokens": 120, "output_tokens": 30},
}


def is_anthropic(record):
    messages = record.get("messages")
    if not isinstance(messages, list):
        return False
    messages = [message for message in messages if isinstance(message, dict)]
    if any(isinstance(block, dict) and block.get("type") in NATIVE_BLOCKS
           for message in messages if isinstance(message.get("content"), list)
           for block in message["content"]):
        return True
    if any(message.get("role") in {"tool", "function", "developer"} or "tool_calls" in message for message in messages):
        return False
    return (str(record.get("provider", "")).lower() == "anthropic"
            or str(record.get("model", "")).lower().startswith("claude-")
            or isinstance(record.get("system"), (str, list)))


def content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block["text"] for block in content if isinstance(block, dict)
                         and block.get("type") == "text" and isinstance(block.get("text"), str))
    return ""


def token_usage(record):
    def valid(value):
        return type(value) is int and value >= 0

    def from_usage(usage):
        value = {}
        if valid(usage.get("input_tokens")):
            value["input_tokens"] = usage["input_tokens"] + sum(
                usage[key] for key in ("cache_read_input_tokens", "cache_creation_input_tokens") if valid(usage.get(key)))
        if valid(usage.get("output_tokens")):
            value["output_tokens"] = usage["output_tokens"]
        return value

    for field in ("usage", "token_totals"):
        usage = record.get(field)
        if isinstance(usage, dict):
            if normalized := from_usage(usage):
                return normalized
    turns = record.get("api_turns")
    if not isinstance(turns, list) or not turns:
        return {}
    usage_rows = []
    for turn in turns:
        response = turn.get("response") if isinstance(turn, dict) else None
        usage = response.get("usage") if isinstance(response, dict) else None
        usage_rows.append(from_usage(usage) if isinstance(usage, dict) else {})
    return {key: sum(row[key] for row in usage_rows) for key in ("input_tokens", "output_tokens")
            if all(key in row for row in usage_rows)}


def message_view(record):
    """Adapt recorded text and client tool blocks to the shared message readers."""
    messages, paths = [], {}

    def emit(message, source, *, content=None, reasoning=None, tool_paths=()):
        key = f"messages[{len(messages)}]"
        messages.append(message)
        paths[key] = source
        if content:
            paths[key + ".content"] = content
        if reasoning:
            paths[key + ".reasoning_content"] = reasoning
        for index, tool_path in enumerate(tool_paths):
            base = f"{key}.tool_calls[{index}]"
            paths[base] = tool_path
            paths[base + ".function.name"] = tool_path + ".name"

    system = content_text(record.get("system"))
    if system:
        emit({"role": "system", "content": system}, "system", content="system")
    if not isinstance(record.get("messages"), list) or not record["messages"]:
        raise ValueError("Anthropic 轨迹缺少 messages")
    for index, message in enumerate(record["messages"]):
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "system"}:
            raise ValueError("Anthropic 消息角色无效")
        if message.get("tool_calls"):
            raise ValueError("Anthropic 消息必须通过 tool_use 内容块记录调用")
        role, content = message["role"], message.get("content")
        base = f"messages[{index}].content"
        if isinstance(content, str):
            emit({"role": role, "content": content}, base, content=base)
            continue
        if not isinstance(content, list):
            raise ValueError("Anthropic 消息内容必须为字符串或块数组")
        text_blocks, thoughts, calls, tool_paths = [], [], [], []
        for block_index, block in enumerate(content):
            path = f"{base}[{block_index}]"
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                raise ValueError("Anthropic 内容块缺少 type")
            kind = block["type"]
            if kind == "tool_use":
                if role != "assistant" or not all(isinstance(block.get(key), str) and block[key] for key in ("id", "name")) or not isinstance(block.get("input"), dict):
                    raise ValueError("Anthropic tool_use 缺少有效的 id、name 或 input")
                calls.append({"id": block["id"], "function": {"name": block["name"], "arguments": dumps(block["input"])}})
                tool_paths.append(path)
            elif kind == "tool_result":
                if role != "user" or not isinstance(block.get("tool_use_id"), str) or not block["tool_use_id"]:
                    raise ValueError("Anthropic tool_result 缺少有效的 tool_use_id")
                output = content_text(block.get("content"))
                result = {"role": "tool", "tool_call_id": block["tool_use_id"], "content": output}
                if block.get("is_error") is True:
                    result["is_error"] = True
                emit(result, path + ".content")
            elif kind == "text" and isinstance(block.get("text"), str):
                text_blocks.append((path + ".text", block["text"]))
            elif kind == "thinking" and role == "assistant" and isinstance(block.get("thinking"), str):
                thoughts.append((path + ".thinking", block["thinking"]))

        if not (text_blocks or thoughts or calls):
            continue
        if role != "assistant":
            for path, value in text_blocks:
                emit({"role": role, "content": value}, path, content=path)
            continue
        adapted = {"role": role}
        if text_blocks:
            adapted["content"] = "\n".join(value for _, value in text_blocks)
        if thoughts:
            adapted["reasoning_content"] = "\n".join(value for _, value in thoughts)
        if calls:
            adapted["tool_calls"] = calls
        emit(adapted, base, content=text_blocks[0][0] if len(text_blocks) == 1 else base,
             reasoning=thoughts[0][0] if len(thoughts) == 1 else base, tool_paths=tool_paths)
    if not messages:
        raise ValueError("Anthropic 轨迹缺少可解析的消息")
    return {**record, "messages": messages, "usage": token_usage(record)}, paths
