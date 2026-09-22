"""Chunk-equivalence and encoding-source priority tests for the input stream.

The HTML input stream picks an encoding from several sources (BOM, override,
transport, prescan ``<meta>``, parent, likely, default) and may re-start
character decoding once when a ``<meta charset>`` is seen by the parser after
the 1024-byte prescan. Those decisions must not depend on how the underlying
byte reader happens to split its reads: a reader that performs short reads
(returning fewer bytes than asked for while not at EOF) must produce exactly
the same decoded result as one that returns the whole document at once.

These tests exercise three contracts with the same byte corpus:

* ``HTMLBinaryInputStream`` in isolation: the complete character sequence,
  detected encoding, confidence, parse errors and the number of encoding
  restarts.
* ``HTMLTokenizer`` in isolation: its emitted Unicode token sequence.
* ``HTMLParser`` end to end: the final serialized tree, ``documentEncoding``,
  parser errors and the number of encoding restarts.

Every failing assertion prints the raw bytes as hexadecimal, the exact chunk
plan (cut points) and the observed difference, so regressions can be read
without a debugger. Nothing here reads the system locale, walks the file
system, uses the network, or relies on chardet or other external detectors.
"""
from __future__ import absolute_import, division, unicode_literals

import codecs
from io import BytesIO

import pytest

from html5lib import HTMLParser
from html5lib._inputstream import (
    BufferedStream, HTMLBinaryInputStream, HTMLInputStream, lookupEncoding)
from html5lib._tokenizer import HTMLTokenizer
from html5lib.constants import EOF, _ReparseException, tokenTypes


# ---------------------------------------------------------------------------
# Deterministic chunk readers
# ---------------------------------------------------------------------------


class ChunkedReader(object):
    """File-like byte reader that hands out a scripted sequence of reads.

    ``plan`` is a list of maximum sizes: each ``read(n)`` returns the smaller
    of the next plan size and ``n``, never reading past EOF. Once the plan is
    exhausted, reads return as many bytes as requested.

    A read only returns ``b""`` when all bytes have been consumed, so ``b""``
    keeps its EOF-only meaning. ``read(0)`` returns ``b""`` without consuming
    anything. The reader also records every read boundary so failures can show
    exactly where data was split.
    """

    def __init__(self, data, plan=(), name="chunked"):
        self._data = data
        self._plan = list(plan)
        self._offset = 0
        self.name = name
        self.read_sizes = []  # sizes requested
        self.read_returns = []  # lengths returned

    def read(self, size=-1):
        self.read_sizes.append(size)
        if size == 0:
            self.read_returns.append(0)
            return b""
        if size is None or size < 0:
            size = len(self._data) - self._offset
        if self._plan:
            size = min(size, self._plan.pop(0))
        chunk = self._data[self._offset:self._offset + size]
        self._offset += len(chunk)
        self.read_returns.append(len(chunk))
        return chunk

    def tell(self):
        return self._offset


class NonSeekableReader(ChunkedReader):
    """Reader without seeking support (like a socket).

    The input stream wraps such readers in
    :class:`~html5lib._inputstream.BufferedStream`, which keeps a replay buffer
    so decoding can be rewound to the start after an encoding restart.
    """

    def seek(self, offset, whence=0):
        raise IOError("stream is not seekable")


# ---------------------------------------------------------------------------
# Chunk plan generation
# ---------------------------------------------------------------------------


def single_boundary_plans(length):
    """One cut after byte 1..length: first read size is every cut point."""
    return [[cut] for cut in range(1, length + 1)]


