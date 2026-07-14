from pathlib import Path
from types import SimpleNamespace

import local_runtime as runtime


def test_preflight_requires_e_scoped_loopback_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "_run", lambda *args, **kwargs: (True, "ok"))
    result = runtime.preflight(
        tmp_path,
        env={
            "OLLAMA_HOST": "https://example.com",
            "HARNESS_LOCAL_OCR_COMMAND": str(tmp_path / "outside-tesseract.exe"),
            "HARNESS_LOCAL_LLM_COMMAND": str(tmp_path / "outside-ollama.exe"),
        },
    )

    checks = {item["name"]: item["passed"] for item in result["checks"]}
    assert checks["runtime_on_e_drive"] is False
    assert checks["ollama_loopback"] is False
    assert result["passed"] is False


def test_preflight_checks_both_models(monkeypatch, tmp_path):
    runtime_root = Path("E:/harness-runtime")
    tesseract = runtime_root / "tesseract" / "tesseract.exe"
    ollama = runtime_root / "ollama" / "ollama.exe"
    tessdata = runtime_root / "tesseract" / "tessdata"
    models = runtime_root / "ollama-models"
    existing = {tesseract, ollama, tessdata, models, tessdata / "kor.traineddata", tessdata / "eng.traineddata"}

    monkeypatch.setattr(Path, "exists", lambda self: self in existing or self == Path(runtime.sys.executable))
    monkeypatch.setattr(Path, "is_file", lambda self: self in existing or self == Path(runtime.sys.executable))
    monkeypatch.setattr(Path, "is_dir", lambda self: self in existing)
    monkeypatch.setattr(runtime, "_inside", lambda path, parent: True)
    calls = []

    def fake_run(command, env, timeout=30):
        calls.append(command)
        return True, "ok"

    monkeypatch.setattr(runtime, "_run", fake_run)
    result = runtime.preflight(
        runtime_root,
        text_model="text-model",
        vision_model="vision-model",
        env={"OLLAMA_HOST": "http://127.0.0.1:11434"},
    )

    assert [str(ollama), "show", "text-model"] in calls
    assert [str(ollama), "show", "vision-model"] in calls
    assert result["passed"] is True


def test_preflight_python_check_requires_absolute_existing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "_run", lambda *args, **kwargs: (True, "ok"))
    # A bare, non-absolute interpreter name must not pass the Python check.
    monkeypatch.setattr(runtime.sys, "executable", "python")
    result = runtime.preflight(tmp_path, env={"OLLAMA_HOST": "http://127.0.0.1:11434"})

    checks = {item["name"]: item["passed"] for item in result["checks"]}
    assert checks["python_absolute_path"] is False
