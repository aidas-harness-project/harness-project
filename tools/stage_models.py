"""One file answers "which model does this stage run on".

Before this module the answer was in three places and none of them was
complete: `model:` in each of eleven `.claude/agents/*.md` frontmatters (a
Claude Code model tier, not a provider slug, and only for the stages that run as
subagents), a scatter of per-role environment variables
(`HARNESS_OCR_READER_A_MODEL`, `HARNESS_REDACTION_MODEL`,
`HARNESS_OPENROUTER_MODEL`), and whatever `--provider`/`--model` an operator
typed. Nothing could be read to answer the question for the whole pipeline.

`config/providers/stage_models_v0.1.json` is now that artifact. This module
reads it; no caller parses it directly.

Resolution order, highest first:

1. **An explicit CLI flag.** An operator saying it now beats a file saying it
   earlier.
2. **The role entry**, then the **stage entry**, then the config's `default`.
   A role inherits its stage's values for whichever of provider/model it does
   not set, so "same provider, different model per reader" needs one line.
3. **The environment**, exactly as before this module existed.
4. **`llm_providers.DEFAULT_PROVIDER`** and the provider's own model env.

Config sits ABOVE the environment because the file is checked in and reviewed
while the environment is ambient: a deployment that disagrees with the recorded
selection should have to edit the record. An ABSENT entry falls straight
through, so a deployment that configures nothing behaves exactly as it did
before -- which is why the shipped file is empty.

This module never invents a model. An undecided model is an absent key, and an
absent key resolves to None, which lets the existing "openrouter requires a
model name" refusal fire at provider selection rather than being papered over
with a plausible slug.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "providers" / "stage_models_v0.1.json"
SCHEMA_NAME = "stage_model_config.schema.json"


class StageModelConfigError(RuntimeError):
    """Raised when the stage/model config cannot be used as written."""


@functools.lru_cache(maxsize=1)
def load_config(path: str | None = None) -> dict[str, Any]:
    """Read and cache the config. A malformed file is an error, not a default.

    Falling back to "no configuration" on a parse error would make a typo look
    like a deliberate absence, and the two must stay distinguishable -- that is
    the same rule the rest of the harness applies to a missing measurement.
    """
    config_path = Path(path) if path else CONFIG_PATH
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # A deployment without the file is legitimate: every lookup then falls
        # through to the environment, which is the pre-config behaviour.
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise StageModelConfigError(f"could not read {config_path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise StageModelConfigError(f"{config_path} must contain a JSON object")
    return loaded


def _selection(entry: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    if not isinstance(entry, Mapping):
        return None, None
    provider = entry.get("provider") or None
    model = entry.get("model") or None
    return provider, model


def resolve(
    stage: str,
    role: str | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """The (provider, model) this stage/role should run on.

    `provider`/`model` are whatever the caller was given explicitly -- a CLI
    flag -- and win outright. Either half may be None independently: naming a
    provider on the command line does not discard the model the file records
    for it.

    Returns None for anything nothing decided, which the caller passes to
    `build_provider` unchanged so the existing environment fallbacks and the
    existing "requires a model name" refusal both still apply.
    """
    source = load_config() if config is None else config
    stage_entry = ((source.get("stages") or {}).get(stage)) or {}
    role_entry = ((stage_entry.get("roles") or {}).get(role)) if role else None

    for candidate in (role_entry, stage_entry, source.get("default")):
        candidate_provider, candidate_model = _selection(candidate)
        provider = provider or candidate_provider
        model = model or candidate_model
    return provider, model


def configured_stages(config: Mapping[str, Any] | None = None) -> dict[str, dict]:
    """Every stage the config names, with what it actually decides.

    Reported rather than assumed: an entry that decides nothing is listed with
    empty values, so "we have not chosen yet" and "this stage makes no call"
    stay visible instead of both being an absence.
    """
    source = load_config() if config is None else config
    out: dict[str, dict] = {}
    for stage, entry in sorted((source.get("stages") or {}).items()):
        provider, model = resolve(stage, config=source)
        roles = {}
        for role in sorted((entry or {}).get("roles") or {}):
            role_provider, role_model = resolve(stage, role, config=source)
            roles[role] = {"provider": role_provider, "model": role_model}
        out[stage] = {"provider": provider, "model": model}
        if roles:
            out[stage]["roles"] = roles
    return out


def main(argv: list[str] | None = None) -> int:
    """Print the resolved selection, so "what runs on what" is one command."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("stage", nargs="?", help="Report one stage instead of all")
    parser.add_argument("--role", help="Report one role within that stage")
    args = parser.parse_args(argv)

    if args.stage:
        provider, model = resolve(args.stage, args.role)
        payload = {"stage": args.stage, "role": args.role,
                   "provider": provider, "model": model}
    else:
        payload = configured_stages()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