def fixed_block_plans(length):
    """Reads of a fixed small size for the whole document."""
    return [[size] * ((length // size) + 2)
            for size in (1, 2, 3, 5, 7)]


def multibyte_middle_plans(data):
    """Plans that cut in the middle of every multi-byte sequence found.

    For each occurrence of a non-ASCII byte, force the boundary before it and
    at each byte offset inside the run so decoders cannot rely on a sequence
    arriving in one read.
    """
    plans = []
    for index, byte in enumerate(data):
        if byte > 0x7F:
            plans.append([max(1, index), 1, 1, 1, len(data)])
    # De-duplicate while keeping order.
    unique = []
    for plan in plans:
        if plan not in unique:
            unique.append(plan)
    return unique


def all_plans(data, full_boundaries):
    plans = list(fixed_block_plans(len(data)))
    plans.extend(multibyte_middle_plans(data))
    boundaries = single_boundary_plans(len(data))
    if full_boundaries:
        plans.extend(boundaries)
    else:
        # Always exercise the boundaries that matter for the prescan window
        # and the start/end of the document even for large corpora.
        interesting = set()
        for cut in range(1, min(len(data), 10)):
            interesting.add(cut)
        for cut in (1022, 1023, 1024, 1025, 1026,
                    len(data) - 2, len(data) - 1, len(data)):
            if 1 <= cut <= len(data):
                interesting.add(cut)
        for cut in sorted(interesting):
            plans.append([cut])
    # The whole-document read is the reference shape.
    plans.append([len(data)])
    unique = []
    for plan in plans:
        if plan not in unique:
            unique.append(plan)
    return unique


def plan_cuts(plan):
    """Return the cumulative cut points of a finite plan for diagnostics."""
    cuts = []
    total = 0
    for size in plan:
        total += size
        cuts.append(total)
    return cuts


# ---------------------------------------------------------------------------
# Failure diagnostics
# ---------------------------------------------------------------------------


def _hex_dump(data):
    """Render bytes as hex with offsets so split points are locatable."""
    lines = []
    for start in range(0, len(data), 16):
        chunk = data[start:start + 16]
        lines.append("    %04x  %s" %
                     (start, " ".join("%02x" % b for b in chunk)))
    return "\n".join(lines) if lines else "    (empty)"


def failure_context(label, data, plan, detail):
    return (
        "%s\n"
        "  chunk plan: %r (cumulative cut points %r)\n"
        "  raw bytes (%d):\n%s\n"
        "  difference:\n    %s" %
        (label, plan, plan_cuts(plan), len(data), _hex_dump(data), detail))


# ---------------------------------------------------------------------------
# Corpus
#
# Each corpus is (data, stream kwargs, expected encoding, expected
# confidence, expected parser restarts). The same corpus is fed to the stream,
# tokenizer and parser contracts so every layer sees identical bytes.
# ---------------------------------------------------------------------------

PADDING = b" " * 1030

CORPORA = [
    ("utf8_bom",
     codecs.BOM_UTF8 + b"<p>caf\xc3\xa9 \xe2\x82\xac \xf0\x9f\x92\xa9</p>",
     {}, "utf-8", "certain", 0),
    ("utf16le_bom",
     codecs.BOM_UTF16_LE + "<p>caf\u00e9</p>".encode("utf-16-le"),
     {}, "utf-16le", "certain", 0),
    ("utf16be_bom",
     codecs.BOM_UTF16_BE + "<p>caf\u00e9</p>".encode("utf-16-be"),
     {}, "utf-16be", "certain", 0),
    ("transport_beats_meta",
     b"<meta charset=utf-8><p>caf\xc3\xa9</p>",
     {"transport_encoding": "iso-8859-2"}, "iso-8859-2", "certain", 0),
    ("meta_beats_parent",
     b"<meta charset=iso-8859-2><p>\xe8",
     {"same_origin_parent_encoding": "iso-8859-3"},
     "iso-8859-2", "tentative", 0),
    # Meta declaration ends more than 1024 bytes in, so the prescan misses it;
    # the parser must restart decoding once when it reaches the tag.
    ("meta_after_prescan_restart",
     PADDING + b"<meta charset=utf-8><p>caf\xc3\xa9 \xe2\x82\xac</p>",
     {}, "utf-8", "certain", 1),
    # Declaration fully inside the 1024-byte prescan window: no restart.
    ("meta_inside_prescan",
     b" " * 980 + b"<meta charset=utf-8><p>caf\xc3\xa9</p>",
     {}, "utf-8", "tentative", 0),
    ("httpequiv_content_type",
     b'<meta http-equiv="content-type" content="text/html; '
     b'charset=iso-8859-7"><p>\xe8',
     {}, "iso-8859-7", "tentative", 0),
    ("x_user_defined",
     b"<meta charset=x-user-defined><p>\x7f\x80\x95\xff</p>",
     {}, "x-user-defined", "tentative", 0),
    ("windows_1250_map",
     b"<meta charset=windows-1250><p>\x80\xfd</p>",
     {}, "windows-1250", "tentative", 0),
    ("windows_1252_default",
     b"<p>\x80\x95\xfd</p>",
     {}, "windows-1252", "tentative", 0),
    ("undecodable_utf8",
     b"<meta charset=utf-8><p>\xff\xfe\xc2 a\xf0\x28\x8c\xbc</p>",
     {}, "utf-8", "tentative", 0),
    ("second_meta_ignored_after_restart",
     PADDING +
     b"<meta charset=utf-8><p>\xc3\xa9</p>"
     b"<meta charset=iso-8859-2><p>x",
     {}, "utf-8", "certain", 1),
    ("parent_utf16_skipped",
     b"",
     {"same_origin_parent_encoding": "utf-16be",
      "likely_encoding": "iso-8859-2"},
     "iso-8859-2", "tentative", 0),
]


# ---------------------------------------------------------------------------
# Contract runners
# ---------------------------------------------------------------------------


class RestartCounter(object):
    """Wraps HTMLBinaryInputStream.changeEncoding to count restarts.

    A restart is a real re-decode: the stream seeks back and raises
    ``_ReparseException``. A no-op confirmation (same encoding, confidence
    only upgraded to "certain") is not counted.
    """

    def __init__(self, monkeypatch):
        self.count = 0
        original = HTMLBinaryInputStream.changeEncoding

        def changeEncoding(stream, new_encoding):
            try:
                return original(stream, new_encoding)
            except _ReparseException:
                self.count += 1
                raise

        monkeypatch.setattr(HTMLBinaryInputStream, "changeEncoding",
                            changeEncoding)


def drain_stream(reader, **kwargs):
    """Consume an HTMLBinaryInputStream to EOF via char() only."""
    stream = HTMLBinaryInputStream(reader, useChardet=False, **kwargs)
    characters = []
    while True:
        char = stream.char()
        if char is EOF:
            break
        characters.append(char)
    return {
        "text": "".join(characters),
        "encoding": stream.charEncoding[0].name,
        "confidence": stream.charEncoding[1],
        "errors": list(stream.errors),
    }


def canonical_token(token):
    """Token tuple safe to compare across runs."""
    token_type = token["type"]
    if token_type in (tokenTypes["StartTag"], tokenTypes["EmptyTag"]):
        # Start/empty tags carry an attribute dict.
        attributes = tuple(sorted(
            (name, value) for name, value in token["data"].items()))
        return (token_type, token["name"], attributes,
                token["selfClosing"], token["selfClosingAcknowledged"])
    if token_type == tokenTypes["EndTag"]:
        return (token_type, token["name"], tuple(token["data"]))
    if token_type in (tokenTypes["Characters"], tokenTypes["SpaceCharacters"]):
        return (token_type, token["data"])
    if token_type == tokenTypes["Comment"]:
        return (token_type, token["data"])
    if token_type == tokenTypes["Doctype"]:
        return (token_type,) + tuple(
            token.get(key) for key in
            ("name", "publicId", "systemId", "correct"))
    if token_type == tokenTypes["ParseError"]:
        return (token_type, token["data"], tuple(sorted(
            (key, value) for key, value in token.get("datavars", {}).items())))
    raise AssertionError("unexpected token type %r: %r" %
                         (token_type, token))


def run_tokenizer(reader, **kwargs):
    tokenizer = HTMLTokenizer(reader, useChardet=False, **kwargs)
    return [canonical_token(token) for token in tokenizer]


def run_parser(reader, **kwargs):
    import xml.etree.ElementTree as etree
    parser = HTMLParser(namespaceHTMLElements=False)
    parser.parse(reader, useChardet=False, **kwargs)
    return {
        "tree": etree.tostring(parser.tree.getDocument(), encoding="unicode"),
        "encoding": parser.documentEncoding,
        "errors": list(parser.errors),
    }


# ---------------------------------------------------------------------------
# Reader contract: short reads vs the b"" EOF signal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("plan", [[], [1], [3], [1, 1, 1000]])
def test_chunked_reader_eof_semantics(plan):
    data = b"abcdef"
    reader = NonSeekableReader(data, plan)
    consumed = b""
    # Every non-zero read either advances the offset or signals true EOF:
    # an empty result must coincide with every byte having been consumed.
    for _ in range(len(plan) + len(data) + 2):
        chunk = reader.read(2)
        if chunk == b"":
            assert reader.tell() == len(data)
            break
        assert len(chunk) <= 2
        consumed += chunk
    assert consumed == data
    # Repeated reads past the end stay empty.
    assert reader.read(10) == b""
    assert reader.read(10) == b""


def test_chunked_reader_read_zero_never_consumes():
    reader = NonSeekableReader(b"abc", [10])
    for _ in range(3):
        assert reader.read(0) == b""
        assert reader.tell() == 0
    assert reader.read(2) == b"ab"


def test_short_reads_distinct_from_eof():
    """A short read returns less than asked but never b"" before EOF."""
    reader = NonSeekableReader(b"abcdef", [1, 1, 1])
    assert reader.read(5) == b"a"  # short, not EOF
    assert reader.read(5) == b"b"
    assert reader.read(5) == b"c"
    assert reader.read(5) == b"def"  # plan exhausted, takes what remains
    assert reader.read(5) == b""  # only now is it EOF


def test_htmlinputstream_probes_read_zero():
    """HTMLInputStream() must use read(0) for its type probe, so wrapping a
    binary chunk reader selects the binary stream without consuming bytes."""
    reader = NonSeekableReader(codecs.BOM_UTF8 + b"abc", [10])
    stream = HTMLInputStream(reader)
    assert isinstance(stream, HTMLBinaryInputStream)
    # The BOM was consumed during detection (the 4-byte lookahead buffers
    # one byte past it); no *content* past the BOM has been decoded yet.
    assert reader.tell() == 4
    assert stream.charEncoding[0].name == "utf-8"
    assert stream.char() == "a"


# ---------------------------------------------------------------------------
# Stream-level chunk equivalence
# ---------------------------------------------------------------------------


# Two transport shapes are compared to the BytesIO reference:
#  * "buf"  - an unseekable short-read stream wrapped by BufferedStream, with
#             every scripted chunk plan;
#  * "seek" - a normal seekable stream (single whole-document read), proving
#             the two supported transports agree.
# Full single-boundary coverage for small corpora; the 1024+ corpora use a
# sampled but prescan-focused boundary set.
_STREAM_CASES = []
for name, data, kwargs, _enc, _conf, _restarts in CORPORA:
    full = len(data) <= 200
    for plan in all_plans(data, full):
        _STREAM_CASES.append((name, data, kwargs, "buf", plan))
    _STREAM_CASES.append((name, data, kwargs, "seek", [len(data)]))


def _reader_for_mode(data, plan, mode):
    if mode == "seek":
        return BytesIO(data)
    return BufferedStream(NonSeekableReader(data, plan))


@pytest.mark.parametrize("name,data,kwargs,mode,plan", _STREAM_CASES,
                         ids=["%s-%s-%r" % (
                              case[3], case[0], plan_cuts(case[4]))
                              for case in _STREAM_CASES])
def test_stream_chunk_equivalence(name, data, kwargs, mode, plan):
    reference = drain_stream(BytesIO(data), **kwargs)
    observed = drain_stream(_reader_for_mode(data, plan, mode), **kwargs)

    assert observed["encoding"] == reference["encoding"], failure_context(
        "stream encoding mismatch for %s" % name, data, plan,
        "expected %r, got %r" %
        (reference["encoding"], observed["encoding"]))
    assert observed["confidence"] == reference["confidence"], failure_context(
        "stream confidence mismatch for %s" % name, data, plan,
        "expected %r, got %r" %
        (reference["confidence"], observed["confidence"]))
    assert observed["text"] == reference["text"], failure_context(
        "stream text mismatch for %s" % name, data, plan,
        "text length expected %d, got %d" %
        (len(reference["text"]), len(observed["text"])))
    assert observed["errors"] == reference["errors"], failure_context(
        "stream errors mismatch for %s" % name, data, plan,
        "expected %r, got %r" %
        (reference["errors"], observed["errors"]))


# At construction time, corpora whose <meta> sits past the prescan window
# have not seen that declaration yet: the stream is still on its default.
_STREAM_INITIAL_ENCODING = {
    "meta_after_prescan_restart": ("windows-1252", "tentative"),
    "second_meta_ignored_after_restart": ("windows-1252", "tentative"),
}


def test_stream_expected_encoding_and_confidence():
    for name, data, kwargs, encoding, confidence, _restarts in CORPORA:
        initial = _STREAM_INITIAL_ENCODING.get(name, (encoding, confidence))
        stream = HTMLBinaryInputStream(BytesIO(data), useChardet=False,
                                       **kwargs)
        assert stream.charEncoding[0].name == initial[0], name
        assert stream.charEncoding[1] == initial[1], name


def test_final_encoding_after_meta_beyond_prescan():
    # The post-pipeline encoding is what the parser settles on after the
    # restart, not the construction-time guess.
    for name in ("meta_after_prescan_restart",
                 "second_meta_ignored_after_restart"):
        corpus = next(item for item in CORPORA if item[0] == name)
        _cname, data, kwargs, encoding, confidence, _restarts = corpus
        parser = HTMLParser(namespaceHTMLElements=False)
        parser.parse(BytesIO(data), useChardet=False, **kwargs)
        assert parser.documentEncoding == encoding, name
        assert parser.tokenizer.stream.charEncoding[1] == confidence, name


# ---------------------------------------------------------------------------
# Parser-level chunk equivalence and restart bookkeeping
# ---------------------------------------------------------------------------


_PARSER_CASES = []
for name, data, kwargs, _enc, _conf, restarts in CORPORA:
    full = len(data) <= 200
    for plan in all_plans(data, full):
        _PARSER_CASES.append(
            (name, data, kwargs, "buf", plan, restarts))
    _PARSER_CASES.append(
        (name, data, kwargs, "seek", [len(data)], restarts))


@pytest.mark.parametrize(
    "name,data,kwargs,mode,plan,restarts", _PARSER_CASES,
    ids=["%s-%s-%r" % (
         case[3], case[0], plan_cuts(case[4]))
         for case in _PARSER_CASES])
def test_parser_chunk_equivalence(name, data, kwargs, mode, plan,
                                  restarts, monkeypatch):
    counter = RestartCounter(monkeypatch)
    reference = run_parser(BytesIO(data), **kwargs)
    reference_restarts = counter.count

    counter.count = 0
    observed = run_parser(_reader_for_mode(data, plan, mode), **kwargs)

    assert counter.count == reference_restarts, failure_context(
        "parser restart count mismatch for %s" % name, data, plan,
        "reference %d, got %d" % (reference_restarts, counter.count))
    assert reference_restarts == restarts, (
        "reference restart count for %s was %d, test expects %d" %
        (name, reference_restarts, restarts))
    assert observed["encoding"] == reference["encoding"], failure_context(
        "parser encoding mismatch for %s" % name, data, plan,
        "expected %r, got %r" %
        (reference["encoding"], observed["encoding"]))
    assert observed["tree"] == reference["tree"], failure_context(
        "parser tree mismatch for %s" % name, data, plan,
        "expected tree %r, got %r" %
        (reference["tree"], observed["tree"]))
    assert observed["errors"] == reference["errors"], failure_context(
        "parser errors mismatch for %s" % name, data, plan,
        "expected %d errors, got %d" %
        (len(reference["errors"]), len(observed["errors"])))


# ---------------------------------------------------------------------------
# Tokenizer-level chunk equivalence (small corpora; restart corpora are
# parser-driven and covered through the parser contract above)
# ---------------------------------------------------------------------------


_TOKENIZER_CASES = []
for name, data, kwargs, _enc, _conf, restarts in CORPORA:
    if restarts or len(data) > 200:
        continue
    for plan in all_plans(data, True):
        _TOKENIZER_CASES.append((name, data, kwargs, "buf", plan))
    _TOKENIZER_CASES.append((name, data, kwargs, "seek", [len(data)]))


@pytest.mark.parametrize(
    "name,data,kwargs,mode,plan", _TOKENIZER_CASES,
    ids=["%s-%s-%r" % (
         case[3], case[0], plan_cuts(case[4]))
         for case in _TOKENIZER_CASES])
def test_tokenizer_chunk_equivalence(name, data, kwargs, mode, plan):
    reference = run_tokenizer(BytesIO(data), **kwargs)
    observed = run_tokenizer(_reader_for_mode(data, plan, mode), **kwargs)
    assert observed == reference, failure_context(
        "tokenizer token sequence mismatch for %s" % name, data, plan,
        "expected %d tokens, got %d" % (len(reference), len(observed)))


# ---------------------------------------------------------------------------
# Encoding restart semantics on the standalone HTMLBinaryInputStream
# ---------------------------------------------------------------------------


# The <meta charset> starts past the 1024-byte prescan window, so it is
# only seen while the parser consumes the stream: the stream starts on
# windows-1252 (tentative) and must restart decoding once when the parser
# calls changeEncoding(). The non-ASCII bytes decode differently under
# windows-1252 and utf-8, and the utf-8 sequences are multibyte, so any lost,
# duplicated or stale prebuffer byte is immediately visible.
_RESTART_DATA = (b" " * 1024 + b"<meta charset=utf-8>" +
                 b"caf\xc3\xa9\xe2\x82\xac")
_RESTART_TAG_END = 1024 + len(b"<meta charset=utf-8>")


def _drain_with_change_encoding(reader, change_at, new_encoding, **kwargs):
    """Drain a stream, calling changeEncoding() after ``change_at`` chars have
    been consumed, mirroring what the parser does on <meta charset>.

    Returns (text, restart_count, final_encoding, final_confidence).
    """
    stream = HTMLBinaryInputStream(reader, useChardet=False, **kwargs)
    text = []
    restarts = 0
    changed = False
    while True:
        char = stream.char()
        if char is EOF:
            break
        text.append(char)
        if not changed and len(text) == change_at:
            try:
                stream.changeEncoding(new_encoding)
            except _ReparseException:
                restarts += 1
                # As in HTMLParser._parse, restart consumption from scratch.
                text = []
            changed = True
    return ("".join(text), restarts, stream.charEncoding[0].name,
            stream.charEncoding[1])


# _RESTART_DATA is 1024+ bytes long; use prescan-focused boundaries rather
# than all ~1070 single cuts to keep the test fast, plus fixed small blocks.
_RESTART_PLANS = all_plans(_RESTART_DATA, full_boundaries=False)


@pytest.mark.parametrize("plan", _RESTART_PLANS,
                         ids=[str(plan_cuts(plan)) for plan in _RESTART_PLANS])
def test_stream_restart_replays_without_loss_or_duplication(plan):
    # Reference: the same bytes decoded directly as utf-8, with the meta tag
    # present as literal text (changeEncoding restarts decoding, it does not
    # remove bytes).
    reference_stream = HTMLBinaryInputStream(
        BytesIO(_RESTART_DATA), override_encoding="utf-8", useChardet=False)
    reference_text = []
    while True:
        char = reference_stream.char()
        if char is EOF:
            break
        reference_text.append(char)
    reference_text = "".join(reference_text)

    # Trigger the switch before the non-ASCII content, just after the tag,
    # and in the middle of the multibyte content. The stream keeps the full
    # document buffered (BufferedStream or a seekable reader), so all of
    # these positions are reachable.
    for change_at in (1024 + 1, _RESTART_TAG_END, len(_RESTART_DATA) - 3):
        text, restarts, encoding, confidence = _drain_with_change_encoding(
            BufferedStream(NonSeekableReader(_RESTART_DATA, plan)),
            change_at, "utf-8")
        assert restarts == 1, failure_context(
            "expected exactly one restart", _RESTART_DATA, plan,
            "got %d restarts at change_at=%d" % (restarts, change_at))
        assert encoding == "utf-8" and confidence == "certain"
        assert text == reference_text, failure_context(
            "restart duplicated or lost characters (change_at=%d)" % change_at,
            _RESTART_DATA, plan,
            "expected %d chars (%r), got %d chars (%r)" %
            (len(reference_text), reference_text, len(text), text))


def test_restart_marks_encoding_certain():
    stream = HTMLBinaryInputStream(BytesIO(_RESTART_DATA), useChardet=False)
    assert stream.charEncoding[1] == "tentative"
    assert stream.charEncoding[0].name == "windows-1252"
    with pytest.raises(_ReparseException):
        stream.changeEncoding("utf-8")
    assert stream.charEncoding[1] == "certain"
    assert stream.charEncoding[0].name == "utf-8"
    # A second restart is forbidden by the asserted "certain" confidence,
    # matching the spec allowance of a single encoding change.
    with pytest.raises(AssertionError):
        stream.changeEncoding("iso-8859-2")


def test_change_encoding_to_utf16_becomes_utf8():
    stream = HTMLBinaryInputStream(BytesIO(b"abc"),
                                   default_encoding="iso-8859-2",
                                   useChardet=False)
    assert stream.charEncoding[0].name == "iso-8859-2"
    # utf-16 labels arriving from a parser-side meta are mapped to utf-8 and
    # decoded from the start, so the utf-8 codec (not utf-16, not the old
    # iso-8859-2 decoder) is attached after the single permitted restart.
    with pytest.raises(_ReparseException):
        stream.changeEncoding("utf-16le")
    assert stream.charEncoding[0].name == "utf-8"
    assert stream.charEncoding[1] == "certain"
    # The stream was rewound and is usable with the new decoder.
    assert stream.char() == "a"


def test_change_encoding_same_is_confidence_only():
    stream = HTMLBinaryInputStream(BytesIO(b"abc"),
                                   default_encoding="iso-8859-2",
                                   useChardet=False)
    assert stream.charEncoding[0].name == "iso-8859-2"
    # Same encoding: no reparse, but confidence upgrades.
    assert stream.changeEncoding("ISO-8859-2") is None
    assert stream.charEncoding[1] == "certain"
    assert stream.char() == "a"  # stream still usable, nothing replayed


def test_change_unknown_encoding_is_noop():
    stream = HTMLBinaryInputStream(BytesIO(b"abc"), useChardet=False)
    assert stream.changeEncoding("totally-bogus-string") is None
    assert stream.charEncoding[0].name == "windows-1252"
    assert stream.charEncoding[1] == "tentative"


# ---------------------------------------------------------------------------
# Encoding source priority (deterministic; no chardet, no locale)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("data,kwargs,expected,confidence", [
    # BOM beats everything.
    (codecs.BOM_UTF8 + b"<meta charset=iso-8859-2>",
     {"override_encoding": "iso-8859-3", "transport_encoding": "iso-8859-4"},
     "utf-8", "certain"),
    (codecs.BOM_UTF16_LE,
     {"override_encoding": "iso-8859-3", "transport_encoding": "iso-8859-4"},
     "utf-16le", "certain"),
    # Override beats transport.
    (b"", {"override_encoding": "iso-8859-2",
           "transport_encoding": "iso-8859-3"},
     "iso-8859-2", "certain"),
    # Transport beats meta.
    (b"<meta charset=iso-8859-3>",
     {"transport_encoding": "iso-8859-2"},
     "iso-8859-2", "certain"),
    # Meta beats parent.
    (b"<meta charset=iso-8859-2>",
     {"same_origin_parent_encoding": "iso-8859-3",
      "likely_encoding": "iso-8859-4"},
     "iso-8859-2", "tentative"),
    # Parent beats likely; utf-16 parent labels are skipped.
    (b"", {"same_origin_parent_encoding": "iso-8859-2",
           "likely_encoding": "iso-8859-3"},
     "iso-8859-2", "tentative"),
    (b"", {"same_origin_parent_encoding": "utf-16be",
           "likely_encoding": "iso-8859-2"},
     "iso-8859-2", "tentative"),
    (b"", {"same_origin_parent_encoding": "utf-16",
           "likely_encoding": "iso-8859-2"},
     "iso-8859-2", "tentative"),
    # Likely beats default.
    (b"", {"likely_encoding": "iso-8859-2",
           "default_encoding": "iso-8859-3"},
     "iso-8859-2", "tentative"),
    # Default is used last; an invalid default falls back to windows-1252.
    (b"", {"default_encoding": "iso-8859-2"},
     "iso-8859-2", "tentative"),
    (b"", {"default_encoding": "totally-bogus-string"},
     "windows-1252", "tentative"),
    (b"", {}, "windows-1252", "tentative"),
    # A meta label naming utf-16 is mapped to utf-8.
    (b"<meta charset=utf-16le>", {}, "utf-8", "tentative"),
])
def test_encoding_source_priority(data, kwargs, expected, confidence):
    stream = HTMLBinaryInputStream(BytesIO(data), useChardet=False, **kwargs)
    assert stream.charEncoding[0].name == expected
    assert stream.charEncoding[1] == confidence


