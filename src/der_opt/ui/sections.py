"""Rewriting a few top-level sections of a YAML file in place (the app's study
settings forms): each named section is replaced, removed or appended, and every other
line, comments included, stays exactly as written."""

from __future__ import annotations

import re

import yaml

KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:")


class _Dumper(yaml.SafeDumper):
    """Lists indented under their key (`  - item`), as the examples write them."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def dump_section(key: str, value) -> str:
    """`key: value` as the examples write it: a list of mappings as `  - k: {flow}` lines,
    a mapping as `  k: {flow}` lines, anything else in block style."""
    if isinstance(value, list) and all(isinstance(item, dict) and item for item in value):
        lines = [f"{key}:"]
        for item in value:
            for n, (k, v) in enumerate(item.items()):
                lines.append(f"  {'- ' if n == 0 else '  '}{k}: {flow(v)}")
        return "\n".join(lines) + "\n"
    if isinstance(value, dict) and value:
        return "\n".join([f"{key}:", *(f"  {flow(k)}: {flow(v)}" for k, v in value.items())]) + "\n"
    return yaml.dump({key: value}, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=110,
                     default_flow_style=None)


def flow(value) -> str:
    """A value as one line of YAML (what a form field shows)."""
    text = yaml.dump(value, Dumper=_Dumper, default_flow_style=True, width=10_000, allow_unicode=True,
                     sort_keys=False).strip()
    return text.removesuffix("\n...").removesuffix("...").strip()


def _spans(lines: list[str]) -> dict[str, tuple[int, int, int]]:
    """Each top-level key's (first comment line above it, key line, end): the comment
    lines right above a key belong to it; trailing blank and comment lines don't."""
    keys = [(i, m.group(1)) for i, line in enumerate(lines) if (m := KEY.match(line))]
    spans = {}
    for n, (i, key) in enumerate(keys):
        floor = keys[n - 1][0] + 1 if n else 0
        start = i
        while start > floor and lines[start - 1].startswith("#"):
            start -= 1
        end = keys[n + 1][0] if n + 1 < len(keys) else len(lines)
        while end > i + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
            end -= 1
        spans[key] = (start, i, end)
    return spans


def replace_sections(text: str, sections: dict) -> str:
    """`text` with each key in `sections` set to its value (None or empty removes it,
    with the comments right above it). A section that's there keeps its place and its
    comments above; a new one goes at the end."""
    lines = text.splitlines()
    for key, value in sections.items():
        spans = _spans(lines)
        empty = value is None or value == [] or value == {}
        body = [] if empty else dump_section(key, value).rstrip("\n").splitlines()
        if key in spans:
            start, keyline, end = spans[key]
            lines[start:end] = [] if empty else lines[start:keyline] + body
        elif body:
            while lines and not lines[-1].strip():
                lines.pop()
            lines += ["", *body]
    return "\n".join(lines) + "\n"
