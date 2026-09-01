"""Text-to-speech formatting: the last line of defense between whatever
the LLM produced and the audio that actually reaches a speaker.

Architecture note (see the J-series "JARVIS reads code aloud" fix): the
system prompt already tells the model never to speak raw code, JSON, tool
syntax, or timestamps in a conversational answer, and the browser client
runs its own cleanForSpeech() before ever calling /tts/speak. Neither of
those is a hard guarantee — a prompt can be ignored, and a client-side
filter only protects the browser tab that shipped it (the phone line's
Cartesia agent gets `reply` straight from /api/voice/ask, never through
index.html's JS at all). This module is the one place that guarantee is
actually enforced, so it is applied server-side, right before synthesis,
on every surface that turns JARVIS text into audio.

This never touches the text shown in a chat panel — only the copy handed
to a TTS engine. A user who explicitly asks to hear code read aloud is a
product decision for the prompt/UI layer, not something this filter can
distinguish from any other text at this final stage, so it always strips;
that trade-off is intentional given the fix this exists for.
"""
from __future__ import annotations

import re

_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.*?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_NUMBERED_RE = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_UNDERSCORE_TILDE_RE = re.compile(r"[_~]")

# ISO-8601-ish timestamps: 2026-09-01T05:13:00-05:00, 2026-09-01 05:13:00Z, etc.
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)

# Python/JS stack-trace lines and tracebacks.
_TRACEBACK_HEADER_RE = re.compile(r"^Traceback \(most recent call last\):.*$", re.MULTILINE)
_STACK_FRAME_RE = re.compile(
    r'^\s*(?:File "[^"]+", line \d+, in \S+|at \S+ \([^)]*\)|at \S+:\d+:\d+)\s*$',
    re.MULTILINE,
)
_EXCEPTION_LINE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)\b.*$", re.MULTILINE)

# A curly-brace object containing at least one "key": pair — JSON payloads
# and tool-call-shaped blobs. Applied repeatedly so nested objects (which
# this non-greedy, non-nested pattern can't match in one pass) still get
# fully removed layer by layer.
_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\"[A-Za-z_][A-Za-z0-9_]*\"\s*:[^{}]*\}")

# A bare function/tool-call signature: search_products(query="x"),
# get_status(). Deliberately narrow — requires a snake_case identifier or
# kwarg-style content — so ordinary English parentheticals like "the
# email(s) will be sent" or "profit (roughly)" are never touched.
_CALL_SYNTAX_RE = re.compile(
    r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\([^()\n]{0,200}\)"   # snake_case_name(...)
    r"|\b[a-zA-Z_][a-zA-Z0-9_]*\([^()\n]*[\"'=][^()\n]{0,200}\)"  # foo(x="y") / foo(x=1)
)

_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{2,}")


def to_speech_text(text: str) -> str:
    """Reduce arbitrary JARVIS reply text to what should actually be
    spoken. Idempotent and safe on plain conversational text — it only
    ever removes patterns that look like code/data, never rewords normal
    sentences."""
    if not text:
        return ""

    out = text

    out = _TRACEBACK_HEADER_RE.sub("", out)
    out = _STACK_FRAME_RE.sub("", out)
    out = _EXCEPTION_LINE_RE.sub("", out)

    out = _FENCED_CODE_RE.sub("", out)
    out = _INLINE_CODE_RE.sub(r"\1", out)

    # Repeat until stable (bounded) to unwind nested JSON objects one
    # bracket-depth at a time.
    for _ in range(6):
        new_out = _JSON_OBJECT_RE.sub("", out)
        if new_out == out:
            break
        out = new_out

    out = _CALL_SYNTAX_RE.sub("", out)
    out = _TIMESTAMP_RE.sub("", out)

    out = _BOLD_RE.sub(r"\1", out)
    out = _ITALIC_RE.sub(r"\1", out)
    out = _HEADER_RE.sub("", out)
    out = _BULLET_RE.sub("", out)
    out = _NUMBERED_RE.sub("", out)
    out = _LINK_RE.sub(r"\1", out)
    out = _UNDERSCORE_TILDE_RE.sub("", out)

    out = _BLANK_LINES_RE.sub(" ", out)
    out = _WHITESPACE_RE.sub(" ", out)
    out = "\n".join(line.strip() for line in out.split("\n"))
    out = re.sub(r"\s+", " ", out).strip()
    return out