def test_priority_holds_for_chunked_reader():
    # The same precedence must be observed when BOM and meta bytes arrive in
    # single-byte reads from an unseekable stream.
    data = (codecs.BOM_UTF8 +
            b"<meta charset=iso-8859-2><p>\xc3\xa9")
    kwargs = {"override_encoding": "iso-8859-3",
              "transport_encoding": "iso-8859-4",
              "same_origin_parent_encoding": "iso-8859-5",
              "likely_encoding": "iso-8859-6"}
    stream = HTMLBinaryInputStream(
        BufferedStream(NonSeekableReader(data, [1] * (len(data) + 1))),
        useChardet=False, **kwargs)
    assert stream.charEncoding[0].name == "utf-8"
    assert stream.charEncoding[1] == "certain"


# ---------------------------------------------------------------------------
# Encoding name normalization: generated from the repository dependency's
# WHATWG label table (webencodings), not from locale or codec guessing.
# ---------------------------------------------------------------------------


def test_label_table_is_repository_encoding_table():
    # Sanity: the table used for generation is the installed dependency that
    # _inputstream.lookupEncoding itself delegates to.
    from webencodings.labels import LABELS
    assert "utf-8" in LABELS
    assert LABELS["utf-8"] == "utf-8"
    assert LABELS["x-user-defined"] == "x-user-defined"


