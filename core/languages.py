"""
KATA. (c)

Author: Gregoire Dehame
Created: Jul 22, 2026
Module: ui.code_editor.core.languages
Execute: from kata.ui.code_editor.core import languages

Syntax colouring for the languages that do not need a hand-written parser (C/C++, json, PowerShell,
markdown, xml, yaml/ini). Python and MEL keep their dedicated highlighters in `editor`; `attach()` picks
whichever suits a given file. Colours come from editor's Dark+ palette so every tab reads the same.
"""

import os
import re

from . import qt


class RuleHighlighter(qt.QSyntaxHighlighter):
    """Regex-driven highlighter.

    `rules` is a list of (compiled pattern, char format). When a pattern defines group 1 only that group
    is coloured, which lets a rule match on context - a json key only when followed by ':' - while
    painting just the interesting span. `block` optionally carries (opener, closer, format) for
    multi-line comments, tracked across blocks through the highlighter's block state.
    """

    def __init__(self, document, rules, block=None) -> None:
        super().__init__(document)
        self._rules = rules
        self._block = block

    def highlightBlock(self, text) -> None:
        for pattern, char_format in self._rules:
            for match in pattern.finditer(text):
                group = 1 if match.lastindex else 0
                start, end = match.span(group)
                if end > start:
                    self.setFormat(start, end - start, char_format)
        self.setCurrentBlockState(0)
        if self._block:
            self._paint_block_comment(text)

    def _paint_block_comment(self, text) -> None:
        """Colour /* ... */ style spans, keeping state so they survive across lines."""
        opener, closer, char_format = self._block
        if self.previousBlockState() == 1:
            start = 0
        else:
            found = opener.search(text)
            start = found.start() if found else -1

        while start >= 0:
            closing = closer.search(text, start + 1)
            if closing:
                length = closing.end() - start
                self.setCurrentBlockState(0)
            else:
                length = len(text) - start
                self.setCurrentBlockState(1)
            self.setFormat(start, length, char_format)
            following = opener.search(text, start + max(1, length))
            start = following.start() if following else -1


# Dark+ token colours, shared with editor.PythonHighlighter
_PALETTE = {
    "keyword":  [86, 156, 214],   "control":  [197, 134, 192],
    "type":     [78, 201, 176],   "string":   [206, 145, 120],
    "comment":  [106, 153, 85],   "number":   [181, 206, 168],
    "function": [220, 220, 170],  "property": [156, 220, 254],
    "constant": [86, 156, 214],   "text":     [212, 212, 212],
}


def _syntax(role: str, style: str = "") -> "qt.QTextCharFormat":
    """Char format for a token role, built with editor's own format helper."""
    from . import editor                      # deferred: editor must not depend on this module
    return editor.get_syntax_format(_PALETTE.get(role, _PALETTE["text"]), role, style)


def _words(names) -> str:
    """Regex matching any of `names` as a whole word."""
    return r"\b(?:%s)\b" % "|".join(names)


_CPP_KEYWORDS = ("alignas alignof asm auto break case catch class concept const consteval constexpr "
                 "continue decltype default delete do else enum explicit export extern for friend goto "
                 "if inline mutable namespace new noexcept operator private protected public register "
                 "reinterpret_cast requires return sizeof static static_assert static_cast struct "
                 "switch template this thread_local throw try typedef typeid typename union using "
                 "virtual volatile while").split()
_CPP_TYPES = ("bool char char8_t char16_t char32_t double float int long short signed unsigned void "
              "wchar_t size_t nullptr true false").split()
_PS_KEYWORDS = ("begin break catch class continue data define do dynamicparam else elseif end enum "
                "exit filter finally for foreach from function hidden if in param process return "
                "static switch throw trap try until using var while").split()


