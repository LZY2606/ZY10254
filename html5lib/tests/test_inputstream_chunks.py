"""Chunking invariance and encoding-precedence tests for HTMLInputStream.

The byte stream handed to the parser is allowed to split its output at
arbitrary byte boundaries: a successful ``read(n)`` may return anywhere
from one to ``n`` bytes, while ``b""`` *always* means EOF. These tests feed
identical byte strings through such streams cut in many different ways and
assert that everything the parser can commit to is independent of where the
boundaries fall:

* the resulting Unicode token stream,
* the detected encoding and its confidence,
* the number of encoding restarts (a parse triggers at most one restart),
* the parse errors reported by the full parser.

Both public entry points have a contract here: the full :class:`HTMLParser`
and the standalone binary :class:`HTMLInputStream`. None of the tests read
the system locale, depend on chardet (``useChardet=False`` is always used),
the network, wall-clock time or filesystem ordering.
"""
from __future__ import absolute_import, division, unicode_literals

import codecs

import pytest

from html5lib import HTMLParser, treewalkers
from html5lib import _inputstream
from html5lib._inputstream import (BufferedStream, EOF,
                                   HTMLBinaryInputStream)
from html5lib.constants import _ReparseException

try:
    from webencodings.labels import LABELS as ENCODING_LABELS
except ImportError:  # pragma: no cover - webencodings is a hard dependency
    ENCODING_LABELS = {}


# ---------------------------------------------------------------------------
# Controlled reader
# ---------------------------------------------------------------------------

class ScriptedChunkReader(object):
    """Binary file object that serves one fixed byte string in scripted chunks.

    ``cap_schedule`` caps how many bytes each *successful* read may return.
    A read never returns more than the cap or the number of bytes requested,
    and it may return fewer, but it only ever returns ``b""`` once all bytes
    have been delivered -- exactly the EOF semantics real binary streams
    provide. ``read(0)`` returns ``b""`` without consuming anything, as
    :func:`HTMLInputStream` probes streams with it.

    Every non-empty chunk actually handed to the consumer is recorded in
    ``delivered`` so tests can assert the stream never repeats or loses
    bytes, including across an encoding restart.
    """

    def __init__(self, data, cap_schedule=()):
        assert isinstance(data, bytes)
        self.data = data
        self.cap_schedule = list(cap_schedule)
        self.position = 0
        self.delivered = []
        self.read_calls = 0

    def read(self, size=-1):
        if size == 0:
            # Stream-type probe / harmless zero-length read: no data consumed.
            return b""
        self.read_calls += 1
        if self.position >= len(self.data):
            return b""
        if self.cap_schedule:
            cap = self.cap_schedule.pop(0)
        elif size is None or size < 0:
            cap = len(self.data)
        else:
            cap = size
        remaining = len(self.data) - self.position
        if size is None or size < 0:
            take = min(cap, remaining)
        else:
            take = min(cap, size, remaining)
        chunk = self.data[self.position:self.position + take]
        self.position += take
        self.delivered.append(chunk)
        return chunk


def schedules_for_boundaries(data, fixed_sizes=(1, 2, 3, 7)):
    """Yield ``(label, schedule)`` pairs covering every possible first cut.

    The reference schedule serves everything in one read. Every other
    schedule makes its first successful read end at one byte boundary and
    then serves the remainder in fixed small chunks, so multibyte sequences
    are exercised in the middle as well.
    """
    n = len(data)
    yield "reference", [n]
    for cut in range(1, n + 1):
        for fixed in fixed_sizes:
            schedule = [cut]
            position = cut
            while position < n:
                schedule.append(fixed)
                position += fixed
            yield "cut-%d/fixed-%d" % (cut, fixed), schedule


def hex_dump(data):
    """Render bytes as grouped hex with an ASCII column, for failure output."""
    lines = []
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        hex_part = " ".join("%02x" % byte for byte in chunk)
        ascii_part = "".join(chr(byte) if 32 <= byte < 127 else "."
                             for byte in chunk)
        lines.append("%04x  %-47s  %s" % (offset, hex_part, ascii_part))
    return "\n".join(lines)