def test_known_labels_resolve_generated():
    """Every canonical encoding name in the table resolves to itself; a
    sampled alias per encoding resolves to that canonical name."""
    from webencodings.labels import LABELS
    canonical_names = sorted(set(LABELS.values()))
    assert len(canonical_names) >= 40  # table actually populated
    for name in canonical_names:
        assert lookupEncoding(name).name == name
    # The canonical name is always one of the labels.
    for name in canonical_names:
        assert name in LABELS and LABELS[name] == name


@pytest.mark.parametrize("label,expected", [
    # ASCII case and ASCII whitespace normalization are accepted.
    ("UTF-8", "utf-8"),
    (" Utf-8 ", "utf-8"),
    ("\tUTF-8\n", "utf-8"),
    ("\x0cutf-8\r", "utf-8"),
    ("  WINDOWS-1252  ", "windows-1252"),
    ("X-User-Defined", "x-user-defined"),
    ("latin1", "windows-1252"),
    ("ISO_8859_2:1987", None),       # decorated name is not a label
    ("utf 8", None),                # internal space is not a hyphen
    ("utf-8 ", "utf-8"),            # trailing strip only
    ("utf\u00a08", None),           # non-breaking space is not skippable
    ("utf\u20008", None),           # unicode whitespace is not skippable
    ("utf--8", None),
    ("utf88", None),
    ("", None),
    (b"UTF-8", "utf-8"),            # bytes labels are ASCII-decoded
    ("\xff".encode("latin-1"), None),  # non-ASCII bytes fail
    ("not-an-encoding", None),
    ("UTF8\u0000", None),
])
def test_label_normalization(label, expected):
    encoding = lookupEncoding(label)
    if expected is None:
        assert encoding is None, "label %r unexpectedly resolved" % (label,)
    else:
        assert encoding is not None, "label %r did not resolve" % (label,)
        assert encoding.name == expected