def rules_for(extension: str):
    """Return (rules, block_comment) for a file extension, or (None, None) when unsupported."""
    extension = (extension or "").lower()

    if extension in (".cpp", ".cxx", ".cc", ".c", ".h", ".hpp", ".hxx", ".inl"):
        return ([
            (re.compile(r'"(?:[^"\\]|\\.)*"'),              _syntax("string")),
            (re.compile(r"'(?:[^'\\]|\\.)*'"),              _syntax("string")),
            (re.compile(r"^\s*(#\s*\w+)"),                  _syntax("control")),
            (re.compile(_words(_CPP_KEYWORDS)),             _syntax("keyword")),
            (re.compile(_words(_CPP_TYPES)),                _syntax("type")),
            (re.compile(r"\b\d+(?:\.\d+)?[fFuUlL]*\b"),     _syntax("number")),
            (re.compile(r"\b(\w+)\s*\("),                   _syntax("function")),
            (re.compile(r"//[^\n]*"),                       _syntax("comment")),
        ], (re.compile(r"/\*"), re.compile(r"\*/"), _syntax("comment")))

    if extension in (".json", ".jsonc"):
        return ([
            (re.compile(r'("(?:[^"\\]|\\.)*")\s*:'),        _syntax("property")),
            (re.compile(r'"(?:[^"\\]|\\.)*"'),              _syntax("string")),
            (re.compile(r"\b-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b"), _syntax("number")),
            (re.compile(_words(("true", "false", "null"))), _syntax("constant")),
            (re.compile(r"//[^\n]*"),                       _syntax("comment")),
        ], None)

    if extension in (".ps1", ".psm1", ".psd1"):
        return ([
            (re.compile(r'"(?:[^"`]|`.)*"'),                _syntax("string")),
            (re.compile(r"'[^']*'"),                        _syntax("string")),
            (re.compile(r"\$[A-Za-z_][\w:]*"),              _syntax("property")),
            (re.compile(r"(?i)" + _words(_PS_KEYWORDS)),    _syntax("keyword")),
            (re.compile(r"(?:^|\s)(-\w+)"),                 _syntax("type")),
            (re.compile(r"\b\d+(?:\.\d+)?\b"),              _syntax("number")),
            (re.compile(r"#[^\n]*"),                        _syntax("comment")),
        ], (re.compile(r"<#"), re.compile(r"#>"), _syntax("comment")))

    if extension in (".md", ".markdown", ".mdown"):
        return ([
            (re.compile(r"^#{1,6}\s.*$"),                   _syntax("keyword", "bold")),
            (re.compile(r"\*\*[^*]+\*\*"),                  _syntax("text", "bold")),
            (re.compile(r"(?<!\*)\*[^*\n]+\*(?!\*)"),       _syntax("text", "italic")),
            (re.compile(r"`[^`\n]+`"),                      _syntax("string")),
            (re.compile(r"\[[^\]]*\]\([^)]*\)"),            _syntax("function")),
            (re.compile(r"^\s*(?:[-*+]|\d+\.)\s"),          _syntax("control")),
            (re.compile(r"^\s*>.*$"),                       _syntax("comment")),
        ], (re.compile(r"^```"), re.compile(r"^```"), _syntax("string")))

    if extension in (".xml", ".ui", ".vcxproj", ".filters", ".props", ".sln", ".html", ".htm"):
        return ([
            (re.compile(r"</?([\w:.-]+)"),                  _syntax("keyword")),
            (re.compile(r"([\w:.-]+)\s*="),                 _syntax("property")),
            (re.compile(r'"(?:[^"\\]|\\.)*"'),              _syntax("string")),
        ], (re.compile(r"<!--"), re.compile(r"-->"), _syntax("comment")))

    if extension in (".yaml", ".yml", ".ini", ".cfg", ".toml"):
        return ([
            (re.compile(r"^\s*([\w.-]+)\s*[:=]"),           _syntax("property")),
            (re.compile(r'"(?:[^"\\]|\\.)*"'),              _syntax("string")),
            (re.compile(r"'[^']*'"),                        _syntax("string")),
            (re.compile(r"\b\d+(?:\.\d+)?\b"),              _syntax("number")),
            (re.compile(r"(?<!\S)#[^\n]*"),                 _syntax("comment")),
            (re.compile(r"^\s*\[[^\]]+\]"),                 _syntax("keyword")),
        ], None)

    return None, None


def attach(document, path: str):
    """Attach the highlighter matching `path` to `document`. Returns it, or None when unsupported.

    Keeping a reference to the returned object matters: a highlighter that gets garbage collected stops
    colouring its document.
    """
    from . import editor                      # deferred, see _syntax
    extension = os.path.splitext(path or "")[1].lower()
    if extension in (".py", ".pyw", ".pyi"):
        return editor.PythonHighlighter(document)
    if extension == ".mel":
        return editor.MelHighlighter(document)
    rules, block = rules_for(extension)
    return RuleHighlighter(document, rules, block) if rules else None


def is_image(path: str) -> bool:
    """True when `path` is an image the editor can preview instead of opening as text."""
    return os.path.splitext(path or "")[1].lower() in (
        ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".ico", ".tif", ".tiff", ".tga")
