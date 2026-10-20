"""Unit tests for scripts/check_language.py (offline, no API calls)."""

import sys
from pathlib import Path

# scripts/ is not a package, so add it to the import path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_language import get_assert  # noqa: E402


def check(output, lang):
    return get_assert(output, {"vars": {"lang": lang}})


def test_spanish_answer_to_english_question_fails():
    out = "Lamentablemente, no puedo proporcionar información específica sobre reembolsos."

    assert check(out, "en")["pass"] is False


def test_polish_answer_when_polish_expected_passes():
    out = "Przepraszam za niedogodność. Proszę podać identyfikator transakcji."

    assert check(out, "pl")["pass"] is True


def test_english_answer_when_english_expected_passes():
    out = "I am sorry about the double charge. Please share your transaction ID."

    assert check(out, "en")["pass"] is True


def test_chinese_answer_when_english_expected_fails():
    out = "对不起，您的订阅被重复扣款了。请提供交易编号。"

    assert check(out, "en")["pass"] is False


def test_missing_language_expectation_skips_check():
    assert get_assert("", {"vars": {}})["pass"] is True