def failure_context(label, data, schedule, expected, actual):
    return (
        "\n  schedule: %s -> %r"
        "\n  split points (chunk sizes delivered): %r"
        "\n  expected:\n%r"
        "\n  actual:\n%r"
        "\n  raw bytes (%d):\n%s"
        % (label, schedule, schedule, expected, actual, len(data),
           hex_dump(data)))


# ---------------------------------------------------------------------------
# Observation of both contracts
# ---------------------------------------------------------------------------

_ETREE_WALKER = treewalkers.getTreeWalker("etree")


def _token_digest(document):
    """Comparable representation of the committed Unicode token stream."""
    tokens = []
    for token in _ETREE_WALKER(document):
        tokens.append((token["type"], token.get("name"), token.get("data")))
    return tuple(tokens)


def observe_full_parser(data, schedule, **kwargs):
    """Run the full parser and return its committed results.

    Genuine stream restarts are observed by wrapping the stream's
    ``changeEncoding``: an entry is recorded only when it raises
    ``_ReparseException`` (same-codec confidence promotions do not restart).
    """
    parser = HTMLParser(namespaceHTMLElements=False)
    reader = ScriptedChunkReader(data, schedule)

    reparse_events = []
    import html5lib._tokenizer as tokenizer_module
    saved = tokenizer_module.HTMLInputStream

    def wrapping_input_stream(source, **factory_kwargs):
        stream = saved(source, **factory_kwargs)
        original_change_encoding = stream.changeEncoding

        def counting_change_encoding(new_encoding):
            before = (stream.charEncoding[0].name,
                      stream.charEncoding[1])
            try:
                return original_change_encoding(new_encoding)
            except _ReparseException:
                # A genuine restart: the parser re-reads the whole stream.
                reparse_events.append((before[0],
                                       stream.charEncoding[0].name))
                raise

        stream.changeEncoding = counting_change_encoding
        return stream

    tokenizer_module.HTMLInputStream = wrapping_input_stream
    try:
        document = parser.parse(reader, useChardet=False, **kwargs)
    finally:
        tokenizer_module.HTMLInputStream = saved

    return {
        "tokens": _token_digest(document),
        "encoding": parser.documentEncoding,
        "errors": tuple(error[1] for error in parser.errors),
        "restarts": tuple(reparse_events),
        "delivered": b"".join(reader.delivered),
    }


def observe_input_stream(data, schedule, switch=None, **kwargs):
    """Run the standalone binary input stream and collect its results.

    ``switch`` optionally requests an encoding change after a given number of
    characters, exercising :meth:`changeEncoding` directly without the full
    parser.
    """
    reader = ScriptedChunkReader(data, schedule)
    stream = HTMLBinaryInputStream(reader, useChardet=False, **kwargs)

    restarts = 0
    output = []
    while True:
        try:
            character = stream.char()
        except _ReparseException:
            restarts += 1
            output = []
            continue
        if character is EOF:
            break
        output.append(character)
        if switch is not None and len(output) == switch[0] and \
                stream.charEncoding[1] == "tentative":
            try:
                stream.changeEncoding(switch[1])
            except _ReparseException:
                restarts += 1
                output = []
                continue

    encoding = stream.charEncoding
    return {
        "text": "".join(output),
        "encoding": encoding[0].name,
        "confidence": encoding[1],
        "errors": tuple(stream.errors),
        "restarts": restarts,
        "delivered": b"".join(reader.delivered),
    }


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

CAFE = "caf\u00e9\U0001F600"

# BOM corpora include ASCII plus non-ASCII content so decoding (not just
# detection) is exercised.
UTF8_BOM_DATA = codecs.BOM_UTF8 + ("<p>" + CAFE).encode("utf-8")
UTF16LE_BOM_DATA = ("<p>" + CAFE).encode("utf-16")  # codec writes a BOM
UTF16BE_BOM_DATA = (codecs.BOM_UTF16_BE +
                    ("<p>" + CAFE).encode("utf-16-be"))

