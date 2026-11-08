"""Numeric source markers, shared verbatim with the frontend. No source guessing."""
import re

ABSTENTION = "I couldn't find that in the documents available to this assistant."

# Preserve fenced/inline code and Markdown links; their bracketed numbers are data.
# Numeric groups outside them accept [1], [ 1 ], [1, 2], and [1; 2].
_TOKENS = re.compile(
    r"(?P<code>```[\s\S]*?(?:```|\Z)|~~~[\s\S]*?(?:~~~|\Z)|`[^`\n]*`)"
    r"|(?P<link>\[[^\]\n]*\]\([^\n)]*\))"
    r"|(?P<citation>\[\s*(?P<numbers>[0-9]+(?:\s*[,;]\s*[0-9]+)*)\s*\])"
)


def _numbers(match):
    # Avoid unbounded integer conversion for malformed very long model output.
    return [int(n) if len(n) <= 6 else -1
            for n in re.split(r"\s*[,;]\s*", match.group('numbers'))]


def citation_numbers(text):
    """Return unique marker numbers in order, including unresolved numbers."""
    seen = dict.fromkeys(n for m in _TOKENS.finditer(text)
                         if m.group('citation') for n in _numbers(m))
    return list(seen)


def normalize_citations(text):
    """Canonicalize marker spelling without changing which sources were named."""
    def replace(match):
        if not match.group('citation'):
            return match.group(0)
        numbers = _numbers(match)
        if -1 in numbers:
            return match.group(0)  # Remains invalid; never invent a replacement.
        return ' '.join(f'[{n}]' for n in dict.fromkeys(numbers))
    return _TOKENS.sub(replace, text)


def strip_history_citations(text):
    """Old turn-local numbers must not masquerade as current evidence."""
    return _TOKENS.sub(lambda m: '' if m.group('citation') else m.group(0), text)
