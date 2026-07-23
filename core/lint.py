"""
KATA. (c)

Author: Gregoire Dehame
Created: Jul 22, 2026
Module: ui.code_editor.core.lint
Execute: from kata.ui.code_editor.core import lint

Scope-aware static checks for the Problems panel, written on the standard `ast` module so nothing has to
be installed into Maya.

The analysis mirrors how python actually resolves names: a chain of scopes (module, function, class,
comprehension) walked outwards, skipping class bodies the way LEGB does. Function bodies are *deferred*
until their enclosing scope is complete, so calling a helper defined further down the file is correctly
seen as valid - that deferral is what separates a real checker from a flat pass.

Reported: syntax errors, undefined names, unused imports, unused local variables, redefinitions, bare
`except:`, mutable default arguments and duplicated definitions.

Import resolution is deliberately NOT checked: inside Maya `maya.cmds` resolves fine, so the
"could not be resolved" warnings an external editor shows would be pure noise here.
"""

import os
import ast
import builtins

ERROR = "error"
WARNING = "warning"

_BUILTINS = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__debug__", "__path__",
}


def _problem(line, column, severity, message, code) -> dict:
    return {"line": max(1, int(line or 1)), "column": int(column or 0),
            "severity": severity, "message": message, "code": code}


def check(path: str = "", source: str = "") -> list:
    """Return the problems found in `source`, errors first then by line."""
    if not source.strip():
        return []
    try:
        tree = ast.parse(source, filename=path or "<untitled>")
    except SyntaxError as error:
        return [_problem(error.lineno, error.offset, ERROR, error.msg or "invalid syntax", "syntax")]
    except Exception:
        return []

    checker = _Checker(path)
    checker.run(tree)
    problems = checker.problems + compiler_warnings(path, source)
    problems.sort(key=lambda item: (item["severity"] != ERROR, item["line"], item["column"]))
    return problems


def compiler_warnings(path:str, source:str) -> list:
    """What CPython itself complains about while compiling `source`.

    The compiler already detects a handful of real bugs that no amount of ast walking will find,
    because they are decided during tokenisation or constant folding:

        "\\d+"                  an invalid escape sequence, silently a literal backslash today and
                                a hard error in a future python
        if x is "abc"           identity compared against a literal, which works by accident
        assert (cond, "msg")    a tuple, so the assertion can never fail

    They cost one extra compile() - microseconds - and need nothing installed, so they run always.
    """
    import warnings
    found = []
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            compile(source, path or "<untitled>", "exec")
        for entry in caught:
            found.append(_problem(getattr(entry, "lineno", 1), 0, WARNING,
                                  str(entry.message), "compiler"))
    except Exception:
        pass                                 # a syntax error is already reported by the caller
    return found


# --------------------------------------------------------------------------------------- scope model

class _Binding(object):
    """One name bound in a scope, with what bound it and whether anything read it."""

    __slots__ = ("name", "kind", "line", "column", "used")

    def __init__(self, name, kind, line, column):
        self.name, self.kind = name, kind
        self.line, self.column = line, column
        self.used = False


class _Scope(object):
    """A python scope. `kind` drives lookup: class bodies are skipped when resolving from inside."""

    def __init__(self, kind, parent=None):
        self.kind = kind                     # module | function | class | comprehension
        self.parent = parent
        self.bindings = {}
        self.globals = set()                 # names declared global/nonlocal here
        self.star_import = False

    def bind(self, binding):
        self.bindings[binding.name] = binding

    def get(self, name):
        return self.bindings.get(name)


