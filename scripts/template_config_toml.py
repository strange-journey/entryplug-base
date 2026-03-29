#!/usr/bin/env python3

# usage: python3 template_config_toml.py --chart-dir /path/to/chart

# generates values.yaml and config.toml.template from config.toml, 
# preserving comments and mapping unset values to null.

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_LINE_RE = re.compile(r"^\s*#\s*default\s*[:=]\s*(.*)\s*$", re.IGNORECASE)
OVERWRITTEN_LINE_RE = re.compile(
    r"^\s*#\s*(?:values are optional,\s*)?overwritten(?:_by| by)\s*:\s*.*$",
    re.IGNORECASE,
)
SECTION_RE = re.compile(r"^\[([A-Za-z0-9_.]+)\]\s*$")
ARRAY_SECTION_RE = re.compile(r"^\[\[([A-Za-z0-9_.]+)\]\]\s*$")
ASSIGNMENT_RE = re.compile(r"^([A-Za-z0-9_]+)\s*=\s*(.*)$")
COMMENTED_ASSIGNMENT_RE = re.compile(r"^#\s*([A-Za-z0-9_]+)\s*=\s*(.*)$")

UNKNOWN = object()
TYPE_OVERRIDES: dict[tuple[str, ...], str] = {}
SPECIAL_TEMPLATE_EXPRESSIONS: dict[tuple[str, ...], str] = {}


@dataclass
class Entry:
    section_path: tuple[str, ...]
    key: str
    comments_raw: list[str]
    comments_clean: list[str]
    active: bool
    value: Any
    normalized_value: Any
    kind: str
    emit_conditionally: bool
    default_raw: str | None
    default_value: Any = UNKNOWN
    default_known: bool = False
    numeric: bool = False


@dataclass
class ArrayItem:
    entries: list[Entry] = field(default_factory=list)


@dataclass
class MappingSection:
    path: tuple[str, ...]
    comments_raw: list[str] = field(default_factory=list)
    comments_clean: list[str] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)
    children: dict[str, SectionNode] = field(default_factory=dict)


@dataclass
class ArraySection:
    path: tuple[str, ...]
    comments_raw: list[str] = field(default_factory=list)
    comments_clean: list[str] = field(default_factory=list)
    items: list[ArrayItem] = field(default_factory=list)


SectionNode = MappingSection | ArraySection


def add_entry(container: MappingSection | ArrayItem, entry: Entry) -> None:
    existing = next((item for item in container.entries if item.key == entry.key), None)
    if existing is None:
        container.entries.append(entry)
        return

    if entry.active:
        raise ValueError(
            f"duplicate active key {'.'.join(entry.section_path + (entry.key,))} in config.toml"
        )

    return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate values.yaml and config.toml.template from config.toml, "
            "preserving config comments and mapping unset values to null."
        )
    )
    parser.add_argument(
        "--chart-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Path to the chart directory containing config.toml.",
    )
    return parser.parse_args()


def load_overrides(chart_dir: Path) -> tuple[dict[tuple[str, ...], str], dict[tuple[str, ...], str]]:
    overrides_path = chart_dir / "overrides.toml"
    if not overrides_path.exists():
        return {}, {}
    data = tomllib.loads(overrides_path.read_text())
    type_overrides: dict[tuple[str, ...], str] = {}
    for key, value in data.get("type_overrides", {}).items():
        type_overrides[tuple(key.split("."))] = value
    template_expressions: dict[tuple[str, ...], str] = {}
    for key, value in data.get("template_expressions", {}).items():
        template_expressions[tuple(key.split("."))] = value
    return type_overrides, template_expressions


def strip_comment_prefix(line: str) -> str:
    if not line.startswith("#"):
        return line
    value = line[1:]
    if value.startswith(" "):
        value = value[1:]
    return value


def trim_blank_lines(lines: list[str]) -> list[str]:
    result = list(lines)
    while result and result[0] == "":
        result.pop(0)
    while result and result[-1] == "":
        result.pop()

    collapsed: list[str] = []
    previous_blank = False
    for line in result:
        if line == "":
            if previous_blank:
                continue
            previous_blank = True
        else:
            previous_blank = False
        collapsed.append(line)
    return collapsed


