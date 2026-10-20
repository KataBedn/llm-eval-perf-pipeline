"""Promptfoo assertion: the answer must be in the language of the user's message if it is polish, english, german or spanish"""
from lingua import Language, LanguageDetectorBuilder

LANGS = {
    "en": Language.ENGLISH, 
    "pl": Language.POLISH, 
    "de": Language.GERMAN,
    "es": Language.SPANISH, 
    "zh":Language.CHINESE   # not supported by the agent; detected only to report it clearly
}
_detector = LanguageDetectorBuilder.from_languages(*LANGS.values()).build()


def get_assert(output, context):
    expected = (context.get("vars") or {}).get("lang")
    if not expected:
        return {"pass": True, "score": 1, "reason": "no language expectation set"}

    detected = _detector.detect_language_of(output)
    got = next((k for k, v in LANGS.items() if v == detected), None)
    ok = got == expected
    return {
        "pass": ok,
        "score": 1 if ok else 0,
        "reason": f"expected '{expected}', detected '{got or 'unknown'}'",
    }