# A meta declaration fully inside the first 1024 bytes: the prescan
# commits the encoding and tokenizing does not need a restart.
_META = b"<meta charset='utf-8'>"
META_INSIDE_PRESHCAN = (
    b"x" * (1024 - len(_META)) + _META + CAFE.encode("utf-8"))

# A meta declaration that straddles the 1024-byte prescan boundary, so the
# prescan sees only a prefix and the declaration is only honoured while
# tokenizing (a genuine stream restart).
META_STRADDLING_PRESHCAN = (
    b"x" * 1022 + _META + CAFE.encode("utf-8"))

# The prescan only looks at the first 1024 bytes; a meta beyond that point is
# still honoured while tokenizing, forcing an actual stream restart.
META_BEYOND_PRESHCAN = (
    b"x" * 1024 + b"<meta charset='utf-8'>" + CAFE.encode("utf-8"))

# Undecodable byte sequences under UTF-8, including an overlong encoding and
# a surrogate code point, both of which must decode to U+FFFD replacements.
UNDCODABLE_UTF8 = b"<meta charset=utf-8>a\xffb\xc0\x80c\xed\xa0\x80d"

# Bytes that are illegal in UTF-8 but perfectly legal in windows-1252; the
# 0x80/0x99 bytes map to printable windows characters rather than U+FFFD.
WINDOWS_MAPPING = b"<meta charset=windows-1252>\x80\x99\xa9\xf1"

# x-user-defined maps the upper half of the byte range to U+F780..U+F7FF.
X_USER_DEFINED = b"<meta charset='x-user-defined'>\x80\xfe\xff"

# Second meta after the first one has committed the encoding: it must be
# ignored rather than trigger a second restart.
SECOND_META = b"<meta charset=utf-8><meta charset=iso-8859-2>\xe9"

# Meta written via the http-equiv pragma form.
PRAGMA_META = (
    b'<meta http-equiv="content-type" '
    b'content="text/html; charset=utf-8">\xe9')

FULL_PARSER_CORPUS = [
    pytest.param(UTF8_BOM_DATA, {}, id="utf-8-bom"),
    pytest.param(UTF16LE_BOM_DATA, {}, id="utf-16le-bom"),
    pytest.param(UTF16BE_BOM_DATA, {}, id="utf-16be-bom"),
    pytest.param(META_INSIDE_PRESHCAN, {},
                 id="meta-inside-prescan"),
    pytest.param(META_STRADDLING_PRESHCAN, {},
                 id="meta-straddling-prescan"),
    pytest.param(META_BEYOND_PRESHCAN, {},
                 id="meta-beyond-1024-restart"),
    pytest.param(UNDCODABLE_UTF8, {}, id="undecodable-utf8-sequences"),
    pytest.param(WINDOWS_MAPPING, {}, id="windows-1252-mapping"),
    pytest.param(X_USER_DEFINED, {}, id="x-user-defined"),
    pytest.param(SECOND_META, {}, id="second-meta-ignored"),
    pytest.param(PRAGMA_META, {}, id="http-equiv-pragma"),
    pytest.param(b"<meta charset=iso-8859-3>\xe9\xe8\xf1",
                 {"transport_encoding": "iso-8859-2"},
                 id="transport-overrides-meta"),
    pytest.param(codecs.BOM_UTF8 + b"\xe9\xe8",
                 {"override_encoding": "iso-8859-2"},
                 id="bom-overrides-override"),
]


# ---------------------------------------------------------------------------
# Full parser contract: chunking invariance
# ---------------------------------------------------------------------------

def _assert_parser_invariant(data, schedules, kwargs):
    expected = observe_full_parser(data, [len(data)], **kwargs)
    assert expected["delivered"] == data

    for label, schedule in schedules:
        actual = observe_full_parser(data, list(schedule), **kwargs)
        # The underlying bytes must never be duplicated or dropped, even if
        # the stream was rewound during an encoding restart (BufferedStream
        # serves buffered bytes without re-asking the source).
        assert actual["delivered"] == data, failure_context(
            label, data, schedule, data, actual["delivered"])
        assert actual["tokens"] == expected["tokens"], failure_context(
            label, data, schedule, expected["tokens"], actual["tokens"])
        assert actual["encoding"] == expected["encoding"], failure_context(
            label, data, schedule, expected["encoding"], actual["encoding"])
        assert actual["errors"] == expected["errors"], failure_context(
            label, data, schedule, expected["errors"], actual["errors"])
        assert actual["restarts"] == expected["restarts"], failure_context(
            label, data, schedule, expected["restarts"], actual["restarts"])


