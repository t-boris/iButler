"""Read complete plan fields and an append-only JSON string prefix safely."""
import json


def object_prefix(text):
    """No guesses about unfinished escapes/numbers or incomplete metadata.

    Only the final reply string may be partial. Callers must validate the
    complete addressing/intent fields before speaking any of that string.
    """
    decoder = json.JSONDecoder()
    values = {}
    index = len(text) - len(text.lstrip())
    if index >= len(text) or text[index] != "{": return values
    index += 1
    while True:
        while index < len(text) and text[index].isspace(): index += 1
        if index >= len(text) or text[index] == "}": return values
        try: key, index = decoder.raw_decode(text, index)
        except ValueError: return values
        if not isinstance(key, str) or key in values: raise ValueError("duplicate plan key")
        while index < len(text) and text[index].isspace(): index += 1
        if index >= len(text) or text[index] != ":": return values
        index += 1
        while index < len(text) and text[index].isspace(): index += 1
        if index >= len(text): return values
        if key == "reply" and text[index] == '"':
            end = index + 1
            while end < len(text):
                char = text[end]
                if char == '"': break
                if char == "\\":
                    if end + 1 >= len(text): break
                    if text[end+1] == "u":
                        if end + 6 > len(text): break
                        end += 6
                    else: end += 2
                else: end += 1
            values[key] = json.loads(text[index:end] + '"')
            return values
        try: value, end = decoder.raw_decode(text, index)
        except ValueError: return values
        # 0.9 could still become 0.99. Require a real field delimiter.
        boundary = end
        while boundary < len(text) and text[boundary].isspace(): boundary += 1
        if boundary >= len(text) or text[boundary] not in ",}": return values
        values[key] = value
        if text[boundary] == "}": return values
        index = boundary + 1
