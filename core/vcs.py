"""
CODE EDITOR.

Author: Gregoire Dehame
Created: Jul 21, 2026
Modified: Aug 01, 2026
Module: code_editor.core.vcs
Execute: from code_editor.core import vcs

Tiny, self-contained Git-status helper for the workspace tree (VS Code-style M/A/U/D/R markers). Runs
`git status --porcelain` under a folder and returns a {absolute_path: status_letter} map. No dependency on
the rest of the host; degrades silently to an empty map when the folder is not a git repo or git is missing.
"""

import os
import subprocess

# porcelain XY codes we surface, mapped to a single VS Code-style letter
# (index/worktree combined; worktree change wins for display, like VS Code)
_LETTER = {
    "M": "M",   # modified
    "A": "A",   # added (staged new)
    "D": "D",   # deleted
    "R": "R",   # renamed
    "C": "C",   # copied
    "?": "U",   # untracked -> U (VS Code shows "U")
    "!": None,  # ignored -> no marker
    "U": "C",   # unmerged -> treat as conflict "C"
}


def is_git_repo(folder:str=None) -> bool:
    """Return True if `folder` is inside a git working tree.

    Args:
        folder: (str): - path to test.

    Returns:
        bool: True when the folder is inside a git working tree.
    """
    if not folder or not os.path.isdir(folder):
        return False
    try:
        out = subprocess.run(
            ["git", "-C", folder, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
            creationflags=_no_window())
        return out.returncode == 0 and out.stdout.strip() == "true"
    except Exception:
        return False


def submodules(folder:str=None) -> set:
    """Return the set of absolute submodule paths under `folder`'s git repo.

    Args:
        folder: (str): - path inside the repository to inspect.

    Returns:
        set: absolute submodule paths (empty if none or not a repo).
    """
    result = set()
    if not folder or not os.path.isdir(folder):
        return result
    try:
        top = subprocess.run(
            ["git", "-C", folder, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, creationflags=_no_window())
        base = top.stdout.strip()
        if not base:
            return result
        out = subprocess.run(
            ["git", "-C", folder, "config", "--file", os.path.join(base, ".gitmodules"),
             "--get-regexp", "path"],
            capture_output=True, text=True, timeout=5, creationflags=_no_window())
        for line in out.stdout.splitlines():
            # "submodule.<name>.path <relative-path>"
            parts = line.split(None, 1)
            if len(parts) == 2:
                result.add(os.path.normpath(os.path.join(base, parts[1].strip())))
    except Exception:
        pass
    return result


def status(folder:str=None) -> dict:
    """Return {absolute_path: letter} for every changed path under `folder`'s git repo.

    Letters follow VS Code: M (modified), A (added), U (untracked), D (deleted), R (renamed), C (conflict).
    Returns an empty dict when `folder` is not a git repo or git is unavailable.

    Args:
        folder: (str): - path inside the repository to inspect.

    Returns:
        dict: {absolute_path: letter} for every changed path.
    """
    result = {}
    if not folder or not os.path.isdir(folder):
        return result
    try:
        # --porcelain -z gives NUL-separated entries, robust to spaces/unicode in names
        out = subprocess.run(
            ["git", "-C", folder, "status", "--porcelain", "-z", "--untracked-files=all"],
            capture_output=True, text=True, timeout=15,
            creationflags=_no_window())
        if out.returncode != 0:
            return result
        # repo root, so we can build absolute paths
        root = subprocess.run(
            ["git", "-C", folder, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
            creationflags=_no_window())
        base = root.stdout.strip() or folder
    except Exception:
        return result

    _parse_porcelain(out.stdout, base, result)
    return result


def _entries(raw:str, base:str) -> object:
    """Yield (index_status, worktree_status, absolute_path) for each `git status --porcelain -z` entry.

    Entries are NUL-separated with a fixed layout: "XY PATH" (2 status chars, one space, then the path,
    which may itself contain spaces). A rename adds a second NUL field holding the old name.

    Args:
        raw:  (str): - raw NUL-separated porcelain output.
        base: (str): - repository root used to build absolute paths.

    Returns:
        object: generator of (index_status, worktree_status, absolute_path) tuples.
    """
    tokens = raw.split("\0")
    index = 0
    while index < len(tokens):
        entry = tokens[index]
        if len(entry) < 3:                     # empty or malformed
            index += 1
            continue
        x, y = entry[0], entry[1]              # index status, worktree status (fixed columns)
        path = entry[3:]                       # skip "XY " (2 status chars + 1 separator space)
        if x == "R" or y == "R":
            index += 1                         # consume the "old name" field that follows
        if path:
            yield x, y, os.path.normpath(os.path.join(base, path))
        index += 1


def _parse_porcelain(raw:str, base:str, result:dict) -> None:
    """Fill `result` with one collapsed letter per path (worktree change wins over the index one).

    Args:
        raw:     (str): - raw NUL-separated porcelain output.
        base:    (str): - repository root used to build absolute paths.
        result: (dict): - mapping filled in place with {path: letter}.

    Returns:
        None: `result` is modified in place.
    """
    for x, y, path in _entries(raw, base):
        code = y if y != " " else x
        letter = "R" if (x == "R" or y == "R") else _LETTER.get(code)
        if letter:
            result[path] = letter


def changes(folder:str=None) -> tuple:
    """Return (staged, unstaged) dicts of {absolute_path: letter} for `folder`'s repo.

    Git tracks two independent states per file: the index (staged) and the working tree (unstaged), so a
    file can legitimately appear in both - edited, staged, then edited again.

    Args:
        folder: (str): - path inside the repository to inspect.

    Returns:
        tuple: (staged, unstaged) dicts of {absolute_path: letter}.
    """
    staged, unstaged = {}, {}
    if not folder or not os.path.isdir(folder):
        return staged, unstaged
    try:
        out = subprocess.run(
            ["git", "-C", folder, "status", "--porcelain", "-z", "--untracked-files=all"],
            capture_output=True, text=True, timeout=15, creationflags=_no_window())
        if out.returncode != 0:
            return staged, unstaged
        root = subprocess.run(
            ["git", "-C", folder, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, creationflags=_no_window())
        base = root.stdout.strip() or folder
    except Exception:
        return staged, unstaged

    for x, y, path in _entries(out.stdout, base):
        if x == "?" or y == "?":
            unstaged[path] = "U"
            continue
        if x != " ":
            staged[path] = _LETTER.get(x) or "M"
        if y != " ":
            unstaged[path] = _LETTER.get(y) or "M"
    return staged, unstaged


# Which repository a folder belongs to, and what branch it is on, are read constantly - every tab
# click asked git twice - and they change only when you switch branch or add a root. Each answer
# costs a process spawn (~70ms on windows), so they are remembered until something says otherwise.
_CACHE = {}


def invalidate() -> None:
    """Forget the cached repository roots and branch names.

    Called whenever git state is re-read.

    Returns:
        None: clears the module cache.
    """
    _CACHE.clear()


def repo_root_cached(folder:str=None) -> str:
    """`repo_root`, answered from memory after the first time.

    Args:
        folder: (str): - path inside the repository to inspect.

    Returns:
        str: absolute repository root, or an empty string.
    """
    key = ("root", os.path.normcase(folder or ""))
    if key not in _CACHE:
        _CACHE[key] = repo_root(folder)
    return _CACHE[key]


def branch_cached(folder:str=None) -> str:
    """`branch`, answered from memory after the first time.

    Args:
        folder: (str): - path inside the repository to inspect.

    Returns:
        str: current branch name, or an empty string.
    """
    key = ("branch", os.path.normcase(folder or ""))
    if key not in _CACHE:
        _CACHE[key] = branch(folder)
    return _CACHE[key]


def repo_root(folder:str=None) -> str:
    """Absolute path of the repository `folder` belongs to, or an empty string.

    Args:
        folder: (str): - path inside the repository to inspect.

    Returns:
        str: absolute repository root, or an empty string.
    """
    if not folder or not os.path.isdir(folder):
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", folder, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, creationflags=_no_window())
        return os.path.normpath(out.stdout.strip()) if out.returncode == 0 else ""
    except Exception:
        return ""


def show(folder:str=None, path:str=None, ref:str="HEAD") -> str:
    """Return a file's content at a git revision.

    `ref` is "HEAD" for the last commit or "" for the index (staged) copy. Returns an empty string when
    the blob does not exist there, which is exactly what a newly added file looks like.

    Args:
        folder: (str): - path inside the repository to inspect.
        path:   (str): - absolute path of the file to read.
        ref:    (str): - git revision to read from.

    Returns:
        str: the file's content at that revision, or an empty string.
    """
    root = repo_root(folder)
    if not root or not path:
        return ""
    relative = os.path.relpath(os.path.normpath(path), root).replace("\\", "/")
    try:
        out = subprocess.run(
            ["git", "-C", root, "show", "%s:%s" % (ref, relative)],
            capture_output=True, timeout=15, creationflags=_no_window())
        if out.returncode != 0:
            return ""
        raw = out.stdout or b""
    except Exception:
        return ""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def log(folder:str=None, limit:int=60, path:str=None) -> list:
    """Return the latest commits as dicts.

    Keys: hash, short, subject, author, email, date, relative, refs (list of ref names) and body. Records
    are separated by \\x1e and fields by \\x1f, because a commit body legitimately contains newlines.

    With `path`, only the commits that touched that ONE file, followed through renames - which is what
    a per-file timeline needs. --follow accepts a single path only, hence one file rather than a list.

    Args:
        folder: (str): - path inside the repository to inspect.
        limit:  (int): - maximum number of commits to return.
        path:   (str): - restrict history to a single file.

    Returns:
        list: commit dicts, newest first.
    """
    commits = []
    root = repo_root(folder)
    if not root:
        return commits

    field, record = "\x1f", "\x1e"
    pattern = field.join(["%H", "%h", "%s", "%an", "%ae", "%ad", "%ar", "%D", "%b"]) + record
    command = ["git", "-C", root, "log", "--max-count=%d" % max(1, limit),
               "--date=format:%Y-%m-%d %H:%M", "--pretty=format:" + pattern]
    if path:
        try:
            relative = os.path.relpath(path, root).replace("\\", "/")
        except ValueError:
            return commits                      # another drive: not in this repository
        command += ["--follow", "--", relative]
    try:
        out = subprocess.run(command, capture_output=True, text=True, timeout=15,
                             creationflags=_no_window())
        if out.returncode != 0:
            return commits
    except Exception:
        return commits

    for chunk in out.stdout.split(record):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        parts = chunk.split(field)
        if len(parts) < 9:
            continue
        refs = [r.strip() for r in parts[7].split(",") if r.strip()] if parts[7] else []
        commits.append({"hash": parts[0], "short": parts[1], "subject": parts[2],
                        "author": parts[3], "email": parts[4], "date": parts[5],
                        "relative": parts[6], "refs": refs, "body": parts[8].strip()})
    return commits


def patch(folder:str=None, sha:str=None, path:str=None) -> str:
    """The full diff a commit introduced, as `git show` prints it.

    With `path`, only that file's hunks - which is what you want when you came from a file timeline
    and the commit touched forty others.

    Args:
        folder: (str): - path inside the repository to inspect.
        sha:    (str): - commit hash to diff.
        path:   (str): - restrict the diff to a single file.

    Returns:
        str: the commit's diff, or an empty string.
    """
    root = repo_root(folder)
    if not root or not sha:
        return ""
    command = ["git", "-C", root, "show", "--stat", "--patch", "--no-color", sha]
    if path:
        try:
            command += ["--", os.path.relpath(path, root).replace("\\", "/")]
        except ValueError:
            return ""
    try:
        out = subprocess.run(command, capture_output=True, text=True, timeout=20,
                             creationflags=_no_window())
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


def remote_url(folder:str=None, name:str="origin") -> str:
    """The remote's address as a BROWSABLE https url, or "" when there is none to browse.

    Remotes are written three ways - git@host:owner/repo.git, ssh://git@host/owner/repo, and plain
    https - and only the last can be opened. The first two are rewritten rather than rejected.

    Args:
        folder: (str): - path inside the repository to inspect.
        name:   (str): - remote name to look up.

    Returns:
        str: a browsable https url, or an empty string.
    """
    root = repo_root(folder)
    if not root:
        return ""
    try:
        out = subprocess.run(["git", "-C", root, "remote", "get-url", name],
                             capture_output=True, text=True, timeout=5, creationflags=_no_window())
        url = out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""
    if not url:
        return ""

    if url.startswith("git@"):                       # git@github.com:owner/repo.git
        url = "https://" + url[4:].replace(":", "/", 1)
    elif url.startswith("ssh://git@"):
        url = "https://" + url[len("ssh://git@"):]
    if url.endswith(".git"):
        url = url[:-4]
    return url if url.startswith("http") else ""


def commit_url(folder:str=None, sha:str=None) -> str:
    """A web address for `sha`, when the remote is a host whose url layout we know.

    Args:
        folder: (str): - path inside the repository to inspect.
        sha:    (str): - commit hash to build a url for.

    Returns:
        str: a web url for the commit, or an empty string.
    """
    url = remote_url(folder)
    if not url or not sha:
        return ""
    host = url.split("/")[2].lower() if "//" in url else ""
    if "github" in host or "gitlab" in host:
        return "%s/commit/%s" % (url.rstrip("/"), sha)
    if "bitbucket" in host:
        return "%s/commits/%s" % (url.rstrip("/"), sha)
    return ""                                        # unknown host: better nothing than a 404


def branch(folder:str=None) -> str:
    """Current branch name, or an empty string when unavailable.

    Args:
        folder: (str): - path inside the repository to inspect.

    Returns:
        str: the current branch name, or an empty string.
    """
    if not folder or not os.path.isdir(folder):
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", folder, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5, creationflags=_no_window())
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _no_window() -> int:
    """On Windows, prevent a console window flashing when git is spawned.

    Returns:
        int: the subprocess creation flag, or 0 on non-Windows.
    """
    try:
        return subprocess.CREATE_NO_WINDOW      # windows only
    except AttributeError:
        return 0
