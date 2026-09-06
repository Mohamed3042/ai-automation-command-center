"""A deliberately small YAML reader for `docs/openapi.v1.yaml`.

The application runtime has no third-party dependency, and neither does the test
that proves the OpenAPI document covers every served route. Rather than ship a
YAML engine, this module reads the strict subset the specification is written in:
block mappings, block sequences, plain and quoted scalars, and `|` block
literals. Anything outside that subset raises, so the specification cannot drift
into syntax this reader silently misreads.

`tests/test_openapi_contract.py` cross-checks the result against PyYAML whenever
PyYAML is installed (the CI `spec-tools` job installs it), so this reader is
itself under test rather than trusted.
"""
from __future__ import annotations

import re

_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?$")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "0": "\0"}


class YamlSubsetError(ValueError):
    """The document uses YAML this reader deliberately does not implement."""


def _strip_comment(text: str) -> str:
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote and (quote == "'" or text[index - 1] != "\\"):
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index]
    return text


def _unquote_double(text: str) -> str:
    out = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            nxt = text[index + 1]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                index += 2
                continue
            if nxt == "u":
                out.append(chr(int(text[index + 2:index + 6], 16)))
                index += 6
                continue
        out.append(char)
        index += 1
    return "".join(out)


def parse_scalar(text: str):
    value = text.strip()
    if not value:
        return None
    if value[0] == "'" and value[-1] == "'" and len(value) >= 2:
        return value[1:-1].replace("''", "'")
    if value[0] == '"' and value[-1] == '"' and len(value) >= 2:
        return _unquote_double(value[1:-1])
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if value[0] in "[{":
        raise YamlSubsetError("only the empty flow collections [] and {{}} are part of this subset: {0}".format(text))
    if value in ("true", "True"):
        return True
    if value in ("false", "False"):
        return False
    if value in ("null", "Null", "~"):
        return None
    if _INT.match(value):
        return int(value)
    if _FLOAT.match(value):
        return float(value)
    return value


def _split_key(text: str):
    """Return `(key, rest)` for `key: rest`, honouring quotes; else `None`."""
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == ":" and (index + 1 == len(text) or text[index + 1] in " \t"):
            key = text[:index].strip()
            if key.startswith("'") or key.startswith('"'):
                key = parse_scalar(key)
            return str(key), text[index + 1:].strip()
    return None


class _Reader:
    def __init__(self, text: str) -> None:
        self.raw = text.replace("\r\n", "\n").split("\n")
        self.index = 0

    def _indent(self, line: str) -> int:
        if "\t" in line[:len(line) - len(line.lstrip())]:
            raise YamlSubsetError("tabs are not allowed for indentation")
        return len(line) - len(line.lstrip(" "))

    def _skip(self) -> None:
        while self.index < len(self.raw):
            stripped = self.raw[self.index].strip()
            if stripped and not stripped.startswith("#") and stripped != "---":
                return
            self.index += 1

    def peek(self):
        self._skip()
        if self.index >= len(self.raw):
            return None, None
        line = self.raw[self.index]
        return self._indent(line), _strip_comment(line).strip()

    def block_scalar(self, indent: int, style: str, chomp: str) -> str:
        lines = []
        while self.index < len(self.raw):
            line = self.raw[self.index]
            if line.strip() and self._indent(line) <= indent:
                break
            lines.append(line)
            self.index += 1
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            return ""
        inner = min(self._indent(line) for line in lines if line.strip())
        body = [line[inner:] if len(line) >= inner else "" for line in lines]
        text = "\n".join(body) if style == "|" else " ".join(item.strip() for item in body)
        return text if chomp == "-" else text + "\n"

    def parse(self, indent: int):
        current, content = self.peek()
        if current is None or current < indent:
            return None
        if content.startswith("- "):
            return self.parse_sequence(current)
        return self.parse_mapping(current)

    def parse_sequence(self, indent: int) -> list:
        items = []
        while True:
            current, content = self.peek()
            if current is None or current != indent or not content.startswith("- "):
                break
            rest = content[2:]
            if _split_key(rest) is None:
                items.append(parse_scalar(rest))
                self.index += 1
            else:
                self.raw[self.index] = " " * (indent + 2) + rest
                items.append(self.parse_mapping(indent + 2))
        return items

    def parse_mapping(self, indent: int) -> dict:
        mapping = {}
        while True:
            current, content = self.peek()
            if current is None or current < indent:
                break
            if current > indent:
                raise YamlSubsetError("unexpected indentation at line {0}: {1!r}".format(self.index + 1, self.raw[self.index]))
            if content.startswith("- "):
                break
            split = _split_key(content)
            if split is None:
                raise YamlSubsetError("expected `key: value` at line {0}: {1!r}".format(self.index + 1, content))
            key, rest = split
            self.index += 1
            if rest in ("|", "|-", ">", ">-"):
                mapping[key] = self.block_scalar(indent, rest[0], "-" if rest.endswith("-") else "")
            elif rest == "":
                nxt_indent, nxt_content = self.peek()
                if nxt_indent is None or nxt_indent < indent:
                    mapping[key] = None
                elif nxt_indent == indent and not nxt_content.startswith("- "):
                    mapping[key] = None
                elif nxt_content.startswith("- "):
                    mapping[key] = self.parse_sequence(nxt_indent)
                else:
                    mapping[key] = self.parse_mapping(nxt_indent)
            else:
                mapping[key] = parse_scalar(rest)
        return mapping


def loads(text: str):
    reader = _Reader(text)
    document = reader.parse(0)
    reader._skip()
    if reader.index < len(reader.raw):
        raise YamlSubsetError("trailing content at line {0}".format(reader.index + 1))
    return document


def load_path(path) -> dict:
    with open(str(path), "r", encoding="utf-8") as handle:
        return loads(handle.read())