def clean_comments(raw_comments: list[str]) -> list[str]:
    cleaned: list[str] = []
    for line in raw_comments:
        if line == "":
            cleaned.append(line)
            continue
        if re.match(r"^\s*#\s*$", line):
            cleaned.append("")
            continue
        if DEFAULT_LINE_RE.match(line) or OVERWRITTEN_LINE_RE.match(line):
            continue
        cleaned.append(line)
    return trim_blank_lines(cleaned)


def parse_toml_value(raw_value: str) -> Any:
    return tomllib.loads(f"value = {raw_value}")["value"]


def parse_default_value(default_raw: str) -> tuple[Any, bool]:
    text = default_raw.strip()
    lower = text.lower()

    if "not set" in lower or "unset" in lower or "<empty>" in lower:
        return None, True

    numeric_match = re.match(r"^(-?\d+)\s+\(.*\)$", text)
    if numeric_match:
        return int(numeric_match.group(1)), True

    try:
        return parse_toml_value(text), True
    except tomllib.TOMLDecodeError:
        pass

    if re.fullmatch(r"[A-Za-z0-9_./:+-]+", text):
        return text, True

    return None, False


def extract_default(raw_comments: list[str]) -> tuple[str | None, Any, bool]:
    for line in raw_comments:
        match = DEFAULT_LINE_RE.match(line)
        if not match:
            continue
        raw_value = match.group(1).strip()
        default_value, default_known = parse_default_value(raw_value)
        return raw_value, default_value, default_known
    return None, UNKNOWN, False


def infer_kind(section_path: tuple[str, ...], key: str, raw_value: str | None, parsed_value: Any) -> str:
    if raw_value is None:
        return TYPE_OVERRIDES.get(section_path + (key,), "string")

    stripped = raw_value.strip()
    if stripped.startswith("["):
        return "array"
    if stripped.startswith('"""') or stripped.startswith("'''"):
        return "multiline_string"
    if isinstance(parsed_value, str):
        return "string"
    return "scalar"


def normalize_value(value: Any) -> Any:
    if isinstance(value, str) and value == "":
        return None
    return value


