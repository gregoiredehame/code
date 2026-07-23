"""
KATA. (c)

Author: Gregoire Dehame
Created: Jul 22, 2026
Module: ui.code_editor.core.mel2py
Execute: from kata.ui.code_editor.core import mel2py

MEL to Python translation with no third-party dependency - pymel's mel2py is gone from Maya 2026, and
it was the only reason this package needed pymel at all.

The hard part of translating a MEL command is not the syntax, it is knowing how many arguments a flag
takes. `select -r pSphere1` and `polySphere -r 1` share the flag name, but in the first `-r` (replace)
takes none and pSphere1 is a positional argument, while in the second `-r` (radius) takes one. Nothing
in the text says which. Maya knows, and says so through `cmds.help(command)`, so that is where the
arity table comes from - parsed once per command and cached.

Outside Maya (or for a command help cannot describe) the translator falls back to a greedy reading:
a flag swallows the literals that follow it until the next flag. That is right for the multi-argument
case (`-ax 0 1 0`) and wrong for the `select -r pSphere1` shape, which is why the help table is
preferred whenever it can be had.

Covered: command calls with flags and positional arguments, variable declarations and assignments,
backtick capture, if / else if / else, while, both for shapes, comments, and the MEL operators that
have a direct Python spelling. Anything it cannot read is passed through as a commented MEL line
rather than guessed at - a wrong translation is worse than an honest one you can finish by hand.
"""

import re

# ------------------------------------------------------------------------------------- flag arity

_ARITY = {}                          # command -> {flag name without dash: number of arguments}
_LONG = {}                           # command -> {short flag: its long spelling}

# a line of `cmds.help` output describing one flag, e.g. " -ax -axis   Length Length Length"
_HELP_FLAG = re.compile(r"^\s*(-[A-Za-z]\w*)(?:\s+(-[A-Za-z]\w*))?\s*(.*?)\s*$")
_HELP_TYPE = re.compile(r"^[A-Za-z][\w|.\[\]]*$")


def _read_help(command:str) -> None:
    """Parse `cmds.help(command)` once, filling both the arity and the short -> long tables.

    The same rows carry both answers, so they are read together: asking maya twice for one block of
    text would double the cost of the only slow step in the translator.
    """
    arity, longer = {}, {}
    try:
        import maya.cmds as cmds
        text = cmds.help(command) or ""
    except Exception:
        text = ""

    for line in text.splitlines():
        if not line.strip().startswith("-"):
            continue                                  # synopsis and prose, not a flag row
        match = _HELP_FLAG.match(line)
        if not match:
            continue
        short, long_name, rest = match.groups()
        count = len([word for word in rest.split() if _HELP_TYPE.match(word)])
        for name in (short, long_name):
            if name:
                arity[name.lstrip("-")] = count
        if short and long_name:
            longer[short.lstrip("-")] = long_name.lstrip("-")

    _ARITY[command], _LONG[command] = arity, longer


def flag_arity(command:str) -> dict:
    """How many arguments each flag of `command` takes, read from Maya's own help. {} when unknown."""
    if command not in _ARITY:
        _read_help(command)
    return _ARITY[command]


def long_flag(command:str, flag:str) -> str:
    """The readable spelling of `flag`: `radius` for `r`, `subdivisionsX` for `sx`.

    Falls back to the flag as written when help has nothing to say - a name we cannot expand is
    better left short than guessed at.
    """
    if command not in _LONG:
        _read_help(command)
    return _LONG[command].get(flag, flag)


# ----------------------------------------------------------------------------------- tokenisation

_TOKEN = re.compile(r"""
      (?P<comment> //[^\n]* | /\*.*?\*/ )
    | (?P<string>  "(?:\\.|[^"\\])*" )
    | (?P<var>     \$[A-Za-z_]\w* )
    | (?P<number>  \d+\.\d*(?:[eE][-+]?\d+)? | \.\d+ | \d+ )
    | (?P<name>    [A-Za-z_][\w:]* )
    | (?P<op>      \+\+ | -- | == | != | <= | >= | \&\& | \|\| | [-+*/%<>=!;,(){}\[\]`.?:] )
    | (?P<space>   \s+ )
""", re.X | re.S)