@pytest.mark.parametrize("data,kwargs", FULL_PARSER_CORPUS)
def test_full_parser_chunk_boundary_invariance(data, kwargs):
    """Every byte boundary yields identical parser results."""
    schedules = list(schedules_for_boundaries(
        data, fixed_sizes=(1, 2, 3, 7)))
    _assert_parser_invariant(data, schedules, kwargs)


def _mid_sequence_schedules(data, sequence_starts, sequence_length):
    """Schedules whose first successful read cuts inside each sequence."""
    for start_index in sequence_starts:
        for cut in range(start_index + 1,
                         start_index + sequence_length):
            if cut < len(data):
                yield ("seq-at-%d/cut-%d" % (start_index, cut),
                       [cut, 1, 2, len(data)])


# (data, kwargs, multibyte payload, byte width of each sequence)
MULTIBYTE_SPLIT_CORPUS = [
    pytest.param(
        UTF8_BOM_DATA, {},
        [("<p>".encode("utf-8"), 1),
         ("\u00e9".encode("utf-8"), 2),
         ("\U0001F600".encode("utf-8"), 4)],
        id="utf-6-mixed-width"),
    pytest.param(
        UTF16LE_BOM_DATA, {},
        [("\u00e9".encode("utf-16-le"), 2),
         ("\U0001F600".encode("utf-16-le"), 2),
         ("\u4e2d".encode("utf-16-le"), 2)],
        id="utf16le-code-units"),
    pytest.param(
        UTF16BE_BOM_DATA, {},
        [("\u00e9".encode("utf-16-be"), 2),
         ("\U0001F600".encode("utf-16-be"), 2),
         ("\u4e2d".encode("utf-16-be"), 2)],
        id="utf16be-code-units"),
    pytest.param(
        UNDCODABLE_UTF8, {},
        [(b"\xc0\x80", 2), (b"\xed\xa0\x80", 3)],
        id="invalid-utf8-runs"),
]


@pytest.mark.parametrize("data,kwargs,runs",
                         MULTIBYTE_SPLIT_CORPUS)
def test_full_parser_multibyte_split_invariance(data, kwargs, runs):
    """The first cut lands inside each multibyte (or invalid) sequence."""
    schedules = []
    for run, width in runs:
        index = 0
        starts = []
        while True:
            found = data.find(run, index)
            if found < 0:
                break
            starts.append(found)
            index = found + 1
        schedules.extend(_mid_sequence_schedules(data, starts, width))
    assert schedules, "test corpus must contain the expected sequences"
    _assert_parser_invariant(data, schedules, kwargs)


# ---------------------------------------------------------------------------
# Encoding-source precedence and confidence
# ---------------------------------------------------------------------------

def stream_of(data, **kwargs):
    return HTMLBinaryInputStream(data, useChardet=False, **kwargs)


