.. _inputstream:

Input streams and encoding determination
=========================================

``HTMLInputStream`` accepts either a Unicode string or a binary byte
stream/file object. For binary input the encoding is selected from, in
decreasing order of precedence:

1. a Unicode (UTF-8/UTF-16) BOM, and

2. ``override_encoding``,

3. ``transport_encoding`` (e.g. an HTTP ``Content-Type`` header),

4. a ``<meta charset>`` / pragma declaration found while prescanning the
   first 1024 bytes (tentative),

5. ``same_origin_parent_encoding`` (UTF-16 values are skipped here),

6. ``likely_encoding``,

7. optional chardet detection (disabled by passing ``useChardet=False``),

8. ``default_encoding``, and finally the built-in ``windows-1252``
   fallback.

BOM, override and transport encodings are committed with ``certain``
confidence and cannot change; everything below them starts ``tentative``
and may be upgraded once when a ``<meta>`` tag is processed.

Chunking semantics
-------------------

A successful ``read(n)`` on the underlying binary object may return any
number of bytes from one to ``n``. Only ``b""`` signals EOF; a shorter
non-empty result is a short read and decoding simply continues. This
matters for:

* a BOM split across reads (the sniffer reads up to four bytes or EOF),

* a multibyte sequence split in the middle, and

* a ``<meta>`` declaration straddling the 1024-byte prescan window.

The observable results -- the committed Unicode token stream, detected
encoding, confidence, parse errors and number of restarts -- are
independent of where these boundaries fall. Non-seekable streams are
wrapped in ``BufferedStream``, which appends chunks rather than joining
them and can seek both inside buffered data and forward (refilling) to
positions such as the byte just past a BOM.

Restarts
--------

When a tentative encoding is replaced by a different one while
tokenizing, the raw stream is rewound and parsed again exactly once:
``HTMLParser._parse`` catches a single ``_ReparseException``. Switching
to the same codec only promotes confidence from tentative to certain and
does not restart. Buffered bytes are served from ``BufferedStream``
during the restart, so the source is never asked for bytes twice and no
previously delivered byte is lost or duplicated.

Label normalization
-------------------

Encoding labels are resolved through the WHATWG labels table shipped
with webencodings: the label is ASCII-whitespace trimmed and ASCII case
folded, then looked up in that closed table. Case/whitespace variants
of a registered label resolve (``"UTF-8"``, ``" utf8 "``), but similar
spellings that are not registered labels are rejected rather than
guessed (``"utf_8"``, ``"latin-1"``, ``"win-1252"``). UTF-32 labels are
not part of the current standard, so a UTF-32 BOM yields no codec.

Complexity
----------

* Prescanning is a single linear scan of at most 1024 bytes: ``O(n)`` in
  the prescan window.
* ``BufferedStream`` keeps chunks in a list and never joins them, making
  repeated appends ``O(1)`` amortized and a full parse ``O(n)`` in the
  number of bytes; seeking walks chunk lengths in ``O(k)`` for ``k``
  buffered chunks.
* An encoding restart parses the buffered input a second time, so a
  document with a late ``<meta charset>`` costs at most ``2n`` decode
  work and never more than one restart.

Compatibility notes
-------------------

* Public constructor parameters, the ``(codec, confidence)`` shape of
  ``charEncoding`` and the default ``windows-1252`` fallback are
  unchanged.
* ``BufferedStream.seek`` previously asserted when asked to move past
  the bytes buffered so far; it now refills from the underlying stream,
  and raises ``IOError`` only when the target is genuinely beyond EOF.
* webencodings implements ``x-user-defined`` (and ``replacement``) with
  charmap codecs whose generated ``StreamReader`` resolves ``decode`` to
  the stateless ``Codec.decode`` via method-resolution order. On
  Python 3 this returned raw bytes. html5lib now reads those codecs
  through an incremental-decoder-backed reader, restoring the specified
  U+F780..U+F7FF mapping without changing the public API.
