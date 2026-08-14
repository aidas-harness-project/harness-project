"""Run a non-sensitive structured-output smoke test through one provider.

This tool deliberately sends no case data. It exists to exercise the common
driver-facing provider adapter with a caller-supplied JSON Schema before a
driver is given processed claim text.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from jsonschema import Draft202012Validator

from llm_providers import (
    ProviderExecutionError,
    add_provider_args,
    build_provider,
    parse_provider_config,
)


DEFAULT_PROMPT = (
    "Return the required JSON object. Set ok to true and value to provider-smoke."
)


def _load_schema(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not load JSON Schema from {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"JSON Schema must be an object: {path}")
    return loaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_provider_args(parser)
    parser.add_argument("--schema", required=True, type=Path,
                        help="JSON Schema for the benign smoke response")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT,
                        help="Benign prompt; do not pass case data")
    args = parser.parse_args(argv)

    try:
        schema = _load_schema(args.schema)
        validator = Draft202012Validator(schema)
        config = parse_provider_config(args)
        with tempfile.TemporaryDirectory(prefix="harness-provider-smoke-") as temp_dir:
            provider = build_provider(config, root=Path(temp_dir))
            result = provider.analyze_text_structured(
                args.prompt, "provider_smoke_v1", schema
            )
        if result.structured_output is None:
            raise ProviderExecutionError("provider returned no structured output")
        errors = sorted(validator.iter_errors(result.structured_output), key=str)
        if errors:
            raise ProviderExecutionError(
                "provider structured output failed the requested schema: "
                + "; ".join(error.message for error in errors)
            )
    except (ValueError, ProviderExecutionError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({
        "provider_name": result.provider_name,
        "model_name": result.model_name,
        "prompt_version": result.prompt_version,
        "structured_output": result.structured_output,
        "schema_valid": True,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
