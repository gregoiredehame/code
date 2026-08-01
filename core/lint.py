"""
CODE EDITOR.

Author: Gregoire Dehame
Created: Jul 22, 2026
Modified: Aug 01, 2026
Module: code_editor.core.lint
Execute: from code_editor.core import lint

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


def _problem(line:int, column:int, severity:str, message:str, code:str) -> dict:
    """Build one normalised problem record for the Problems panel.

    Args:
        line:     (int): - 1-based line the problem sits on.
        column:   (int): - 0-based column the problem sits on.
        severity: (str): - ERROR or WARNING.
        message:  (str): - human readable description.
        code:     (str): - short machine code for the rule.

    Returns:
        dict: the normalised problem record.
    """
    return {"line": max(1, int(line or 1)), "column": int(column or 0),
            "severity": severity, "message": message, "code": code}


def check(path:str="", source:str="") -> list:
    """Return the problems found in `source`, errors first then by line.

    Args:
        path:   (str): - file path used only for error messages.
        source: (str): - python source code to analyse.

    Returns:
        list: problem dicts, errors first then ordered by line.
    """
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

    Args:
        path:   (str): - file path used only for error messages.
        source: (str): - python source code to compile.

    Returns:
        list: warning problem dicts found while compiling.
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

    def __init__(self, name:str, kind:str, line:int, column:int) -> None:
        """Store one binding and its source location.

        Args:
            name:   (str): - the bound name.
            kind:   (str): - what created it (import, assignment, ...).
            line:   (int): - 1-based line of the binding.
            column: (int): - 0-based column of the binding.

        Returns:
            None: nothing.
        """
        self.name, self.kind = name, kind
        self.line, self.column = line, column
        self.used = False


class _Scope(object):
    """A python scope. `kind` drives lookup: class bodies are skipped when resolving from inside."""

    def __init__(self, kind:str, parent=None) -> None:
        """Create a scope of `kind` nested under `parent`.

        Args:
            kind:      (str): - module, function, class or comprehension.
            parent: (object): - the enclosing scope, or None at module level.

        Returns:
            None: nothing.
        """
        self.kind = kind                     # module | function | class | comprehension
        self.parent = parent
        self.bindings = {}
        self.globals = set()                 # names declared global/nonlocal here
        self.star_import = False

    def bind(self, binding:_Binding) -> None:
        """Record `binding` under its name in this scope.

        Args:
            binding: (_Binding): - the binding to store.

        Returns:
            None: nothing.
        """
        self.bindings[binding.name] = binding

    def get(self, name:str) -> object:
        """Return the binding for `name` in this scope, or None.

        Args:
            name: (str): - name to look up.

        Returns:
            object: the _Binding, or None when absent.
        """
        return self.bindings.get(name)


class _Checker(ast.NodeVisitor):
    """Walks the tree building scopes, deferring function bodies, then reports what it found."""

    def __init__(self, path:str="") -> None:
        """Prepare an empty checker for the file at `path`.

        Args:
            path: (str): - file path, used for messages and __init__ handling.

        Returns:
            None: nothing.
        """
        self.path = path or "<untitled>"
        self.problems = []
        self.scope = None
        self.scopes = []
        self._deferred = []                  # (node, scope chain) processed after the module pass
        self._any_star_import = False
        self._branching = 0                  # depth inside try/except or if/else alternatives
        self._pending = []                   # scopes whose unused report waits for nested bodies

    # ---- entry point

    def run(self, tree:ast.AST) -> None:
        """Walk `tree`, defer function bodies, then report every finding.

        Args:
            tree: (ast.AST): - the parsed module to check.

        Returns:
            None: nothing.
        """
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
    def _mark_exported(tree:ast.AST, module:_Scope) -> None:
        """A name listed in __all__ is part of the module's surface, so it counts as used.

        Args:
            tree:  (ast.AST): - the parsed module.
            module: (_Scope): - the module scope holding the bindings.

        Returns:
            None: nothing.
        """
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

    def _push(self, kind:str) -> _Scope:
        """Create and enter a child scope of `kind`.

        Args:
            kind: (str): - the new scope's kind.

        Returns:
            _Scope: the scope just pushed.
        """
        scope = _Scope(kind, self.scope)
        self.scopes.append(scope)
        self.scope = scope
        return scope

    def _pop(self) -> _Scope:
        """Leave the current scope and return to its parent.

        Returns:
            _Scope: the scope just popped.
        """
        scope = self.scopes.pop()
        self.scope = self.scopes[-1] if self.scopes else None
        return scope

    def _bind(self, name:str, kind:str, node:ast.AST) -> None:
        """Bind `name` in the current scope, warning on real redefinitions.

        Args:
            name:     (str): - name being bound.
            kind:     (str): - what created the binding.
            node: (ast.AST): - AST node the binding comes from.

        Returns:
            None: nothing.
        """
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

    def _resolve(self, name:str, node:ast.AST) -> None:
        """Mark `name` used along the scope chain; report it when nothing binds it.

        The scope chain is walked FIRST, builtins only after. Short-cutting on the builtin name, as
        this did, meant a local that shadows one never had its use recorded: `for a, b, format in
        rules: setFormat(..., format)` reported `format` as assigned and never used, because every
        read of it resolved to the builtin instead of to the loop variable.

        Args:
            name:     (str): - name being read.
            node: (ast.AST): - AST node of the read.

        Returns:
            None: nothing.
        """
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
        if name in _BUILTINS:
            return                           # nothing local binds it, so it really is the builtin
        if self._any_star_import:
            return
        self.problems.append(_problem(node.lineno, node.col_offset, ERROR,
                                      '"%s" is not defined' % name, "undefined"))

    def _report_unused(self, scope:_Scope, module_level:bool=False) -> None:
        """Unused imports (any scope) and unused locals (functions only).

        Args:
            scope:        (_Scope): - the scope whose bindings to inspect.
            module_level:   (bool): - True when `scope` is the module scope.

        Returns:
            None: nothing.
        """
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

    def visit_Import(self, node:ast.Import) -> None:
        """Bind each `import x` name in the current scope.

        Args:
            node: (ast.Import): - the import statement.

        Returns:
            None: nothing.
        """
        for alias in node.names:
            self._bind(alias.asname or alias.name.split(".")[0], "import", node)

    def visit_ImportFrom(self, node:ast.ImportFrom) -> None:
        """Bind each `from x import y` name, honouring star imports and re-exports.

        Args:
            node: (ast.ImportFrom): - the from-import statement.

        Returns:
            None: nothing.
        """
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

    def visit_FunctionDef(self, node:ast.AST) -> None:
        """Bind the function name and defer its body to the enclosing pass.

        Args:
            node: (ast.AST): - the function definition.

        Returns:
            None: nothing.
        """
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._visit_signature(node)
        self._bind(node.name, "function", node)
        # body deferred: it may legitimately use names bound later in the enclosing scope
        self._deferred.append((node, list(self.scopes)))

    visit_AsyncFunctionDef = visit_FunctionDef

    def _visit_signature(self, node:ast.AST) -> None:
        """Defaults and annotations are evaluated in the ENCLOSING scope, not the function's.

        Args:
            node: (ast.AST): - the function or lambda whose signature to visit.

        Returns:
            None: nothing.
        """
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

    def _walk_function_body(self, node:ast.AST) -> None:
        """Enter a function scope, bind its arguments and visit its body.

        Args:
            node: (ast.AST): - the function whose body to walk.

        Returns:
            None: nothing.
        """
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

    def visit_ClassDef(self, node:ast.ClassDef) -> None:
        """Bind the class name, then visit its body in a class scope.

        Args:
            node: (ast.ClassDef): - the class definition.

        Returns:
            None: nothing.
        """
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in list(node.bases) + [k.value for k in node.keywords]:
            self.visit(base)
        self._bind(node.name, "class", node)
        self._push("class")
        for statement in node.body:
            self.visit(statement)
        self._pending.append((self._pop(), False))

    def visit_Lambda(self, node:ast.Lambda) -> None:
        """Visit a lambda's signature and body in a fresh function scope.

        Args:
            node: (ast.Lambda): - the lambda expression.

        Returns:
            None: nothing.
        """
        self._visit_signature(node)
        self._push("function")
        args = node.args
        for argument in (list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
                         + [a for a in (args.vararg, args.kwarg) if a]):
            self._bind(argument.arg, "argument", node)
        self.visit(node.body)
        self._pop()

    # ---- names and bindings

    def visit_Name(self, node:ast.Name) -> None:
        """Resolve a read name, or bind it when it is a write target.

        Args:
            node: (ast.Name): - the name node.

        Returns:
            None: nothing.
        """
        if isinstance(node.ctx, ast.Load):
            self._resolve(node.id, node)
        else:
            self._bind(node.id, "assignment", node)

    def visit_Global(self, node:ast.AST) -> None:
        """Record global/nonlocal names and bind them at module level.

        Args:
            node: (ast.AST): - the global or nonlocal statement.

        Returns:
            None: nothing.
        """
        self.scope.globals.update(node.names)
        for name in node.names:              # a global is bound at module level by convention
            self.scopes[0].bind(_Binding(name, "assignment", node.lineno, node.col_offset))

    visit_Nonlocal = visit_Global

    def visit_Try(self, node:ast.AST) -> None:
        """try/except/else/finally: every branch is an alternative, so rebinding across them is fine.

        Args:
            node: (ast.AST): - the try statement.

        Returns:
            None: nothing.
        """
        self._branching += 1
        self.generic_visit(node)
        self._branching -= 1

    visit_TryStar = visit_Try

    def visit_If(self, node:ast.If) -> None:
        """An if with an else is the same story: only one of the two branches ever binds.

        Args:
            node: (ast.If): - the if statement.

        Returns:
            None: nothing.
        """
        self.visit(node.test)
        branching = bool(node.orelse)
        self._branching += branching
        for statement in node.body + node.orelse:
            self.visit(statement)
        self._branching -= branching

    def visit_ExceptHandler(self, node:ast.ExceptHandler) -> None:
        """Warn on bare `except:` and bind the caught exception name.

        Args:
            node: (ast.ExceptHandler): - the except clause.

        Returns:
            None: nothing.
        """
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

    def visit_comprehension_scope(self, node:ast.AST) -> None:
        """List/set/dict comprehensions and generators own a scope in python 3.

        Args:
            node: (ast.AST): - the comprehension or generator expression.

        Returns:
            None: nothing.
        """
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
