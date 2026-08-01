"""
CODE EDITOR.

Author: Gregoire Dehame
Created: Jul 22, 2026
Modified: Aug 01, 2026
Module: code_editor.core.signature
Execute: from code_editor.core import signature

What a callable takes, described for the Quick Help panel.

Maya's own Quick Help only knows `cmds`. This knows two sources and prefers the richer one:

    a python callable   -> inspect.signature: parameter names, ANNOTATIONS, defaults, and the
                           docstring. That covers your own the host functions, which Maya's help cannot
                           see at all.
    a maya command      -> `cmds.help(name)`, parsed into flags with their argument types. Those are
                           C functions: they have no python signature, and inspect returns nothing.

Both come back in one shape, so the panel does not care which it got.
"""

import inspect
import re

# "   -ax -axis    Float Float Float" - the same rows mel2py reads for flag arity, but parsed here
# too: it wants an arity count, this wants the type names, and neither should bend for the other
_FLAG = re.compile(r"^\s*(-[A-Za-z]\w*)(?:\s+(-[A-Za-z]\w*))?\s*(.*?)\s*$")
_TYPE = re.compile(r"^[A-Za-z][\w|.\[\]]*$")

_CACHE = {}                    # {command: [(short, long, types)]}, maya's help is slow to re-ask

# a section header in a docstring, and one argument row under it. The row form is the one used
# across the host - "name:    (type): - what it is" - with the type and the dash both optional, so a
# plainer Google-style docstring is read just as well.
_SECTION = re.compile(r"^\s*(Args|Arguments|Parameters|Keyword Args)\s*:\s*$", re.I)
_OTHER_SECTION = re.compile(r"^\s*(Returns|Raises|Yields|Examples?|Notes?|Attributes)\s*:\s*$", re.I)
_ARG_ROW = re.compile(r"^(\s+)(\*{0,2}\w+)\s*:\s*(?:\(([^)]*)\)\s*:?)?\s*-?\s*(.*)$")


def doc_arguments(doc:str) -> dict:
    """{name: description} read from a docstring's Args: block.

    This is what turns the panel into documentation rather than a list of names: maya's help gives
    no per-flag prose at all, and for your own functions the description is sitting right there in
    the docstring you already wrote.

    Args:
        doc: (str): - the docstring text to read the Args: block from.

    Returns:
        dict: {name: description} for each documented argument.
    """
    if not doc:
        return {}
    out, inside, name, indent = {}, False, None, 0
    for line in doc.splitlines():
        if _SECTION.match(line):
            inside, name = True, None
            continue
        if not inside:
            continue
        if _OTHER_SECTION.match(line) or (line.strip() and not line[:1].isspace()):
            break                                   # the Args block ended
        match = _ARG_ROW.match(line)
        if match and (name is None or len(match.group(1)) <= indent):
            indent = len(match.group(1))
            name = match.group(2).lstrip("*")
            out[name] = match.group(4).strip()
        elif name and line.strip():
            out[name] = (out[name] + " " + line.strip()).strip()   # a wrapped description
    return out


def _annotation(value) -> str:
    """A parameter annotation as short readable text ('' when there is none).

    Args:
        value: (object): - the annotation object read from a parameter.

    Returns:
        str: the annotation as text, or '' when there is none.
    """
    if value is inspect.Parameter.empty:
        return ""
    if isinstance(value, type):
        return value.__name__
    return str(value).replace("typing.", "")


def _default(parameter) -> str:
    """The default as it would be typed, or '' when the parameter is required.

    Args:
        parameter: (inspect.Parameter): - the parameter to read the default from.

    Returns:
        str: the default as source text, or '' when required.
    """
    if parameter.default is inspect.Parameter.empty:
        return ""
    return repr(parameter.default)


def python_parameters(obj, described:dict=None) -> list:
    """[(name, type, default, description)] from a python callable, [] when it has no signature.

    A class is described by its __init__, since that is what the brackets after its name take.

    Args:
        obj:     (object): - the callable or class to inspect.
        described: (dict): - {name: description} to attach by parameter name.

    Returns:
        list: [(name, type, default, description)] for each parameter.
    """
    target = obj
    if inspect.isclass(obj):
        target = getattr(obj, "__init__", obj)
    try:
        parameters = inspect.signature(target).parameters
    except (TypeError, ValueError):
        return []
    described = described or {}
    out = []
    for name, parameter in parameters.items():
        if name == "self":
            continue
        label = name
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            label = "*" + name
        elif parameter.kind == inspect.Parameter.VAR_KEYWORD:
            label = "**" + name
        out.append((label, _annotation(parameter.annotation), _default(parameter),
                    described.get(name, "")))
    return out