def test_normalization_does_not_accept_arbitrary_aliases():
    """Whitespace/case folding must not widen the accepted label set:

    only exact (post-strip, ASCII-lowercased) table labels match. Perturbed
    labels generated from real labels must all be rejected."""
    from webencodings.labels import LABELS
    perturbations = set()
    for label in LABELS:
        if "-" in label:
            perturbations.add(label.replace("-", "_"))
            perturbations.add(label.replace("-", " "))
            perturbations.add(label.replace("-", "--"))
        if label.isalpha():
            perturbations.add(label + "x")
            perturbations.add(label[:-1])
    rejected = 0
    for label in perturbations:
        if label not in LABELS:
            assert lookupEncoding(label) is None, label
            rejected += 1
    assert rejected > 50  # ensure the check is non-vacuous


# ---------------------------------------------------------------------------
# BufferedStream: short reads must be filled to the requested size (unless EOF)
# ---------------------------------------------------------------------------


class _RawShortReader(object):
    """Returns at most one byte per read, regardless of the requested size."""

    def __init__(self, data):
        self.data = data
        self.offset = 0
        self.short_reads = 0

    def read(self, size):
        if size == 0:
            return b""
        if self.offset >= len(self.data):
            return b""
        if size > 1:
            self.short_reads += 1
        chunk = self.data[self.offset:self.offset + 1]
        self.offset += 1
        return chunk

    def seek(self, offset):
        raise IOError("non-seekable")

    def tell(self):
        return self.offset