# MEL spells booleans several ways; all of them are Python bools once translated
_BOOLEANS = {"true": "True", "false": "False", "yes": "True", "no": "False",
             "on": "True", "off": "False"}

_OPERATORS = {"&&": "and", "||": "or"}

# `string $s;` leaves an empty string behind, not an undefined name
_DEFAULTS = {"string": '""', "int": "0", "float": "0.0", "vector": "(0, 0, 0)", "matrix": "[]"}

# MEL builtins that are python builtins too: they must NOT be prefixed with the cmds module
_BUILTINS = {"print": "print", "size": "len", "int": "int", "float": "float", "string": "str",
             "abs": "abs", "min": "min", "max": "max", "sort": "sorted"}

# statements with no honest one-line translation. Guessing at these produces code that LOOKS right,
# which is worse than a comment you can finish yourself.
_UNSUPPORTED = {"proc", "switch", "case", "default", "do", "alias", "catch", "catchQuiet"}

# MEL-only calls that stay MEL, evaluated through maya.mel
_VIA_MEL = {"source", "eval"}


class Token(object):
    """One lexical unit, remembering whether whitespace preceded it.

    That flag is what separates a flag from a subtraction: ` -x` starts a flag, `a-x` does not, and
    the two are otherwise identical strings.
    """

    __slots__ = ("kind", "text", "spaced")

    def __init__(self, kind, text, spaced):
        self.kind, self.text, self.spaced = kind, text, spaced

    def __repr__(self):
        return "<%s %r>" % (self.kind, self.text)


def tokenize(mel:str) -> list:
    """Split MEL source into tokens, dropping whitespace but recording where it was."""
    tokens, position, spaced = [], 0, False
    while position < len(mel):
        match = _TOKEN.match(mel, position)
        if not match:
            tokens.append(Token("other", mel[position], spaced))
            position, spaced = position + 1, False
            continue
        position = match.end()
        kind = match.lastgroup
        if kind == "space":
            spaced = True
            continue
        tokens.append(Token(kind, match.group(), spaced))
        spaced = False
    return tokens


def _is_flag(tokens:list, index:int) -> bool:
    """True when tokens[index] starts a command flag rather than a minus sign."""
    token = tokens[index]
    if token.kind != "op" or token.text != "-":
        return False
    if index + 1 >= len(tokens):
        return False
    following = tokens[index + 1]
    return (not following.spaced) and following.kind == "name" and token.spaced


# ------------------------------------------------------------------------------------ expressions

def _value(token:Token) -> str:
    """One MEL literal as its Python spelling."""
    if token.kind == "var":
        return token.text[1:]
    if token.kind == "string":
        return token.text
    if token.kind == "name":
        return _BOOLEANS.get(token.text.lower(), token.text)
    return token.text


def translate_expression(tokens:list) -> str:
    """Render an expression's tokens as Python, rewriting the operators that differ."""
    out = []
    for token in tokens:
        if token.kind == "op" and token.text in _OPERATORS:
            out.append(" %s " % _OPERATORS[token.text])
        elif token.kind == "op" and token.text == "!":
            out.append("not ")
        elif token.kind == "op" and token.text in ("{", "}"):
            out.append("[" if token.text == "{" else "]")      # MEL array literal
        elif token.kind == "op":
            spacing = token.text in ("=", "==", "!=", "<", ">", "<=", ">=", "+", "-", "*", "/", "%")
            out.append(" %s " % token.text if spacing else token.text)
        else:
            out.append(_value(token))
    return re.sub(r"\s+", " ", "".join(out)).strip()


# --------------------------------------------------------------------------------------- commands

