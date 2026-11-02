import re
from contextvars import ContextVar

runtime: ContextVar[dict | None] = ContextVar("media_runtime", default=None)


async def emit(kind, detail):
    context = runtime.get()
    if context and context.get("emit"):
        await context["emit"](kind, detail)


async def record_call(**data):
    context = runtime.get()
    if context and context.get("record_call"):
        await context["record_call"](data)


def redact(value: str):
    value = re.sub(r"lsv2_[\w-]+|sk-[\w-]{20,}", "[密钥已隐藏]", value)
    value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号已隐藏]", value)
    return re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱已隐藏]", value)


def partial_json_string(text: str, field: str):
    """Read only a completed prefix of a JSON string; incomplete escape sequences wait."""
    import json

    found = re.search(r'"' + re.escape(field) + r'"\s*:\s*"', text)
    if not found:
        return ""
    raw = text[found.end() :]
    output, index = [], 0
    while index < len(raw):
        char = raw[index]
        if char == '"':
            break
        if char == "\\":
            if index + 1 >= len(raw):
                break
            width = 6 if raw[index + 1] == "u" else 2
            if index + width > len(raw):
                break
            if width == 6:
                try:
                    codepoint = int(raw[index + 2 : index + 6], 16)
                except ValueError:
                    break
                if 0xD800 <= codepoint <= 0xDBFF:
                    if len(raw) < index + 12 or raw[index + 6 : index + 8] != "\\u":
                        break
                    width = 12
                elif 0xDC00 <= codepoint <= 0xDFFF:
                    break
            try:
                char = json.loads('"' + raw[index : index + width] + '"')
            except ValueError:
                break
            index += width
        else:
            index += 1
        output.append(char)
    return "".join(output)