def command_flags(command:str) -> list:
    """[(name, types, short, description)] for a maya command's flags, long names first, cached.

    The long name is what the panel shows: `-constructionHistory` says what it does where `-ch`
    does not, and it is what the OUTPUT panel writes too when it is set to long names. maya's help
    carries no prose per flag, so the description stays empty - the table says so rather than
    inventing one.

    Args:
        command: (str): - the maya command name to read flags for.

    Returns:
        list: [(name, types, short, description)] for each flag.
    """
    if command in _CACHE:
        return _CACHE[command]
    try:
        import maya.cmds as cmds
        text = cmds.help(command) or ""
    except Exception:
        text = ""

    flags = []
    for line in text.splitlines():
        if not line.strip().startswith("-"):
            continue                                # synopsis and prose, not a flag row
        match = _FLAG.match(line)
        if not match:
            continue
        short, long_name, rest = match.groups()
        types = " ".join(word for word in rest.split() if _TYPE.match(word))
        name = (long_name or short).lstrip("-")
        flags.append((name, types or "flag",
                      "-" + short.lstrip("-") if short and long_name else "", ""))
    _CACHE[command] = flags
    return flags


_RETURNS_SECTION = re.compile(r"^\s*Returns?\s*:\s*$", re.I)
_MAYA_RETURN = re.compile(r"^\s*Return value\s*:\s*(\S+)", re.I | re.M)


def return_of(obj, doc:str) -> str:
    """What the call gives back, from whichever source knows.

    Three, in order of trust: the python return annotation, maya's own "Return value: string[]"
    line, and finally the type at the head of a Google-style `Returns:` block - which is the shape
    used across the host.

    Args:
        obj: (object): - the callable or class the return is read from.
        doc:    (str): - the docstring to fall back on for the return type.

    Returns:
        str: the return type as text, or '' when unknown.
    """
    if inspect.isclass(obj):
        return getattr(obj, "__name__", "")
    try:
        annotation = inspect.signature(obj).return_annotation
        if annotation is not inspect.Signature.empty:
            return _annotation(annotation)
    except (TypeError, ValueError):
        pass

    match = _MAYA_RETURN.search(doc or "")
    if match:
        return match.group(1)

    lines = (doc or "").splitlines()
    for index, line in enumerate(lines):
        if not _RETURNS_SECTION.match(line):
            continue
        for follow in lines[index + 1:]:
            if not follow.strip():
                continue
            head = follow.strip().split(":", 1)[0].strip()
            return head if re.match(r"^[\w\[\]., ]+$", head) else ""
        break
    return ""


MAYA_HEADS = ("cmds", "mc", "maya")        # how maya.cmds is spelled in practice


def is_maya_command(name:str, obj) -> bool:
    """True for something like cmds.polyCube.

    Two ways in, because either can miss: the object really being a builtin that maya.cmds exposes,
    or simply the name being written through a cmds alias. Asking maya's help for a name that is not
    a command prints an error into the script editor, so this stays the gate.

    Args:
        name:   (str): - the name as written in the source, e.g. cmds.polyCube.
        obj: (object): - the resolved object, or None.

    Returns:
        bool: True when it is a maya command.
    """
    if name.split(".")[0] in MAYA_HEADS:
        return True
    if obj is None or inspect.isclass(obj):
        return False
    if not (inspect.isbuiltin(obj) or type(obj).__name__ == "builtin_function_or_method"):
        return False
    try:
        import maya.cmds as cmds
        return getattr(cmds, getattr(obj, "__name__", ""), None) is obj
    except Exception:
        return False


def describe(name:str, obj) -> dict:
    """Everything the Quick Help panel needs about `name`.

    A python signature wins when there is one - it carries annotations and defaults, which maya's
    help does not. A maya command has no signature at all, so its flags are read instead.

    Returns {} when there is nothing useful to say.

    Args:
        name:   (str): - the name to describe, e.g. cmds.polyCube.
        obj: (object): - the resolved object, or None.

    Returns:
        dict: everything the panel needs, or {} when nothing useful.
    """
    if obj is None:
        return {}
    short = name.rsplit(".", 1)[-1]
    doc = (inspect.getdoc(obj) or "").strip()

    parameters = python_parameters(obj, doc_arguments(doc))
    if not parameters and is_maya_command(name, obj):
        flags = command_flags(short)
        if flags:
            return {"name": name, "kind": "command", "params": flags, "doc": doc,
                    "returns": return_of(obj, doc), "note": "%d flags" % len(flags)}

    if parameters or callable(obj):
        return {"name": name, "kind": "python", "params": parameters, "doc": doc,
                "returns": return_of(obj, doc),
                "note": "%d parameter%s" % (len(parameters), "" if len(parameters) == 1 else "s")}
    return {}