def _read_value(tokens:list, index:int) -> tuple:
    """One argument starting at `index`, as (python text, next index).

    A parenthesised group counts as a single argument: `setAttr ($name + ".tx") 5` passes ONE
    concatenated string, and reading its tokens one by one would turn it into several.
    """
    token = tokens[index]
    if token.kind == "op" and token.text == "(":
        depth, end = 0, index
        while end < len(tokens):
            if tokens[end].kind == "op" and tokens[end].text == "(":
                depth += 1
            elif tokens[end].kind == "op" and tokens[end].text == ")":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        return translate_expression(tokens[index + 1:end]), end + 1
    if token.kind == "op" and token.text == "`":
        end = next((i for i in range(index + 1, len(tokens))
                    if tokens[i].kind == "op" and tokens[i].text == "`"), len(tokens))
        return _call(tokens[index + 1:end], "cmds"), end + 1
    return _value(token), index + 1


def _starts_value(tokens:list, index:int) -> bool:
    """True when a value (and not a flag or the end of the statement) begins at `index`."""
    if index >= len(tokens) or _is_flag(tokens, index):
        return False
    token = tokens[index]
    return token.kind in ("string", "number", "var", "name") or \
        (token.kind == "op" and token.text in ("(", "`"))


def _split_arguments(tokens:list, long_names:bool=False) -> tuple:
    """Read a command's tokens into (positional, keyword) using the arity Maya reports."""
    command = tokens[0].text
    arity = flag_arity(command)
    # in query mode MEL flags carry no argument at all, whatever their arity says
    query = any(_is_flag(tokens, i) and tokens[i + 1].text in ("q", "query")
                for i in range(len(tokens) - 1))
    positional, keyword, index = [], [], 1

    while index < len(tokens):
        if _is_flag(tokens, index):
            name = tokens[index + 1].text
            if long_names:
                name = long_flag(command, name)
            index += 2
            count = 0 if query else arity.get(name)
            values = []
            if count is None:
                # no table: take every value up to the next flag. Right for "-ax 0 1 0", wrong for
                # a no-argument flag followed by a positional - see the module docstring.
                while _starts_value(tokens, index):
                    value, index = _read_value(tokens, index)
                    values.append(value)
            else:
                for _ in range(count):
                    if not _starts_value(tokens, index):
                        break
                    value, index = _read_value(tokens, index)
                    values.append(value)

            if not values:
                keyword.append("%s=True" % name)                # a flag with no argument is a switch
            elif len(values) == 1:
                keyword.append("%s=%s" % (name, values[0]))
            else:
                keyword.append("%s=(%s)" % (name, ", ".join(values)))
        elif _starts_value(tokens, index):
            value, index = _read_value(tokens, index)
            positional.append(value)
        else:
            index += 1                                          # stray punctuation: skip it

    return positional, keyword


def translate_command(mel:str, module:str="cmds", long_names:bool=False) -> str:
    """Translate a single MEL command into its `cmds` call. Returns "" for an empty statement."""
    tokens = [t for t in tokenize(mel) if t.kind != "comment"]
    while tokens and tokens[-1].kind == "op" and tokens[-1].text == ";":
        tokens.pop()
    return _statement(tokens, module, long_names) if tokens else ""


def _call(tokens:list, module:str, long_names:bool=False) -> str:
    """A command invocation: name, flags, positional arguments."""
    positional, keyword = _split_arguments(tokens, long_names)
    return "%s.%s(%s)" % (module, tokens[0].text, ", ".join(positional + keyword))


