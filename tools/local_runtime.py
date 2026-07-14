"""Preflight checks for the project-local OCR/Ollama runtime.

This tool never downloads or installs anything. It verifies that the runtime
prepared by ``tools/setup_local_runtime.ps1`` is complete, E:-scoped, loopback
only, and has the configured text/vision models already present.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNTIME_ROOT = ROOT / ".runtime"
DEFAULT_MODEL = "qwen3-vl:4b"


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _run(command: list[str], env: dict[str, str], timeout: int = 30) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    detail = (result.stdout or result.stderr).strip()
    return result.returncode == 0, detail


def preflight(
    runtime_root: Path,
    *,
    text_model: str = DEFAULT_MODEL,
    vision_model: str = DEFAULT_MODEL,
    env: dict[str, str] | None = None,
) -> dict:
    source_env = dict(os.environ if env is None else env)
    runtime_root = runtime_root.resolve()
    tesseract = Path(source_env.get("HARNESS_LOCAL_OCR_COMMAND") or runtime_root / "tesseract" / "tesseract.exe")
    ollama = Path(
        source_env.get("HARNESS_LOCAL_LLM_COMMAND")
        or source_env.get("HARNESS_LOCAL_VLM_COMMAND")
        or runtime_root / "ollama" / "ollama.exe"
    )
    tessdata = Path(source_env.get("TESSDATA_PREFIX") or runtime_root / "tesseract" / "tessdata")
    models = Path(source_env.get("OLLAMA_MODELS") or runtime_root / "ollama-models")
    host = source_env.get("OLLAMA_HOST") or "http://127.0.0.1:11434"
    normalized_host = host if "://" in host else f"http://{host}"
    parsed_host = urlparse(normalized_host)

    checks: list[dict] = []

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})

    record("runtime_on_e_drive", runtime_root.drive.upper() == "E:", str(runtime_root))
    python_exe = sys.executable or ""
    python_ok = bool(python_exe) and Path(python_exe).is_absolute() and Path(python_exe).is_file()
    record("python_absolute_path", python_ok, python_exe or "<empty sys.executable>")
    record("tesseract_scoped", tesseract.exists() and _inside(tesseract, runtime_root), str(tesseract))
    record("tessdata_scoped", tessdata.is_dir() and _inside(tessdata, runtime_root), str(tessdata))
    for language in ("kor", "eng"):
        traineddata = tessdata / f"{language}.traineddata"
        record(f"tessdata_{language}", traineddata.is_file(), str(traineddata))
    record("ollama_scoped", ollama.exists() and _inside(ollama, runtime_root), str(ollama))
    record("ollama_models_scoped", models.is_dir() and _inside(models, runtime_root), str(models))
    loopback = parsed_host.scheme in {"http", "https"} and parsed_host.hostname in {
        "localhost", "127.0.0.1", "::1",
    }
    record("ollama_loopback", loopback, host)

    run_env = dict(source_env)
    run_env.update(
        {
            "HARNESS_LOCAL_OCR_COMMAND": str(tesseract),
            "HARNESS_LOCAL_LLM_COMMAND": str(ollama),
            "HARNESS_LOCAL_VLM_COMMAND": str(ollama),
            "TESSDATA_PREFIX": str(tessdata),
            "OLLAMA_MODELS": str(models),
            "OLLAMA_HOST": normalized_host,
        }
    )
    if tesseract.exists():
        ok, detail = _run([str(tesseract), "--version"], run_env)
        record("tesseract_runs", ok, detail.splitlines()[0] if detail else "no output")
    if ollama.exists() and loopback:
        ok, detail = _run([str(ollama), "--version"], run_env)
        record("ollama_runs", ok, detail.splitlines()[0] if detail else "no output")
        for purpose, model in (("text", text_model), ("vision", vision_model)):
            ok, detail = _run([str(ollama), "show", model], run_env, timeout=60)
            record(f"ollama_model_{purpose}", ok, model if ok else detail)

    return {
        "runtime_root": str(runtime_root),
        "text_model": text_model,
        "vision_model": vision_model,
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
        "environment": {
            "HARNESS_LOCAL_OCR_COMMAND": str(tesseract),
            "HARNESS_LOCAL_LLM_COMMAND": str(ollama),
            "HARNESS_LOCAL_VLM_COMMAND": str(ollama),
            "TESSDATA_PREFIX": str(tessdata),
            "OLLAMA_MODELS": str(models),
            "OLLAMA_HOST": normalized_host,
            "HARNESS_LOCAL_LLM_MODEL": text_model,
            "HARNESS_LOCAL_VLM_MODEL": vision_model,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--text-model", default=DEFAULT_MODEL)
    parser.add_argument("--vision-model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    result = preflight(
        args.runtime_root, text_model=args.text_model, vision_model=args.vision_model
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