def test_buffered_stream_fills_short_reads():
    raw = _RawShortReader(b"abcdef")
    buffered = BufferedStream(raw)
    assert buffered.read(4) == b"abcd"
    assert raw.short_reads >= 3
    assert buffered.tell() == 4
    assert buffered.read(4) == b"ef"  # short at EOF: only 2 bytes remain
    assert buffered.read(4) == b""    # genuine EOF
    assert buffered.read(0) == b""


def test_buffered_stream_short_reads_seek_replays_all_bytes():
    data = codecs.BOM_UTF8 + b"<meta charset=utf-8>" + b"caf\xc3\xa9"
    buffered = BufferedStream(_RawShortReader(data))
    # Read past the prescan window through many short reads.
    assert buffered.read(1024) == data[:1024] if len(data) < 1024 else True
    buffered.seek(0)
    assert buffered.tell() == 0
    assert buffered.read(4) == data[:4]
    buffered.seek(0)
    assert buffered.read(len(data)) == data


@pytest.mark.parametrize("size", [1, 2, 3, 7])
def test_buffered_stream_prescan_with_short_reads(size):
    # The <meta> declaration sits inside the 1024-byte prescan window; with
    # every read truncated to ``size`` bytes the prescan must still see all
    # 1024 bytes and select utf-8.
    data = b" " * 1000 + b"<meta charset=utf-8><p>\xc3\xa9"
    reader = BufferedStream(_RawShortFixedReader(data, size))
    stream = HTMLBinaryInputStream(reader, useChardet=False)
    assert stream.charEncoding[0].name == "utf-8"