def _statement(tokens:list, module:str, long_names:bool=False) -> str:
    """One MEL statement (no trailing semicolon) as a line of Python, or "" when it is not one."""
    if tokens[0].kind == "name" and tokens[0].text in _UNSUPPORTED:
        return ""                                    # the caller comments it out verbatim

    # `global string $g;` declares at module level, which a plain assignment already does
    if tokens[0].kind == "name" and tokens[0].text == "global":
        tokens = tokens[1:]
        if not tokens:
            return ""

    if tokens[0].kind == "name" and tokens[0].text in _VIA_MEL:
        inner = _join(tokens).rstrip(";").strip()
        return "mel.eval(%r)" % inner                # still MEL: run it as MEL

    # `$x = ...` or `string $x = ...`
    declared = tokens[0].kind == "name" and tokens[0].text in _DEFAULTS
    body = tokens[1:] if declared else tokens

    if body and body[0].kind == "var":
        name = body[0].text[1:]
        rest = body[1:]
        # `string $a[]` and `string $a[] = {...}`: an array declaration, with or without a value
        if len(rest) >= 2 and rest[0].kind == "op" and rest[0].text == "[" \
                and rest[1].kind == "op" and rest[1].text == "]":
            rest = rest[2:]
            if not rest:
                return "%s = []" % name
            if rest[0].kind == "op" and rest[0].text == "=":
                return "%s = %s" % (name, _expression_or_call(rest[1:], module, long_names) or "[]")
        if not rest:                                             # `string $s;`
            return "%s = %s" % (name, _DEFAULTS.get(tokens[0].text, "None"))
        if rest[0].kind == "op" and rest[0].text in ("=", "+=", "-="):
            return "%s %s %s" % (name, rest[0].text, _expression_or_call(rest[1:], module, long_names))
        if rest[0].kind == "op" and rest[0].text in ("++", "--"):
            return "%s %s= 1" % (name, rest[0].text[0])

    if tokens[0].kind == "name":
        if tokens[0].text in _BUILTINS:                          # print / size / ... stay python
            positional, keyword = _split_arguments(tokens, long_names)
            return "%s(%s)" % (_BUILTINS[tokens[0].text], ", ".join(positional + keyword))
        return _call(tokens, module, long_names)
    return translate_expression(tokens)


def _expression_or_call(tokens:list, module:str, long_names:bool=False) -> str:
    """The right-hand side of an assignment: a backticked command, a call, or a plain expression."""
    if not tokens:
        return "None"
    if tokens[0].kind == "op" and tokens[0].text == "`":
        closing = next((i for i, t in enumerate(tokens)
                        if i and t.kind == "op" and t.text == "`"), len(tokens))
        return _call(tokens[1:closing], module, long_names)
    if tokens[0].kind == "name" and tokens[0].text not in _BOOLEANS and len(tokens) > 1 \
            and tokens[1].kind in ("string", "number", "var") or _looks_like_call(tokens):
        return _call(tokens, module, long_names)
    return translate_expression(tokens)


def _looks_like_call(tokens:list) -> bool:
    """A bare `command -flag value` on the right of an assignment is still a call."""
    return (tokens and tokens[0].kind == "name"
            and any(_is_flag(tokens, i) for i in range(1, len(tokens))))


# ---------------------------------------------------------------------------------------- scripts

_CONTROL = re.compile(r"^\s*(if|else\s+if|else|while|for)\b")


def _parses(python:str) -> bool:
    """True when `python` is a syntactically valid statement."""
    try:
        import ast
        ast.parse(python)
        return True
    except SyntaxError:
        return False


def translate(mel:str, module:str="cmds", long_names:bool=False) -> str:
    """Translate a MEL script (or a single line) into Python.

    Statements that cannot be read are emitted as a commented MEL line: a translation you can see is
    incomplete beats one that looks finished and is wrong.
    """
    lines, indent = [], 0
    for raw in _statements(mel):
        text = raw.strip()
        if not text:
            continue

        if text.startswith("//"):
            lines.append("    " * indent + "#" + text[2:])
            continue
        if text.startswith("/*"):
            for piece in text.strip("/*").strip().splitlines():
                lines.append("    " * indent + "# " + piece.strip())
            continue

        if text == "}":
            indent = max(0, indent - 1)
            continue

        opens = text.endswith("{")
        head = text[:-1].strip() if opens else text
        if head.startswith("}"):                       # "} else {"
            indent = max(0, indent - 1)
            head = head[1:].strip()

        translated = _control(head, module) if _CONTROL.match(head) else None
        if translated is None:
            try:
                translated = translate_command(head, module, long_names)
            except Exception:
                translated = None
            # last line of defence: whatever came out has to BE python. A statement that does not
            # parse is a wrong guess dressed as a result, so it goes back to being MEL, in a comment.
            if translated and not _parses(translated):
                translated = None
            if not translated:
                translated = "# MEL: %s" % head       # unreadable: keep it, flagged, for a human

        lines.append("    " * indent + translated)
        if opens:
            indent += 1

    return "\n".join(lines)


