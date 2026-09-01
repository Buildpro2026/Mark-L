"""core/headless/voice_format.py — the server-side "never speak code" gate.

Applied right before any surface hands text to a TTS engine, as a hard
guarantee independent of both the system prompt (which the model can
ignore) and the browser's own cleanForSpeech() (which only protects that
one client, never the phone line). See the module docstring for why this
exists as a separate, final layer rather than trusting either of those.
"""
from core.headless.voice_format import to_speech_text


def test_plain_conversational_text_is_unchanged_in_substance():
    text = "I found several products that match what you're looking for."
    assert to_speech_text(text) == text


def test_fenced_code_block_is_removed():
    text = "Here's the fix:\n\n```python\ndef search_products(query):\n    return results\n```\n\nThat should do it."
    out = to_speech_text(text)
    assert "def search_products" not in out
    assert "```" not in out
    assert "Here's the fix" in out
    assert "That should do it" in out


def test_raw_json_tool_call_blob_is_removed():
    text = 'Sure, one moment. {"tool":"web_search","arguments":{"query":"jarvis"}} Done, here are the results.'
    out = to_speech_text(text)
    assert '"tool"' not in out
    assert "web_search" not in out
    assert "Done, here are the results" in out


def test_nested_json_is_fully_removed():
    text = 'Result: {"outer": {"inner": {"key": "value"}}} — all good.'
    out = to_speech_text(text)
    assert "{" not in out and "}" not in out
    assert "all good" in out


def test_iso_timestamp_is_removed():
    text = "The event happened at 2026-09-01T05:13:00-05:00 this morning."
    out = to_speech_text(text)
    assert "2026-09-01" not in out
    assert "this morning" in out


def test_stack_trace_is_removed():
    text = (
        "Something went wrong.\n"
        "Traceback (most recent call last):\n"
        '  File "main.py", line 42, in run\n'
        "    raise ValueError('bad')\n"
        "ValueError: bad\n"
        "Try again shortly."
    )
    out = to_speech_text(text)
    assert "Traceback" not in out
    assert "File \"main.py\"" not in out
    assert "ValueError" not in out
    assert "Something went wrong" in out
    assert "Try again shortly" in out


def test_snake_case_tool_call_syntax_is_removed():
    text = "I ran search_products(query='desk lamp', limit=5) and found some options."
    out = to_speech_text(text)
    assert "search_products" not in out
    assert "found some options" in out


def test_ordinary_parenthetical_with_no_underscore_or_kwarg_survives():
    text = "The email(s) will be sent shortly, at a small profit (roughly ten percent)."
    out = to_speech_text(text)
    assert "email(s)" in out
    assert "profit (roughly ten percent)" in out


def test_markdown_formatting_is_stripped_not_read_aloud():
    text = "# Update\n\n**Status:** good\n\n- item one\n- item two\n\n1. first\n2. second\n\nSee [the doc](https://example.com/x)."
    out = to_speech_text(text)
    assert "#" not in out
    assert "**" not in out
    assert out.count("-") == 0 or "item one" in out
    assert "https://example.com" not in out
    assert "the doc" in out


def test_inline_code_is_unwrapped_to_plain_words():
    text = "Set the `voice_provider` field to `elevenlabs` in the config."
    out = to_speech_text(text)
    assert "`" not in out
    assert "voiceprovider" in out   # unwrapped and de-snaked, not deleted — still speakable, not code syntax
    assert "elevenlabs" in out


def test_empty_and_none_safe():
    assert to_speech_text("") == ""
    assert to_speech_text(None) == ""


def test_idempotent():
    text = 'Ran {"tool":"x"} then ```print(1)``` at 2026-01-01T00:00:00Z, done.'
    once = to_speech_text(text)
    twice = to_speech_text(once)
    assert once == twice