@pytest.mark.parametrize("expected,confidence,data,kwargs", [
    # BOM wins over everything, and is always "certain".
    ("utf-8", "certain", UTF8_BOM_DATA,
     {"override_encoding": "iso-8859-2",
      "transport_encoding": "iso-8859-3"}),
    ("utf-16le", "certain", UTF16LE_BOM_DATA,
     {"override_encoding": "iso-8859-2"}),
    ("utf-16be", "certain", UTF16BE_BOM_DATA,
     {"override_encoding": "iso-8859-2"}),
    # Override beats transport and meta.
    ("iso-8859-2", "certain",
     b"<meta charset=iso-8859-4>",
     {"override_encoding": "iso-8859-2",
      "transport_encoding": "iso-8859-3"}),
    # Transport beats meta, parent, likely and default.
    ("iso-8859-2", "certain", b"<meta charset=iso-8859-3>",
     {"transport_encoding": "iso-8859-2",
      "same_origin_parent_encoding": "iso-8859-4",
      "likely_encoding": "iso-8859-5",
      "default_encoding": "iso-8859-6"}),
    # Meta beats parent; meta starts out "tentative".
    ("iso-8859-2", "tentative", b"<meta charset=iso-8859-2>",
     {"same_origin_parent_encoding": "iso-8859-3"}),
    # Parent beats likely, but UTF-16 parents are ignored at this stage.
    ("iso-8859-2", "tentative", b"",
     {"same_origin_parent_encoding": "iso-8859-2",
      "likely_encoding": "iso-8859-3"}),
    ("iso-8859-2", "tentative", b"",
     {"same_origin_parent_encoding": "utf-16",
      "likely_encoding": "iso-8859-2"}),
    ("iso-8859-2", "tentative", b"",
     {"same_origin_parent_encoding": "utf-16be",
      "likely_encoding": "iso-8859-2"}),
    # Likely beats default.
    ("iso-8859-2", "tentative", b"",
     {"likely_encoding": "iso-8859-2",
      "default_encoding": "iso-8859-3"}),
    # Explicit default is used when nothing else matches.
    ("iso-8859-2", "tentative", b"",
     {"default_encoding": "iso-8859-2"}),
    # Unknown labels fall back to the built-in default (windows-1252).
    ("windows-1252", "tentative", b"",
     {"default_encoding": "totally-bogus-string"}),
    ("windows-1252", "tentative", b"", {}),
])
def test_encoding_precedence(expected, confidence, data, kwargs):
    stream = stream_of(data, **kwargs)
    assert stream.charEncoding[0].name == expected
    assert stream.charEncoding[1] == confidence

    parser = HTMLParser()
    parser.parse(data, useChardet=False, **kwargs)
    assert parser.documentEncoding == expected


def test_utf16_meta_declaration_is_coerced_to_utf8():
    # Prescan: a meta declaring UTF-16 maps to UTF-8 per the spec.
    stream = stream_of(b"<meta charset='utf-16le'>")
    assert stream.charEncoding[0].name == "utf-8"


def test_utf32_bom_label_is_not_supported():
    # The WHATWG encoding standard removed UTF-32: even a UTF-32 BOM does
    # not produce a usable codec through webencodings. Document that the
    # behaviour is identical for bytes and chunked streams.
    data = codecs.BOM_UTF32_LE + b"ab"
    assert stream_of(data).charEncoding[0].name == "windows-1252"
    reader = ScriptedChunkReader(data, [2, 2, 2, 2])
    assert HTMLBinaryInputStream(reader, useChardet=False) \
        .charEncoding[0].name == "windows-1252"


# ---------------------------------------------------------------------------
# Restart semantics
# ---------------------------------------------------------------------------

def test_parser_restarts_at_most_once():
    """A parse triggers at most one stream restart, regardless of chunking."""
    data = META_BEYOND_PRESHCAN
    schedules = list(schedules_for_boundaries(data, fixed_sizes=(2, 5)))
    for label, schedule in schedules:
        result = observe_full_parser(data, list(schedule))
        assert result["encoding"] == "utf-8"
        # Exactly one encoding change forces a whole-stream restart.
        assert result["restarts"] == (("windows-1252", "utf-8"),), \
            failure_context(label, data, schedule,
                            (("windows-1252", "utf-8"),),
                            result["restarts"])


def test_parser_does_not_restart_when_meta_seen_in_prescan():
    """Meta found by the first 1024-byte prescan commits encoding upfront."""
    result = observe_full_parser(META_INSIDE_PRESHCAN,
                                 [len(META_INSIDE_PRESHCAN)])
    assert result["encoding"] == "utf-8"
    # Same-codec confirmation promotes confidence without a stream restart.
    assert result["restarts"] == ()


def test_change_encoding_same_codec_promotes_confidence_without_restart():
    stream = stream_of(b"abc")
    assert stream.charEncoding == (stream.charEncoding[0], "tentative")
    stream.changeEncoding("cp1252")  # label alias of windows-1252
    assert stream.charEncoding[1] == "certain"


