The moving parts
================

html5lib consists of a number of components, which are responsible for
handling its features.

Parsing uses a *tree builder* to generate a *tree*, the in-memory representation of the document.
Several tree representations are supported, as are translations to other formats via *tree adapters*.
The tree may be translated to a token stream with a *tree walker*, from which :class:`~html5lib.serializer.HTMLSerializer` produces a stream of bytes.
The token stream may also be transformed by use of *filters* to accomplish tasks like sanitization.

Tree builders
-------------

The parser reads HTML by tokenizing the content and building a tree that
the user can later access. html5lib can build three types of trees:

* ``etree`` - this is the default; builds a tree based on
  :mod:`xml.etree.ElementTree`, which can be found in the standard library.
  Whenever possible, the accelerated ``ElementTree`` implementation (i.e.
  ``xml.etree.cElementTree`` on Python 2.x) is used.

* ``dom`` - builds a tree based on :mod:`xml.dom.minidom`.

* ``lxml`` - uses the :mod:`lxml.etree` implementation of the ``ElementTree``
  API.  The performance gains are relatively small compared to using the
  accelerated ``ElementTree`` module.

You can specify the builder by name when using the shorthand API:

.. code-block:: python

  import html5lib
  with open("mydocument.html", "rb") as f:
      lxml_etree_document = html5lib.parse(f, treebuilder="lxml")

To get a builder class by name, use the :func:`~html5lib.treebuilders.getTreeBuilder` function.

When instantiating a :class:`~html5lib.html5parser.HTMLParser` object, you must pass a tree builder class via the ``tree`` keyword attribute:

.. code-block:: python

  import html5lib
  TreeBuilder = html5lib.getTreeBuilder("dom")
  parser = html5lib.HTMLParser(tree=TreeBuilder)
  minidom_document = parser.parse("<p>Hello World!")

The implementation of builders can be found in `html5lib/treebuilders/
<https://github.com/html5lib/html5lib-python/tree/master/html5lib/treebuilders>`_.


Tree walkers
------------

In addition to manipulating a tree directly, you can use a tree walker to generate a streaming view of it.
html5lib provides walkers for ``etree``, ``dom``, and ``lxml`` trees, as well as ``genshi`` `markup streams <https://genshi.edgewall.org/wiki/Documentation/streams.html>`_.

The implementation of walkers can be found in `html5lib/treewalkers/
<https://github.com/html5lib/html5lib-python/tree/master/html5lib/treewalkers>`_.

html5lib provides :class:`~html5lib.serializer.HTMLSerializer` for generating a stream of bytes from a token stream, and several filters which manipulate the stream.

HTMLSerializer
~~~~~~~~~~~~~~

The serializer lets you write HTML back as a stream of bytes.

.. code-block:: pycon

  >>> import html5lib
  >>> element = html5lib.parse('<p xml:lang="pl">Witam wszystkich')
  >>> walker = html5lib.getTreeWalker("etree")
  >>> stream = walker(element)
  >>> s = html5lib.serializer.HTMLSerializer()
  >>> output = s.serialize(stream)
  >>> for item in output:
  ...   print("%r" % item)
  '<p'
  ' '
  'xml:lang'
  '='
  'pl'
  '>'
  'Witam wszystkich'

You can customize the serializer behaviour in a variety of ways. Consult
the :class:`~html5lib.serializer.HTMLSerializer` documentation.


Filters
~~~~~~~

html5lib provides several filters:

* :class:`alphabeticalattributes.Filter
  <html5lib.filters.alphabeticalattributes.Filter>` sorts attributes on
  tags to be in alphabetical order

* :class:`inject_meta_charset.Filter
  <html5lib.filters.inject_meta_charset.Filter>` sets a user-specified
  encoding in the correct ``<meta>`` tag in the ``<head>`` section of
  the document

* :class:`lint.Filter <html5lib.filters.lint.Filter>` raises
  :exc:`AssertionError` exceptions on invalid tag and attribute names, invalid
  PCDATA, etc.

* :class:`optionaltags.Filter <html5lib.filters.optionaltags.Filter>`
  removes tags from the token stream which are not necessary to produce valid
  HTML

* :class:`sanitizer.Filter <html5lib.filters.sanitizer.Filter>` removes
  unsafe markup and CSS. Elements that are known to be safe are passed
  through and the rest is converted to visible text. The default
  configuration of the sanitizer follows the `WHATWG Sanitization Rules
  <http://wiki.whatwg.org/wiki/Sanitization_rules>`_.

* :class:`whitespace.Filter <html5lib.filters.whitespace.Filter>`
  collapses all whitespace characters to single spaces unless they're in
  ``<pre/>`` or ``<textarea/>`` tags.

