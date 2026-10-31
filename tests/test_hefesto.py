"""Hefesto: what differs from Íris (name, wake word)."""

import pytest

import toad
from toad.iris_commands import Command, parse_command, strip_wake_word


def test_brand():
    assert toad.TITLE == "Hefesto"
    assert toad.WAKE_NAME == "Vulcano"


@pytest.mark.parametrize(
    "spoken, expected",
    [
        ("Vulcano, modo planejamento.", Command("modo_planejamento")),
        ("Vulcano abre o Gemini", Command("abrir", "gemini")),
        ("Ei Vulcano, status", Command("status")),
        ("Íris, status", None),  # Íris' name doesn't wake Hefesto
    ],
)
def test_vulcano_commands(spoken, expected):
    command = parse_command(spoken)
    if expected is None:
        assert command is None
    else:
        assert command is not None and command.name == expected.name


@pytest.mark.parametrize(
    "transcript, rest",
    [
        ("É, Vulcano, que horas são?", "que horas sao"),
        ("Volcano, abre o Claude", "abre o claude"),  # Whisper's English spelling
        ("Vulcão, status", "status"),
        ("Bulcano status", "status"),
        ("o vulcanismo da região", None),
    ],
)
def test_vulcano_after_wake(transcript, rest):
    assert strip_wake_word(transcript, loose=True) == rest


def test_wake_word_sent_to_voice_service():
    from toad.iris_voice import IrisVoice

    voice = IrisVoice.__new__(IrisVoice)
    voice._get = lambda key, default=None: {"modo": "ditado", "ativacao": True}.get(key, default)
    voice.__dict__["mode"] = "ditado"
    command = voice._command()
    assert command[command.index("--wake-word") + 1] == "vulcano"