def _statements(mel:str) -> list:
    """Split a script on semicolons and braces, keeping comments and blocks as their own pieces."""
    pieces, current, depth, braces = [], [], 0, []
    tokens = tokenize(mel)
    for token in tokens:
        if token.kind == "comment":
            if current:
                pieces.append(_join(current))
                current = []
            pieces.append(token.text)
            continue
        if token.kind == "op" and token.text in ("(", "["):
            depth += 1
        elif token.kind == "op" and token.text in (")", "]"):
            depth = max(0, depth - 1)
        # a `for (...;...;...)` header holds semicolons that do NOT end a statement, so the split
        # only happens outside brackets
        if token.kind == "op" and token.text == ";" and depth == 0:
            pieces.append(_join(current))
            current = []
            continue
        if token.kind == "op" and token.text == "{":
            # what a brace opens is decided HERE and remembered on a stack: deciding again at the
            # closing brace, from the token before it, mistakes "{}" for an empty block
            block = _block_brace(current)
            braces.append(block)
            if block:
                current.append(token)
                pieces.append(_join(current))
                current = []
                continue
        elif token.kind == "op" and token.text == "}":
            if braces.pop() if braces else True:
                if current:
                    pieces.append(_join(current))
                    current = []
                pieces.append("}")
                continue
        current.append(token)
    if current:
        pieces.append(_join(current))
    return [p for p in pieces if p and p.strip()]


def _block_brace(current:list) -> bool:
    """True when a brace opens a block rather than an array literal.

    `{1, 2, 3}` is a MEL array and belongs to the expression; the brace after `if (...)` opens a body.
    An array literal always follows an assignment or an opening bracket, never a condition.
    """
    if not current:
        return True
    last = current[-1]
    return not (last.kind == "op" and last.text in ("=", "(", ",", "["))


def _join(tokens:list) -> str:
    """Re-emit tokens as MEL text, so the statement translator can re-read them in context."""
    out = []
    for token in tokens:
        if out and token.spaced:
            out.append(" ")
        out.append(token.text)
    return "".join(out)


def _control(head:str, module:str) -> str:
    """Translate a control-flow header, or None when it is not one we recognise."""
    match = re.match(r"^(else\s+if|if|while)\s*\((.*)\)\s*$", head, re.S)
    if match:
        keyword = "elif" if match.group(1).startswith("else") else match.group(1)
        return "%s %s:" % (keyword, translate_expression(tokenize(match.group(2))))
    if re.match(r"^else\s*$", head):
        return "else:"

    match = re.match(r"^for\s*\(\s*(\$\w+)\s+in\s+(.+?)\s*\)\s*$", head, re.S)
    if match:                                          # for ($item in $list)
        return "for %s in %s:" % (match.group(1)[1:],
                                  translate_expression(tokenize(match.group(2))))

    match = re.match(r"^for\s*\(\s*(?:int\s+)?(\$\w+)\s*=\s*(.+?)\s*;\s*"
                     r"\1\s*<\s*(.+?)\s*;\s*\1\s*\+\+\s*\)\s*$", head, re.S)
    if match:                                          # the classic counted loop
        start = translate_expression(tokenize(match.group(2)))
        stop = translate_expression(tokenize(match.group(3)))
        name = match.group(1)[1:]
        return "for %s in range(%s):" % (name, stop if start == "0" else "%s, %s" % (start, stop))

    if head.startswith("for"):
        return None                                    # an exotic loop: let the caller flag it
    return None