To use a filter, simply wrap it around a token stream:

.. code-block:: python

  >>> import html5lib
  >>> from html5lib.filters import sanitizer
  >>> dom = html5lib.parse("<p><script>alert('Boo!')", treebuilder="dom")
  >>> walker = html5lib.getTreeWalker("dom")
  >>> stream = walker(dom)
  >>> clean_stream = sanitizer.Filter(stream)


Tree adapters
-------------

Tree adapters can be used to translate between tree formats.
Two adapters are provided by html5lib:

* :func:`html5lib.treeadapters.genshi.to_genshi()` generates a `Genshi markup stream <https://genshi.edgewall.org/wiki/Documentation/streams.html>`_.
* :func:`html5lib.treeadapters.sax.to_sax()` calls a SAX handler based on the tree.

Encoding discovery
------------------

Parsed trees are always Unicode. However a large variety of input
encodings are supported. The encoding of the document is determined from
several sources in a fixed precedence order (BOM, override, transport,
``<meta>`` prescan, parent, likely encoding, optional chardet, and
default), and the parser restarts decoding once if a tentative encoding
is superseded by a ``<meta charset>`` seen after the prescan window. The
full precedence, chunking semantics, complexity and compatibility notes
are in the ``Input streams and encodings`` section below.

Input streams and encodings
---------------------------

The parser accepts both text (``str``) and byte input. Byte input goes through
:class:`~html5lib._inputstream.HTMLBinaryInputStream`, which selects an
encoding using the precedence defined by the HTML specification:

#. a BOM at the start of the document (always authoritative);
#. the ``override_encoding`` argument;
#. the ``transport_encoding`` argument (for example an HTTP
   ``Content-Type`` charset);
#. an encoding declared by a ``<meta charset>`` (or
   ``<meta http-equiv="content-type">``) found while prescanning the first
   1024 bytes;
#. ``same_origin_parent_encoding`` (UTF-16 labels are skipped at this step);
#. ``likely_encoding``;
#. optional chardet detection (disabled by passing ``useChardet=False``);
#. ``default_encoding``, which itself defaults to ``windows-1252``.

Encodings declared by BOM, override, or transport are *certain*; all other
sources are *tentative*. A tentative stream changes its encoding at most
once: when the parser reaches a ``<meta charset>`` during tokenization it
rewinds to the start of the byte stream, rebuilds the decoder, marks the
encoding certain, and re-parses from the beginning. A later ``<meta>``
cannot trigger another change. A declaration naming UTF-16 when a change
occurs is honoured as UTF-8.

Encoding *labels* are resolved through the WHATWG label table provided by
:mod:`webencodings`: only exact table labels match after ASCII case folding
and stripping of ASCII whitespace (tab, line feed, form feed, carriage
return, space). Other Unicode whitespace is not stripped, and underscores,
internal spaces, or extra punctuation do not make a label valid.

Chunking semantics
~~~~~~~~~~~~~~~~~~

The decoded result is independent of how the byte source splits its reads.
The parser distinguishes two stream shapes:

* **Seekable streams** (objects whose ``seek()``/``tell()`` round-trip) may
  be rewound directly after an encoding change.
* **Non-seekable streams** (such as raw sockets) are wrapped in
  :class:`~html5lib._inputstream.BufferedStream`, which retains every byte it
  has read so the stream can still be rewound. Both shapes honour the normal
  file-object contract: ``read(n)`` may be a *short read* returning fewer than
  ``n`` bytes while more data remains, but only ``b""`` means EOF; ``read(0)``
  is a non-consuming probe. ``BufferedStream`` keeps issuing reads until the
  requested amount is available or EOF (``b""``) is reached.

Invalid byte sequences for the selected encoding are replaced with U+FFFD
using Python's incremental codec, so a multibyte sequence split across two
reads decodes the same as one delivered in a single read.

Complexity
~~~~~~~~~~

Prescanning inspects at most the first 1024 bytes (constant work for a fixed
window), and BOM detection reads at most 4 bytes. ``BufferedStream`` stores
every byte read (O(n) memory for n bytes) so a restart can replay it;
re-reading already buffered data is an O(1) buffer lookup plus the bytes
copied. The single permitted encoding restart re-tokenizes the prefix once,
so the total tokenization work is proportional to the document length plus
that one prefix.

Compatibility
~~~~~~~~~~~~~

No public argument names or defaults change and no network, locale, clock,
or file-system state is consulted by encoding selection. Detection stays
local: chardet is only attempted when ``useChardet`` is true (the default)
and installed; passing ``useChardet=False`` gives fully deterministic
selection. The test-suite contract for chunk independence and encoding
precedence lives in ``html5lib/tests/test_stream_chunks.py``.