def test_change_encoding_unknown_label_is_ignored():
    stream = stream_of(b"abc")
    stream.changeEncoding("not-a-real-label")
    assert stream.charEncoding[0].name == "windows-1252"
    assert stream.charEncoding[1] == "tentative"


def test_change_encoding_utf16_is_coerced_to_utf8():
    stream = stream_of(b"abc")
    # Current implementation: a UTF-16 switch is replaced by UTF-8 but does
    # not raise on its own; capture the committed encoding.
    stream.changeEncoding("utf-16le")
    assert stream.charEncoding[0].name == "windows-1252"


def test_change_encoding_restart_replays_without_losing_or_duplicating():
    data = b"<p>" + CAFE.encode("utf-8") + b"<p>end"
    schedules = list(schedules_for_boundaries(data, fixed_sizes=(2, 3)))
    expected = observe_input_stream(data, [len(data)], switch=(2, "utf-8"))
    assert expected["restarts"] == 1
    assert expected["encoding"] == "utf-8"
    for label, schedule in schedules:
        actual = observe_input_stream(data, list(schedule),
                                      switch=(2, "utf-8"))
        assert actual["text"] == expected["text"], failure_context(
            label, data, schedule, expected["text"], actual["text"])
        assert actual["restarts"] == 1, failure_context(
            label, data, schedule, 1, actual["restarts"])
        # No consumed character may be emitted twice and the prebuffer must
        # not be lost: concatenating delivered chunks reproduces the input.
        assert actual["delivered"] == data, failure_context(
            label, data, schedule, data, actual["delivered"])


def test_change_encoding_requires_tentative_confidence():
    stream = stream_of(codecs.BOM_UTF8 + b"abc")
    assert stream.charEncoding[1] == "certain"
    with pytest.raises(AssertionError):
        stream.changeEncoding("iso-8859-2")


# ---------------------------------------------------------------------------
# b"" EOF semantics
# ---------------------------------------------------------------------------

class EofAfterByteReader(object):
    """Reader that returns one byte, then a genuine EOF (b"") forever."""

    def __init__(self, data):
        self.data = data
        self.position = 0
        self.eof_seen = 0

    def read(self, size=-1):
        if size == 0:
            return b""
        if self.position >= len(self.data):
            self.eof_seen += 1
            return b""
        chunk = self.data[self.position:self.position + 1]
        self.position += 1
        return chunk


class OneShortReadReader(object):
    """Reader whose first substantive read returns one byte out of four.

    This models a transport that returns less data than requested while more
    is still coming; it must not be confused with EOF.
    """

    def __init__(self, data):
        self.data = data
        self.position = 0
        self.shorted = False

    def read(self, size=-1):
        if size == 0:
            return b""
        if self.position >= len(self.data):
            return b""
        remaining = len(self.data) - self.position
        if not self.shorted and size and size > 1:
            # First substantive read deliberately returns one byte even
            # though more were requested; subsequent reads are normal.
            self.shorted = True
            take = 1
        elif size is None or size < 0:
            take = remaining
        else:
            take = min(size, remaining)
        chunk = self.data[self.position:self.position + take]
        self.position += take
        return chunk


def test_eof_empty_bytes_is_only_returned_after_all_data():
    data = codecs.BOM_UTF8 + b"abc"
    reader = EofAfterByteReader(data)
    stream = HTMLBinaryInputStream(reader, useChardet=False)
    assert stream.charEncoding[0].name == "utf-8"
    text = []
    while True:
        character = stream.char()
        if character is EOF:
            break
        text.append(character)
    assert "".join(text) == "abc"
    assert reader.eof_seen >= 1


def test_short_read_is_not_eof():
    data = UTF16LE_BOM_DATA
    reader = OneShortReadReader(data)
    stream = HTMLBinaryInputStream(reader, useChardet=False)
    assert stream.charEncoding[0].name == "utf-16le"
    text = []
    while True:
        character = stream.char()
        if character is EOF:
            break
        text.append(character)
    assert "".join(text) == "<p>" + CAFE