def is_numeric_value(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def collect_array_value(lines: list[str], start_index: int, commented: bool, initial_value: str) -> tuple[str, int]:
    raw_lines = [initial_value]
    depth = initial_value.count("[") - initial_value.count("]")
    index = start_index

    while depth > 0:
        index += 1
        if index >= len(lines):
            raise ValueError("unterminated array value")
        next_line = strip_comment_prefix(lines[index]) if commented else lines[index]
        raw_lines.append(next_line)
        depth += next_line.count("[") - next_line.count("]")

    return "\n".join(raw_lines), index + 1


def collect_multiline_string_value(
    lines: list[str], start_index: int, commented: bool, initial_value: str
) -> tuple[str, int]:
    raw_lines = [initial_value]
    index = start_index

    if initial_value.count('"""') >= 2 or initial_value.count("'''") >= 2:
        return initial_value, index + 1

    terminator = '"""' if initial_value.startswith('"""') else "'''"
    while True:
        index += 1
        if index >= len(lines):
            raise ValueError("unterminated multiline string value")
        next_line = strip_comment_prefix(lines[index]) if commented else lines[index]
        raw_lines.append(next_line)
        if next_line.strip().endswith(terminator):
            break

    return "\n".join(raw_lines), index + 1


def parse_entry(
    lines: list[str],
    start_index: int,
    section_path: tuple[str, ...],
    pending_comments: list[str],
    commented: bool,
) -> tuple[Entry, int]:
    line = lines[start_index]
    pattern = COMMENTED_ASSIGNMENT_RE if commented else ASSIGNMENT_RE
    match = pattern.match(line)
    if not match:
        raise ValueError(f"invalid entry line: {line}")

    key = match.group(1)
    raw_value_part = match.group(2)
    stripped_value = raw_value_part.strip()
    next_index = start_index + 1
    parsed_value: Any = None
    raw_value: str | None = None

    if stripped_value.startswith("["):
        raw_value, next_index = collect_array_value(lines, start_index, commented, raw_value_part)
        parsed_value = parse_toml_value(raw_value)
    elif stripped_value.startswith('"""') or stripped_value.startswith("'''"):
        raw_value, next_index = collect_multiline_string_value(lines, start_index, commented, raw_value_part)
        parsed_value = parse_toml_value(raw_value)
    elif stripped_value != "":
        raw_value = raw_value_part
        parsed_value = parse_toml_value(raw_value)

    comments_raw = list(pending_comments)
    comments_clean = clean_comments(comments_raw)
    default_raw, default_value, default_known = extract_default(comments_raw)
    kind = infer_kind(section_path, key, raw_value, parsed_value)

    if commented:
        value = None
        normalized_value = None
        emit_conditionally = True
    else:
        value = parsed_value
        normalized_value = normalize_value(parsed_value)
        emit_conditionally = normalized_value is None

    entry = Entry(
        section_path=section_path,
        key=key,
        comments_raw=comments_raw,
        comments_clean=comments_clean,
        active=not commented,
        value=value,
        normalized_value=normalized_value,
        kind=kind,
        emit_conditionally=emit_conditionally,
        default_raw=default_raw,
        default_value=default_value,
        default_known=default_known,
        numeric=is_numeric_value(parsed_value),
    )
    return entry, next_index


def ensure_mapping_section(root: MappingSection, path: tuple[str, ...]) -> MappingSection:
    node = root
    for segment in path:
        child = node.children.get(segment)
        if child is None:
            child = MappingSection(path=node.path + (segment,))
            node.children[segment] = child
        if isinstance(child, ArraySection):
            raise ValueError(f"section path conflict on {'.'.join(path)}")
        node = child
    return node


def ensure_array_section(root: MappingSection, path: tuple[str, ...]) -> ArraySection:
    parent = ensure_mapping_section(root, path[:-1])
    segment = path[-1]
    child = parent.children.get(segment)
    if child is None:
        child = ArraySection(path=path)
        parent.children[segment] = child
    if isinstance(child, MappingSection):
        raise ValueError(f"section path conflict on {'.'.join(path)}")
    return child


def parse_config(config_text: str) -> MappingSection:
    root = MappingSection(path=())
    lines = config_text.splitlines()
    current_container: MappingSection | ArrayItem = root
    current_section_path: tuple[str, ...] = ()
    pending_comments: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]

        if line == "":
            pending_comments.append("")
            index += 1
            continue

        array_section_match = ARRAY_SECTION_RE.match(line)
        if array_section_match:
            section_path = tuple(array_section_match.group(1).split("."))
            array_section = ensure_array_section(root, section_path)
            if not array_section.comments_raw and pending_comments:
                array_section.comments_raw = list(pending_comments)
                array_section.comments_clean = clean_comments(pending_comments)
            pending_comments = []

            item = ArrayItem()
            array_section.items.append(item)
            current_container = item
            current_section_path = section_path
            index += 1
            continue

        section_match = SECTION_RE.match(line)
        if section_match:
            section_path = tuple(section_match.group(1).split("."))
            section = ensure_mapping_section(root, section_path)
            if not section.comments_raw and pending_comments:
                section.comments_raw = list(pending_comments)
                section.comments_clean = clean_comments(pending_comments)
            pending_comments = []
            current_container = section
            current_section_path = section_path
            index += 1
            continue

        if line.startswith("#"):
            if DEFAULT_LINE_RE.match(line) or OVERWRITTEN_LINE_RE.match(line):
                pending_comments.append(line)
                index += 1
                continue
            if COMMENTED_ASSIGNMENT_RE.match(line):
                entry, index = parse_entry(lines, index, current_section_path, pending_comments, commented=True)
                pending_comments = []
                add_entry(current_container, entry)
                continue
            pending_comments.append(line)
            index += 1
            continue

        if ASSIGNMENT_RE.match(line):
            entry, index = parse_entry(lines, index, current_section_path, pending_comments, commented=False)
            pending_comments = []
            add_entry(current_container, entry)
            continue

        raise ValueError(f"unsupported config line: {line}")

    return root


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, list):
        return "[" + ", ".join(yaml_scalar(item) for item in value) + "]"
    raise TypeError(f"unsupported yaml scalar value: {value!r}")


YAML_RESERVED_KEYWORDS = {
    "",
    "~",
    "null",
    "true",
    "false",
    "yes",
    "no",
    "on",
    "off",
    ".nan",
    ".inf",
    "+.inf",
    "-.inf",
}


def yaml_key(key: str) -> str:
    lower_key = key.lower()
    if lower_key in YAML_RESERVED_KEYWORDS:
        return yaml_scalar(key)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
        return key
    return yaml_scalar(key)


