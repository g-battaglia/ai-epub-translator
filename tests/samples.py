"""A chapter page with the awkward markup real EPUBs are full of.

The round-trip and self-diff checks used to run on two books of the maintainer's
own library — copyrighted files nobody else could have, so the tests skipped
everywhere but on one machine. This sample carries the same difficulties on
purpose: an XML declaration and a doctype, a namespaced root, a ``<style>``
block, an undefined entity (``&nbsp;`` is legal in XHTML but not in XML), a
pagebreak marker, footnote anchors, an empty self-closing anchor, single-quoted
attributes, a ``<pre>`` whose whitespace must survive, a void tag written both
ways, and a comment.
"""

CHAPTER = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<!DOCTYPE html>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"'
    ' lang="en" xml:lang="en">\n'
    '<head><title>Chapter 2</title>\n'
    '<style type="text/css">p.first { text-indent: 0 }</style>\n'
    '<link rel="stylesheet" href="../Styles/style.css"/>\n'
    '</head>\n'
    '<body>\n'
    '<section epub:type="chapter">\n'
    '<h2 id="ch02"><span class="small">Chapter</span> 2</h2>\n'
    '<span epub:type="pagebreak" id="page_37" title="37"/>\n'
    '<p class="first">The compiler&#8217;s job is to&#160;translate&nbsp;&mdash; and to\n'
    'complain.<a id="ch02fn1-back"/><a href="notes.xhtml#ch02fn1" '
    "epub:type='noteref'>1</a></p>\n"
    '<p>A pointer <em>declares</em> what it points <em>to</em>: <code>int&#160;*p</code>,\n'
    'never <code>int&#160;* p</code>.<br/>Read it right&#8211;to&#8211;left.<br />\n'
    'See <a href="ch04.xhtml#listing4-1">Listing&#160;4-1</a>.</p>\n'
    '<!-- the listing is set as preformatted text on purpose -->\n'
    '<pre class="code">  int main(void) {\n'
    '      return 0;   /* 5 &lt; 6 &amp;&amp; 6 &gt; 5 */\n'
    '  }</pre>\n'
    '<figure><img src="../Images/fig2-1.png" alt="A stack frame"/>\n'
    '<figcaption>Figure 2-1: a stack frame</figcaption></figure>\n'
    '<p class="last">&#8220;Everything else&#8221; is <strong><em>not</em></strong> a plan.</p>\n'
    '</section>\n'
    '</body>\n'
    '</html>\n'
)