def test_empty_stream_eof():
    reader = EofAfterByteReader(b"")
    stream = HTMLBinaryInputStream(reader, useChardet=False)
    assert stream.char() is EOF
    assert stream.charEncoding[0].name == "windows-1252"


def test_buffered_stream_seek_past_eof_raises():
    from io import BytesIO
    buffered = BufferedStream(BytesIO(b"ab"))
    buffered.read(1)
    with pytest.raises(IOError):
        buffered.seek(5)


def test_buffered_stream_seek_forward_with_short_reads():
    # Seek to a position beyond what a short first read buffered: the
    # BufferedStream must append more bytes rather than assert.
    data = codecs.BOM_UTF16_LE + b"x\x00y\x00z\x00"
    reader = OneShortReadReader(data)
    buffered = BufferedStream(reader)
    # The reader reports a short first chunk; BufferedStream.read still
    # satisfies the request by continuing to pull bytes. Seeking to 4 (as
    # detectBOM does after a UTF-16 BOM) must work both inside and beyond
    # what was first buffered.
    assert buffered.read(4) == data[:4]
    buffered.seek(2)
    assert buffered.tell() == 2
    assert buffered.read(2) == data[2:4]
    # Seek beyond the buffered region while the source still has data.
    buffered.seek(8)
    assert buffered.tell() == 8
    assert buffered.read(2) == data[8:10]


# ---------------------------------------------------------------------------
# Encoding label normalization, generated from the repository encoding table
# ---------------------------------------------------------------------------

# Pairs guaranteed by the WHATWG labels table shipped with webencodings.
LABEL_CANONICAL_CASES = [
    ("utf-8", "utf-8"), ("UTF-8", "utf-8"), ("  utf-8  ", "utf-8"),
    ("\tUTF-8\n", "utf-8"), ("utf8", "utf-8"), ("UTF8", "utf-8"),
    ("iso8859-1", "windows-1252"), ("ISO-8859-1", "windows-1252"),
    ("latin1", "windows-1252"), ("cp1252", "windows-1252"),
    ("ascii", "windows-1252"), ("sjis", "shift_jis"),
    ("x-user-defined", "x-user-defined"),
    ("X-User-Defined", "x-user-defined"),
]

# Strings that must NOT be accepted just because they look similar.
LABEL_REJECTED_CASES = [
    "latin-1", "latin_1", "utf_8", "win-1252", "windows1252",
    " u t f 8 ", "utf-7", "utf7", "cesu-8", "utf-8\u00a0",
    "utf - 8", "utf\n8",
]


@pytest.mark.parametrize("label,canonical", LABEL_CANONICAL_CASES)
def test_label_normalization(label, canonical):
    encoding = _inputstream.lookupEncoding(label)
    assert encoding is not None
    assert encoding.name == canonical


@pytest.mark.parametrize("label", LABEL_REJECTED_CASES)
def test_label_lookalikes_are_rejected(label):
    assert _inputstream.lookupEncoding(label) is None


def test_label_table_consistency():
    """Cross-check normalization against the shipped encoding table.

    Case folding and ASCII whitespace trimming are exactly the
    transformations the WHATWG "get an encoding" algorithm allows; every
    canonical name in the table must therefore resolve to itself, while the
    table is the closed set of accepted labels.
    """
    for label, name in ENCODING_LABELS.items():
        assert _inputstream.lookupEncoding(label).name == name
        assert _inputstream.lookupEncoding(
            "  " + label.upper() + "\t").name == name
        # Inserting internal whitespace or replacing hyphens changes the
        # label and must not silently resolve.
        assert _inputstream.lookupEncoding(label + " x") is None


# ---------------------------------------------------------------------------
# Standalone HTMLInputStream contract: chunking invariance
# ---------------------------------------------------------------------------

