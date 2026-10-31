"""Model fetching: local folder first, then the Íris release, then the original source."""

import zipfile

import pytest
from iris_voz import server


def test_sources_order(monkeypatch):
    monkeypatch.setattr(server, "LOCAL_MODELS", r"E:\modelos")
    monkeypatch.setattr(server, "RELEASE_URL", "https://exemplo/rel")
    sources = server.model_sources("pt_BR-faber-medium.onnx")
    assert sources[0].endswith("pt_BR-faber-medium.onnx") and "modelos" in sources[0]
    assert sources[1] == "https://exemplo/rel/pt_BR-faber-medium.onnx"
    assert "huggingface" in sources[2]
    # Folders travel zipped; Whisper has no original URL (faster-whisper handles it).
    assert server.model_sources("faster-whisper-small")[-1] == "https://exemplo/rel/faster-whisper-small.zip"


def test_ensure_models_from_local_folder(tmp_path, monkeypatch):
    local = tmp_path / "pendrive"
    local.mkdir()
    (local / "voices-v1.0.bin").write_bytes(b"vozes")
    with zipfile.ZipFile(local / "faster-whisper-base.zip", "w") as archive:
        archive.writestr("faster-whisper-base/model.bin", b"pesos")
    monkeypatch.setattr(server, "LOCAL_MODELS", str(local))
    monkeypatch.setattr(server, "RELEASE_URL", "http://127.0.0.1:9/bloqueado")
    models = tmp_path / "models"
    server.ensure_models(models, ["voices-v1.0.bin", "faster-whisper-base"])
    assert (models / "voices-v1.0.bin").read_bytes() == b"vozes"
    assert (models / "faster-whisper-base" / "model.bin").read_bytes() == b"pesos"
    assert not list(models.glob("*.part"))


def test_ensure_models_reports_every_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "LOCAL_MODELS", "")
    monkeypatch.setattr(server, "RELEASE_URL", "http://127.0.0.1:9/bloqueado")
    with pytest.raises(RuntimeError, match="faster-whisper-small"):
        server.ensure_models(tmp_path, ["faster-whisper-small"])


def test_machine_models():
    cpu = server.machine_models(cuda=False, stt="base", everything=False)
    assert "pt_BR-faber-medium.onnx" in cpu and "faster-whisper-base" in cpu
    assert "kokoro-v1.0.onnx" not in cpu and server.WAKE_MODEL in cpu
    assert "faster-whisper-large-v3-turbo" in server.machine_models(True, "auto", False)