class _Checker(ast.NodeVisitor):
    """Walks the tree building scopes, deferring function bodies, then reports what it found."""

    def __init__(self, path=""):
        self.path = path or "<untitled>"
        self.problems = []
        self.scope = None
        self.scopes = []
        self._deferred = []                  # (node, scope chain) processed after the module pass
        self._any_star_import = False
        self._branching = 0                  # depth inside try/except or if/else alternatives
        self._pending = []                   # scopes whose unused report waits for nested bodies

    # ---- entry point

    def run(self, tree):
        module = _Scope("module")
        self.scope = module
        self.scopes = [module]

        for node in tree.body:
            self.visit(node)

        # function bodies see everything the module bound, including names defined after them
        while self._deferred:
            node, chain = self._deferred.pop(0)
            saved = self.scopes
            self.scopes = chain
            self.scope = chain[-1]
            self._walk_function_body(node)
            self.scopes = saved
            self.scope = saved[-1]

        self._mark_exported(tree, module)
        for scope, module_level in self._pending:
            self._report_unused(scope, module_level)
        self._report_unused(module, module_level=True)

    @staticmethod
    def _mark_exported(tree, module):
        """A name listed in __all__ is part of the module's surface, so it counts as used."""
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(getattr(target, "id", "") == "__all__" for target in node.targets):
                continue
            for element in getattr(node.value, "elts", []):
                name = getattr(element, "value", None)
                binding = module.get(name) if isinstance(name, str) else None
                if binding is not None:
                    binding.used = True

    # ---- scope helpers

    def _push(self, kind):
        scope = _Scope(kind, self.scope)
        self.scopes.append(scope)
        self.scope = scope
        return scope

    def _pop(self):
        scope = self.scopes.pop()
        self.scope = self.scopes[-1] if self.scopes else None
        return scope

    def _bind(self, name, kind, node):
        if not name:
            return
        scope = self.scope
        if name in scope.globals:            # declared global/nonlocal: the binding lives elsewhere
            return
        previous = scope.get(name)
        # a name bound twice inside a try/except or an if/else is a FALLBACK, not a mistake:
        #     try:    from PySide6 import QtCore
        #     except: from PySide2 import QtCore
        # exactly one of them ever runs, so reporting the second as a redefinition is noise.
        if (previous is not None and not self._branching and not previous.used
                and previous.kind in ("import", "function", "class")
                and kind in ("import", "function", "class")):
            self.problems.append(_problem(
                node.lineno, node.col_offset, WARNING,
                '"%s" is redefined, the one on line %d is never used' % (name, previous.line),
                "redefined"))
        binding = _Binding(name, kind, node.lineno, node.col_offset)
        # a rebind inherits the old binding's "was read" flag. Without this, the counter in
        #     while position < len(text):
        #         position = match.end()
        # is reported as never used: the read happened against the FIRST binding, and the assignment
        # that follows it replaced that binding with a fresh, unread one.
        if previous is not None and previous.used:
            binding.used = True
        scope.bind(binding)

    def _resolve(self, name, node):
        """Mark `name` used along the scope chain; report it when nothing binds it."""
        if name in _BUILTINS:
            return
        for scope in reversed(self.scopes):
            # a class body is not visible from the scopes nested inside it
            if scope.kind == "class" and scope is not self.scope:
                continue
            binding = scope.get(name)
            if binding is not None:
                binding.used = True
                return
            if scope.star_import:
                return                       # `from x import *`: cannot know, stay silent
        if self._any_star_import:
            return
        self.problems.append(_problem(node.lineno, node.col_offset, ERROR,
                                      '"%s" is not defined' % name, "undefined"))

    def _report_unused(self, scope, module_level=False):
        """Unused imports (any scope) and unused locals (functions only)."""
        for binding in scope.bindings.values():
            if binding.used or binding.name.startswith("_"):
                continue
            if binding.kind == "import":
                # an __init__.py exists to re-export: every import in it is used by definition, by
                # whoever imports the package. Reporting them all would drown the real findings.
                if module_level and os.path.basename(self.path).lower() == "__init__.py":
                    continue
                self.problems.append(_problem(
                    binding.line, binding.column, WARNING,
                    '"%s" is imported but never used' % binding.name, "unused-import"))
            elif binding.kind == "assignment" and not module_level and scope.kind == "function":
                self.problems.append(_problem(
                    binding.line, binding.column, WARNING,
                    '"%s" is assigned but never used' % binding.name, "unused-variable"))

    # ---- imports

    def visit_Import(self, node):
        for alias in node.names:
            self._bind(alias.asname or alias.name.split(".")[0], "import", node)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if alias.name == "*":
                self.scope.star_import = True
                self._any_star_import = True
                continue
            self._bind(alias.asname or alias.name, "import", node)
            # `from x import y as y` is the standard way to say "re-exported on purpose" (the same
            # spelling mypy reads for implicit re-export). A shim module can use it to stay quiet.
            if alias.asname and alias.asname == alias.name:
                binding = self.scope.get(alias.asname)
                if binding is not None:
                    binding.used = True

    # ---- definitions

    def visit_FunctionDef(self, node):
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._visit_signature(node)
        self._bind(node.name, "function", node)
        # body deferred: it may legitimately use names bound later in the enclosing scope
        self._deferred.append((node, list(self.scopes)))

    visit_AsyncFunctionDef = visit_FunctionDef

    def _visit_signature(self, node):
        """Defaults and annotations are evaluated in the ENCLOSING scope, not the function's."""
        args = node.args
        for default in list(args.defaults) + [d for d in args.kw_defaults if d]:
            self.visit(default)
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                self.problems.append(_problem(
                    default.lineno, default.col_offset, WARNING,
                    "mutable default argument is shared between calls", "mutable-default"))
        for argument in list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs):
            if argument.annotation:
                self.visit(argument.annotation)
        for extra in (args.vararg, args.kwarg):
            if extra is not None and extra.annotation:
                self.visit(extra.annotation)
        if getattr(node, "returns", None):
            self.visit(node.returns)

    def _walk_function_body(self, node):
        self._push("function")
        args = node.args
        for argument in (list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
                         + [a for a in (args.vararg, args.kwarg) if a]):
            self._bind(argument.arg, "argument", node)
        for statement in node.body:
            self.visit(statement)
        # reported at the very end, not here: the functions defined INSIDE this one are still on the
        # deferred queue, and a local they capture would otherwise look unused
        self._pending.append((self._pop(), False))

    def visit_ClassDef(self, node):
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in list(node.bases) + [k.value for k in node.keywords]:
            self.visit(base)
        self._bind(node.name, "class", node)
        self._push("class")
        for statement in node.body:
            self.visit(statement)
        self._pending.append((self._pop(), False))

    def visit_Lambda(self, node):
        self._visit_signature(node)
        self._push("function")
        args = node.args
        for argument in (list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
                         + [a for a in (args.vararg, args.kwarg) if a]):
            self._bind(argument.arg, "argument", node)
        self.visit(node.body)
        self._pop()

    # ---- names and bindings

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self._resolve(node.id, node)
        else:
            self._bind(node.id, "assignment", node)

    def visit_Global(self, node):
        self.scope.globals.update(node.names)
        for name in node.names:              # a global is bound at module level by convention
            self.scopes[0].bind(_Binding(name, "assignment", node.lineno, node.col_offset))

    visit_Nonlocal = visit_Global

    def visit_Try(self, node):
        """try/except/else/finally: every branch is an alternative, so rebinding across them is fine."""
        self._branching += 1
        self.generic_visit(node)
        self._branching -= 1

    visit_TryStar = visit_Try

    def visit_If(self, node):
        """An if with an else is the same story: only one of the two branches ever binds."""
        self.visit(node.test)
        branching = bool(node.orelse)
        self._branching += branching
        for statement in node.body + node.orelse:
            self.visit(statement)
        self._branching -= branching

    def visit_ExceptHandler(self, node):
        if node.type is None:
            self.problems.append(_problem(
                node.lineno, node.col_offset, WARNING,
                "bare 'except:' also swallows KeyboardInterrupt and SystemExit", "bare-except"))
        else:
            self.visit(node.type)
        if node.name:
            self._bind(node.name, "exception", node)
        for statement in node.body:
            self.visit(statement)

    def visit_comprehension_scope(self, node):
        """List/set/dict comprehensions and generators own a scope in python 3."""
        self._push("comprehension")
        for index, generator in enumerate(node.generators):
            if index == 0:
                # the first iterable is evaluated in the enclosing scope
                self.scopes.pop()
                self.scope = self.scopes[-1]
                self.visit(generator.iter)
                self.scopes.append(_Scope("comprehension", self.scope))
                self.scope = self.scopes[-1]
            else:
                self.visit(generator.iter)
            self.visit(generator.target)
            for condition in generator.ifs:
                self.visit(condition)
        for attribute in ("elt", "key", "value"):
            child = getattr(node, attribute, None)
            if child is not None:
                self.visit(child)
        self._pop()

    visit_ListComp = visit_comprehension_scope
    visit_SetComp = visit_comprehension_scope
    visit_GeneratorExp = visit_comprehension_scope
    visit_DictComp = visit_comprehension_scope
