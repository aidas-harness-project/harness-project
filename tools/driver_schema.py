"""Materialize repository-local JSON Schema references for native CLI output.

Claude and Codex receive one JSON object through their native structured-output
interfaces.  They cannot resolve a repository-relative reference such as
``common_component_output.schema.json#/$defs/evidence_reference`` themselves,
so a driver must inline those references before sending a schema.  This helper
only reads versioned schemas; it never opens case data or writes artifacts.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"


class LocalSchemaReferenceError(ValueError):
    """Raised when a driver schema refers outside the repository schema set."""


def load_materialized_schema(schema_name: str, *, schemas_dir: Path = SCHEMAS) -> dict[str, Any]:
    """Load a schema and recursively inline repository-local external refs.

    Fragment-only references (``#/...``) remain intact because they resolve
    inside the single schema object sent to the provider.  Remote URLs and
    parent-directory paths are rejected rather than becoming an implicit read
    capability for a driver.
    """
    root = schemas_dir.resolve()
    start = (root / schema_name).resolve()
    _require_child(start, root)
    return _materialize_file(start, root, stack=(), ref_stack=())


def _materialize_file(path: Path, root: Path, *, stack: tuple[Path, ...],
                      ref_stack: tuple[tuple[Path, str], ...]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalSchemaReferenceError(f"could not load schema {path.name}") from exc
    if not isinstance(payload, dict):
        raise LocalSchemaReferenceError(f"schema {path.name} must be a JSON object")
    return _materialize_node(payload, path, payload, root, stack=(*stack, path),
                             ref_stack=ref_stack)


def _materialize_node(node: Any, current: Path, document: dict[str, Any], root: Path,
                      *, stack: tuple[Path, ...],
                      ref_stack: tuple[tuple[Path, str], ...]) -> Any:
    if isinstance(node, list):
        return [_materialize_node(item, current, document, root, stack=stack,
                                  ref_stack=ref_stack) for item in node]
    if not isinstance(node, dict):
        return copy.deepcopy(node)

    ref = node.get("$ref")
    if isinstance(ref, str):
        if ref.startswith("#"):
            target, target_document, fragment = current, document, ref[1:]
        else:
            target_name, fragment = _split_reference(ref)
            target = (current.parent / target_name).resolve()
            _require_child(target, root)
            try:
                target_document = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise LocalSchemaReferenceError(f"could not load schema {target.name}") from exc
            if not isinstance(target_document, dict):
                raise LocalSchemaReferenceError(f"schema {target.name} must be a JSON object")
        reference_key = (target, fragment)
        if reference_key in ref_stack:
            chain = " -> ".join(f"{item.name}#{part}" for item, part in (*ref_stack, reference_key))
            raise LocalSchemaReferenceError(f"cyclic schema reference: {chain}")
        target_node = _resolve_fragment(target_document, fragment)
        overlay = {key: value for key, value in node.items() if key != "$ref"}
        materialized = _materialize_node(target_node, target, target_document, root,
                                         stack=(*stack, target),
                                         ref_stack=(*ref_stack, reference_key))
        if overlay:
            if not isinstance(materialized, dict):
                raise LocalSchemaReferenceError(f"cannot overlay non-object reference {ref!r}")
            materialized = {
                **materialized,
                **_materialize_node(overlay, current, document, root, stack=stack,
                                    ref_stack=ref_stack),
            }
        return materialized
    return {key: _materialize_node(value, current, document, root, stack=stack,
                                   ref_stack=ref_stack)
            for key, value in node.items()}


def _split_reference(ref: str) -> tuple[str, str]:
    if "://" in ref or ref.startswith("/"):
        raise LocalSchemaReferenceError(f"non-local schema reference is not supported: {ref!r}")
    name, marker, fragment = ref.partition("#")
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        raise LocalSchemaReferenceError(f"unsafe schema reference: {ref!r}")
    return name, fragment if marker else ""


def _resolve_fragment(document: dict[str, Any], fragment: str) -> Any:
    if not fragment:
        return document
    if not fragment.startswith("/"):
        raise LocalSchemaReferenceError(f"unsupported schema fragment: #{fragment}")
    current: Any = document
    for part in fragment[1:].split("/"):
        token = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or token not in current:
            raise LocalSchemaReferenceError(f"schema fragment not found: #{fragment}")
        current = current[token]
    return current


def _require_child(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise LocalSchemaReferenceError("schema reference escapes schemas directory") from exc