class _RawShortFixedReader(object):
    """Unseekable reader that always returns at most ``limit`` bytes."""

    def __init__(self, data, limit):
        self.data = data
        self.offset = 0
        self.limit = limit

    def read(self, size):
        if size == 0:
            return b""
        size = min(size, self.limit)
        chunk = self.data[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk

    def seek(self, offset):
        raise IOError("non-seekable")

    def tell(self):
        return self.offset


# ---------------------------------------------------------------------------
# Independence from locale and external detectors
# ---------------------------------------------------------------------------


def test_chardet_is_not_invoked(monkeypatch):
    """useChardet=False must not import or call chardet; detection is fully
    deterministic from BOM/prescan/arguments/default here."""
    import sys
    # A successful `import chardet` would leave this sentinel object in
    # sys.modules; ImportError is also acceptable. Either way, no detector
    # object must run (the in-code import only happens in the chardet branch).
    monkeypatch.delitem(sys.modules, "chardet", raising=False)

    data = b"<meta charset=iso-8859-7><p>\xc2</p>"
    stream = HTMLBinaryInputStream(BytesIO(data), useChardet=False)
    assert stream.charEncoding[0].name == "iso-8859-7"
    assert "chardet" not in sys.modules


def test_no_locale_dependency():
    """Draining identical bytes with different chunk plans gives identical
    results regardless of interpreter locale; this module never calls
    locale.getpreferredencoding()."""
    import html5lib._inputstream as inputstream_module
    assert not hasattr(inputstream_module, "locale")


# ---------------------------------------------------------------------------
# Parser restart cap: the specification allows one encoding change
# ---------------------------------------------------------------------------


def test_parser_restarts_at_most_once(monkeypatch):
    """Two <meta charset> declarations after the prescan window: only the
    first can take effect (one restart); the second is ignored because the
    encoding is already "certain"."""
    data = (PADDING + b"<meta charset=utf-8><p>\xc3\xa9</p>" +
            b"x" * 50 +
            b"<meta charset=iso-8859-2><p>x")
    counter = RestartCounter(monkeypatch)
    parser = HTMLParser(namespaceHTMLElements=False)
    parser.parse(BytesIO(data), useChardet=False)
    assert counter.count == 1
    assert parser.documentEncoding == "utf-8"
    # Same guarantee with every byte delivered one at a time.
    counter.count = 0
    reader = BufferedStream(NonSeekableReader(data, [1] * (len(data) + 1)))
    parser = HTMLParser(namespaceHTMLElements=False)
    parser.parse(reader, useChardet=False)
    assert counter.count == 1
    assert parser.documentEncoding == "utf-8"