def append_indented_comment(lines: list[str], comment_lines: list[str], indent: int) -> None:
    for comment in comment_lines:
        if comment == "":
            lines.append(" " * indent + "#")
        else:
            lines.append(" " * indent + comment)


def template_override(entry: Entry) -> str | None:
    return SPECIAL_TEMPLATE_EXPRESSIONS.get(entry.section_path + (entry.key,))


def emit_yaml_entry(lines: list[str], entry: Entry, indent: int) -> None:
    if template_override(entry) is not None:
        return

    if entry.comments_clean:
        append_indented_comment(lines, entry.comments_clean, indent)

    if isinstance(entry.normalized_value, str) and "\n" in entry.normalized_value:
        lines.append(" " * indent + f"{yaml_key(entry.key)}: |-")
        for value_line in entry.normalized_value.splitlines():
            lines.append(" " * (indent + 2) + value_line)
        return

    rendered_value = yaml_scalar(entry.normalized_value)
    lines.append(" " * indent + f"{yaml_key(entry.key)}: {rendered_value}")


def emit_yaml_mapping_section(lines: list[str], section: MappingSection, indent: int, name: str | None) -> None:
    if name is not None:
        if section.comments_clean:
            append_indented_comment(lines, section.comments_clean, indent)
        lines.append(" " * indent + f"{yaml_key(name)}:")
        indent += 2

    first_item = True
    for entry in section.entries:
        if template_override(entry) is not None:
            continue
        if not first_item:
            lines.append("")
        emit_yaml_entry(lines, entry, indent)
        first_item = False

    for child_name, child in section.children.items():
        if not first_item:
            lines.append("")
        emit_yaml_section(lines, child, indent, child_name)
        first_item = False


def emit_yaml_array_section(lines: list[str], section: ArraySection, indent: int, name: str) -> None:
    if section.comments_clean:
        append_indented_comment(lines, section.comments_clean, indent)
    lines.append(" " * indent + f"{yaml_key(name)}:")

    item_indent = indent + 2
    for item_index, item in enumerate(section.items):
        if item_index > 0:
            lines.append("")
        lines.append(" " * item_indent + "-")
        first_entry = True
        for entry in item.entries:
            if template_override(entry) is not None:
                continue
            if not first_entry:
                lines.append("")
            emit_yaml_entry(lines, entry, item_indent + 2)
            first_entry = False


def emit_yaml_section(lines: list[str], section: SectionNode, indent: int, name: str) -> None:
    if isinstance(section, MappingSection):
        emit_yaml_mapping_section(lines, section, indent, name)
    else:
        emit_yaml_array_section(lines, section, indent, name)


def load_values_preamble(values_path: Path) -> list[str]:
    lines = values_path.read_text().splitlines() if values_path.exists() else []
    stop_index = len(lines)

    for index, line in enumerate(lines):
        if line.startswith("# config.toml values") or line.strip() == "config:":
            stop_index = index
            break

    preamble = lines[:stop_index]
    while preamble and preamble[-1] == "":
        preamble.pop()
    return preamble


def generate_values_yaml(root: MappingSection, values_path: Path) -> str:
    preamble = load_values_preamble(values_path)
    lines = list(preamble)
    if lines:
        lines.append("")
    lines.append("# config.toml values - these map directly to TOML sections.")
    lines.append("config:")

    first_child = True
    for entry in root.entries:
        if template_override(entry) is not None:
            continue
        if not first_child:
            lines.append("")
        emit_yaml_entry(lines, entry, 2)
        first_child = False

    for child_name, child in root.children.items():
        if not first_child:
            lines.append("")
        emit_yaml_section(lines, child, 2, child_name)
        first_child = False

    return "\n".join(lines) + "\n"


def value_expression(ref: str, kind: str, numeric: bool = False) -> str:
    if kind == "array":
        return f"{{{{ {ref} | toJson }}}}"
    if kind == "string":
        return f"{{{{ {ref} | quote }}}}"
    if kind == "multiline_string":
        return f"{{{{ {ref} }}}}"
    if numeric:
        return f"{{{{ {ref} | int }}}}"
    return f"{{{{ {ref} }}}}"


