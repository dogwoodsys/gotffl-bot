"""Post length and thread splitting.

X does not count characters — it counts a weighted length. Most characters
weigh 1, but CJK and emoji weigh 2. A 12-team league where two owners put emoji
in their team names can render a post that is 265 "characters" and still be
rejected at 281 weighted units. Counting with len() would look correct in every
test written in ASCII and fail the first week of the season.
"""

import unicodedata

MAX_WEIGHTED = 280

# Ranges X weights as 2. From X's published character-counting rules.
_WIDE_RANGES = (
    (0x1100, 0x115F),
    (0x2329, 0x232A),
    (0x2E80, 0x303E),
    (0x3041, 0x33FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xA000, 0xA4CF),
    (0xA960, 0xA97F),
    (0xAC00, 0xD7A3),
    (0xF900, 0xFAFF),
    (0xFE10, 0xFE19),
    (0xFE30, 0xFE6F),
    (0xFF00, 0xFF60),
    (0xFFE0, 0xFFE6),
    (0x1F300, 0x1F64F),
    (0x1F900, 0x1F9FF),
    (0x20000, 0x3FFFD),
)


def _weight(char: str) -> int:
    # Combining marks attach to the preceding character and cost nothing.
    if unicodedata.combining(char):
        return 0
    # Zero-width joiners and variation selectors bind emoji sequences together.
    if char in ("‍", "️", "︎"):
        return 0
    code = ord(char)
    return 2 if any(lo <= code <= hi for lo, hi in _WIDE_RANGES) else 1


def weighted_length(text: str) -> int:
    """X's character count for `text`, not its Python length."""
    return sum(_weight(c) for c in unicodedata.normalize("NFC", text))


def fits(text: str) -> bool:
    return weighted_length(text) <= MAX_WEIGHTED


class SegmentTooLong(ValueError):
    """An indivisible unit exceeds one post.

    Raised rather than truncated. A tweet cut mid-word is worse than no tweet:
    the bot goes quiet and alarms, which is visible, instead of posting
    something garbled, which is public.
    """


def split_thread(items: list[str], header: str = "") -> list[str]:
    """Pack `items` into as few posts as possible, in order.

    Every returned segment satisfies `fits()`. Items are never split across
    posts — a standings row belongs in one post or the thread is wrong. If a
    single item cannot fit even alone, raise rather than mangle it.

    `header` is prefixed to the first segment only; the numbering suffix
    (" 1/3") is added afterwards, and its own width is accounted for.
    """
    if not items:
        return []

    # Numbering widens every segment, and the count depends on the packing,
    # which depends on the width. Grow the reserve until the packing is stable.
    reserve = 0
    while True:
        segments = _pack(items, header, reserve)
        needed = 0 if len(segments) == 1 else weighted_length(f" {len(segments)}/{len(segments)}")
        if needed <= reserve:
            break
        reserve = needed

    if len(segments) == 1:
        return segments
    total = len(segments)
    return [f"{seg} {i}/{total}" for i, seg in enumerate(segments, 1)]


def _pack(items: list[str], header: str, reserve: int) -> list[str]:
    budget = MAX_WEIGHTED - reserve
    segments: list[str] = []
    current = header
    for item in items:
        candidate = f"{current}\n{item}" if current else item
        if weighted_length(candidate) <= budget:
            current = candidate
            continue
        if current and current != header:
            segments.append(current)
            current = ""
            candidate = item
        if weighted_length(candidate) > budget:
            raise SegmentTooLong(
                f"item of {weighted_length(item)} weighted units cannot fit in {budget}"
            )
        current = candidate
    if current and current != header:
        segments.append(current)
    return segments or [header]