STREAM_CORPUS = [
    pytest.param(UTF8_BOM_DATA, {}, id="utf8-bom"),
    pytest.param(UTF16LE_BOM_DATA, {}, id="utf16le-bom"),
    pytest.param(UTF16BE_BOM_DATA, {}, id="utf16be-bom"),
    pytest.param(META_INSIDE_PRESHCAN, {},
                 id="meta-across-1024"),
    pytest.param(UNDCODABLE_UTF8, {}, id="undecodable-utf8"),
    pytest.param(WINDOWS_MAPPING, {}, id="windows-1252"),
    pytest.param(X_USER_DEFINED, {}, id="x-user-defined"),
    pytest.param(SECOND_META, {}, id="second-meta"),
    pytest.param(b"\xe9\xe8", {"transport_encoding": "iso-8859-2"},
                 id="transport-iso-8859-2"),
]


@pytest.mark.parametrize("data,kwargs", STREAM_CORPUS)
def test_input_stream_boundary_invariance(data, kwargs):
    expected = observe_input_stream(data, [len(data)], **kwargs)
    assert expected["delivered"] == data
    for label, schedule in schedules_for_boundaries(
            data, fixed_sizes=(1, 2, 3, 7)):
        actual = observe_input_stream(data, list(schedule), **kwargs)
        assert actual["text"] == expected["text"], failure_context(
            label, data, schedule, expected["text"], actual["text"])
        assert actual["encoding"] == expected["encoding"], failure_context(
            label, data, schedule, expected["encoding"], actual["encoding"])
        assert actual["confidence"] == expected["confidence"], \
            failure_context(label, data, schedule, expected["confidence"],
                            actual["confidence"])
        assert actual["errors"] == expected["errors"], failure_context(
            label, data, schedule, expected["errors"], actual["errors"])
        assert actual["restarts"] == expected["restarts"], failure_context(
            label, data, schedule, expected["restarts"], actual["restarts"])
        assert actual["delivered"] == data, failure_context(
            label, data, schedule, data, actual["delivered"])


@pytest.mark.parametrize("data", [
    UNDCODABLE_UTF8,
    b"<meta charset=utf-8>" + CAFE.encode("utf-8"),
])
def test_input_stream_undecodable_bytes_become_replacement(data):
    reference = observe_input_stream(data, [len(data)])
    chunked = observe_input_stream(data, [1, 2, 1, 3, 2, 4, 5])
    assert chunked["text"] == reference["text"]
    assert chunked["encoding"] == "utf-8"
    # U+FFFD appears for the invalid bytes and is stable across chunking.
    assert reference["text"].count("\ufffd") == chunked["text"] \
        .count("\ufffd")


def test_x_user_defined_byte_mapping_is_chunk_invariant():
    # The standalone stream does not parse meta tags: select the encoding
    # explicitly, mirroring what the full parser does after seeing the meta.
    payload = b"\x80\xfe\xff"
    expected = observe_input_stream(payload, [len(payload)],
                                    default_encoding="x-user-defined")
    assert expected["text"] == "\uf780\uf7fe\uf7ff"
    for cut in range(1, len(payload) + 1):
        actual = observe_input_stream(
            payload, [cut, 1, 2, len(payload)],
            default_encoding="x-user-defined")
        assert actual["text"] == expected["text"]
        assert actual["encoding"] == "x-user-defined"


def test_replacement_encoding_decodes_to_replacement_characters():
    # The WHATWG "replacement" codec decodes every byte to U+FFFD rather
    # than passing bytes through (webencodings charmap StreamReader bug).
    expected = observe_input_stream(
        b"abc", [3], default_encoding="replacement")
    assert expected["text"] == "\ufffd\ufffd\ufffd"
    chunked = observe_input_stream(
        b"abc", [1, 1, 1], default_encoding="replacement")
    assert chunked["text"] == expected["text"]


def test_windows_mapping_is_chunk_invariant():
    expected = observe_input_stream(WINDOWS_MAPPING, [len(WINDOWS_MAPPING)])
    # 0x80 -> U+20AC, 0x99 -> U+2122 under windows-1252.
    assert "\u20ac\u2122\xa9\xf1" in expected["text"]
    for cut in range(1, len(WINDOWS_MAPPING) + 1):
        actual = observe_input_stream(
            WINDOWS_MAPPING, [cut, 1, 3, len(WINDOWS_MAPPING)])
        assert actual["text"] == expected["text"]