def render_template_entry(entry: Entry, ref: str) -> list[str]:
    override = template_override(entry)
    if override is not None:
        return [f"{entry.key} = {override}"]

    if entry.kind == "multiline_string":
        if entry.emit_conditionally:
            return [
                f"{{{{ if not (kindIs \"invalid\" {ref}) }}}}{entry.key} = \"\"\"",
                value_expression(ref, entry.kind),
                '"""',
                "{{ end -}}",
            ]
        return [
            f"{entry.key} = \"\"\"",
            value_expression(ref, entry.kind),
            '"""',
        ]

    rendered_value = value_expression(ref, entry.kind)
    if entry.numeric:
        rendered_value = value_expression(ref, entry.kind, numeric=True)
    if entry.emit_conditionally:
        return [
            f"{{{{ if not (kindIs \"invalid\" {ref}) }}}}{entry.key} = {rendered_value}",
            "{{ end -}}",
        ]
    return [f"{entry.key} = {rendered_value}"]


def emit_template_mapping_section(lines: list[str], section: MappingSection) -> None:
    if section.comments_raw:
        lines.extend(section.comments_raw)
    lines.append(f"[{'.'.join(section.path)}]")

    for entry in section.entries:
        if entry.comments_raw:
            lines.extend(entry.comments_raw)
        ref = ".Values.config." + ".".join(entry.section_path + (entry.key,))
        lines.extend(render_template_entry(entry, ref))

    for child in section.children.values():
        if lines and lines[-1] != "":
            lines.append("")
        emit_template_section(lines, child)


def emit_template_array_section(lines: list[str], section: ArraySection) -> None:
    if section.comments_raw:
        lines.extend(section.comments_raw)

    lines.append(f"{{{{ range .Values.config.{'.'.join(section.path)} }}}}")
    lines.append(f"[[{'.'.join(section.path)}]]")

    if section.items:
        prototype = section.items[0]
        for entry in prototype.entries:
            if entry.comments_raw:
                lines.extend(entry.comments_raw)
            lines.extend(render_template_entry(entry, f".{entry.key}"))

    lines.append("{{ end }}")


def emit_template_section(lines: list[str], section: SectionNode) -> None:
    if isinstance(section, MappingSection):
        emit_template_mapping_section(lines, section)
    else:
        emit_template_array_section(lines, section)


def generate_template(root: MappingSection) -> str:
    lines: list[str] = []
    for entry in root.entries:
        if entry.comments_raw:
            lines.extend(entry.comments_raw)
        ref = ".Values.config." + entry.key
        lines.extend(render_template_entry(entry, ref))
    for child in root.children.values():
        if lines and lines[-1] != "":
            lines.append("")
        emit_template_section(lines, child)
    return "\n".join(lines) + "\n"


def collect_entries(section: SectionNode) -> list[Entry]:
    entries: list[Entry] = []
    if isinstance(section, MappingSection):
        entries.extend(section.entries)
        for child in section.children.values():
            entries.extend(collect_entries(child))
    else:
        for item in section.items:
            entries.extend(item.entries)
    return entries


def format_value_for_report(value: Any) -> str:
    if value is UNKNOWN:
        return "<unknown>"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return repr(value)
    return repr(value)


def collect_mismatches(root: MappingSection) -> list[str]:
    mismatches: list[str] = []
    for entry in collect_entries(root):
        if not entry.active or not entry.default_known:
            continue
        if entry.normalized_value != entry.default_value:
            dotted_key = ".".join(entry.section_path + (entry.key,))
            mismatches.append(
                f"{dotted_key}: current={format_value_for_report(entry.normalized_value)} default={format_value_for_report(entry.default_value)}"
            )
    return mismatches


def main() -> int:
    global TYPE_OVERRIDES, SPECIAL_TEMPLATE_EXPRESSIONS

    args = parse_args()
    chart_dir = args.chart_dir.resolve()
    TYPE_OVERRIDES, SPECIAL_TEMPLATE_EXPRESSIONS = load_overrides(chart_dir)

    config_path = chart_dir / "config.toml"
    values_path = chart_dir / "values.yaml"
    template_path = chart_dir / "config.toml.template"

    root = parse_config(config_path.read_text())
    values_yaml = generate_values_yaml(root, values_path)
    template_text = generate_template(root)

    values_path.write_text(values_yaml)
    template_path.write_text(template_text)

    mismatches = collect_mismatches(root)
    if mismatches:
        print("Documented default mismatches:")
        for mismatch in mismatches:
            print(f"- {mismatch}")
    else:
        print("No documented default mismatches.")

    return 0


if __name__ == "__main__":
    sys.exit(main())