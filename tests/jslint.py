"""
A lexical validator for the dashboard's JavaScript.

Not a parser and not a linter. It tokenises the things that are easy to break
by editing a file with a script -- string literals, template literals,
comments, regular expressions -- and asserts that every one of them is closed
and that brackets balance.

It exists because a real syntax error shipped to production. A stray newline
inside a string literal left it unterminated, and a syntax error anywhere in
``app.js`` stops the *whole file* executing: the page rendered as signed out
and the sign-in button did nothing, because no handler had ever been bound.
Checking element ids and handler names -- which is what was being done --
cannot catch that.

``node --check`` would be strictly better and is what should be used when a
JavaScript runtime is available. This is the check that works without one.
"""

from typing import List, NamedTuple

# A "/" starts a regular expression rather than a division when the previous
# meaningful token cannot end an expression. This is the standard heuristic;
# it is not perfect, but the alternative is a full parser.
_REGEX_PRECEDING_PUNCTUATION = set("(,=:[!&|?{};+-*%~^<>")
_REGEX_PRECEDING_KEYWORDS = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "do", "else", "yield", "await", "case", "throw",
}


class JsProblem(NamedTuple):
    line: int
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.kind} -- {self.detail}"


def check_javascript(source: str) -> List[JsProblem]:
    """
    Return every lexical problem found, or an empty list.

    Reports the first line of an unterminated literal rather than the end of
    the file, because that is where the fix goes.
    """
    problems: List[JsProblem] = []
    # Stack of open brackets, and of "${" contexts inside template literals,
    # so a nested template inside an interpolation is handled.
    brackets: List[tuple] = []
    i = 0
    line = 1
    length = len(source)
    # The last meaningful character and word, for the regex heuristic.
    prev_char = ""
    prev_word = ""

    def rest_of_word(pos: int) -> str:
        end = pos
        while end < length and (source[end].isalnum() or source[end] in "_$"):
            end += 1
        return source[pos:end]

    while i < length:
        c = source[i]

        if c == "\n":
            line += 1
            i += 1
            continue
        if c in " \t\r":
            i += 1
            continue

        # Comments
        if source.startswith("//", i):
            i = source.find("\n", i)
            if i == -1:
                break
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            if end == -1:
                problems.append(JsProblem(line, "unterminated block comment",
                                          "/* was never closed"))
                break
            line += source.count("\n", i, end)
            i = end + 2
            continue

        # Quoted strings. A raw newline inside one is the exact defect that
        # broke production, so it is reported specifically.
        if c in ("'", '"'):
            start_line = line
            i += 1
            closed = False
            while i < length:
                ch = source[i]
                if ch == "\\":
                    if i + 1 < length and source[i + 1] == "\n":
                        line += 1
                    i += 2
                    continue
                if ch == "\n":
                    problems.append(JsProblem(
                        start_line, "unterminated string literal",
                        f"a {c} string contains a real newline; use a \\n "
                        f"escape or concatenate two strings",
                    ))
                    line += 1
                    i += 1
                    closed = True  # reported; resume scanning after the line
                    break
                if ch == c:
                    closed = True
                    i += 1
                    break
                i += 1
            if not closed:
                problems.append(JsProblem(start_line, "unterminated string literal",
                                          f"a {c} string reached end of file"))
            prev_char, prev_word = c, ""
            continue

        # Template literals, which may legitimately span lines.
        if c == "`":
            start_line = line
            i += 1
            closed = False
            while i < length:
                ch = source[i]
                if ch == "\\":
                    i += 2
                    continue
                if ch == "\n":
                    line += 1
                    i += 1
                    continue
                if source.startswith("${", i):
                    brackets.append(("${", line))
                    i += 2
                    closed = True   # handed off to the interpolation
                    break
                if ch == "`":
                    closed = True
                    i += 1
                    break
                i += 1
            if not closed:
                problems.append(JsProblem(start_line, "unterminated template literal",
                                          "a ` template reached end of file"))
            prev_char, prev_word = "`", ""
            continue

        # Regular expressions, distinguished from division by what precedes.
        if c == "/":
            starts_regex = (
                prev_char == ""
                or prev_char in _REGEX_PRECEDING_PUNCTUATION
                or prev_word in _REGEX_PRECEDING_KEYWORDS
            )
            if starts_regex:
                start_line = line
                i += 1
                in_class = False
                closed = False
                while i < length:
                    ch = source[i]
                    if ch == "\\":
                        i += 2
                        continue
                    if ch == "\n":
                        problems.append(JsProblem(
                            start_line, "unterminated regular expression",
                            "a / regex contains a real newline",
                        ))
                        line += 1
                        i += 1
                        closed = True
                        break
                    if ch == "[":
                        in_class = True
                    elif ch == "]":
                        in_class = False
                    elif ch == "/" and not in_class:
                        closed = True
                        i += 1
                        break
                    i += 1
                if not closed:
                    problems.append(JsProblem(start_line, "unterminated regular expression",
                                              "a / regex reached end of file"))
                prev_char, prev_word = "/", ""
                continue

        # Brackets
        if c in "([{":
            brackets.append((c, line))
            prev_char, prev_word = c, ""
            i += 1
            continue
        if c in ")]}":
            expected = {")": "(", "]": "[", "}": "{"}[c]
            if not brackets:
                problems.append(JsProblem(line, "unbalanced bracket",
                                          f"{c} with nothing open"))
            else:
                opener, opened_at = brackets[-1]
                if opener == "${" and c == "}":
                    # Closing an interpolation resumes the template literal
                    # that opened it. The resume must consume a following
                    # "${" itself and re-push the context: leaving the "$" for
                    # the main loop made it a bare identifier, which dropped
                    # the scanner out of template mode entirely -- after which
                    # a "</div>" inside the template read as a regex and
                    # produced a cascade of false positives.
                    brackets.pop()
                    i += 1
                    new_i, detail, another = _resume_template(source, i)
                    line += source.count("\n", i, new_i)
                    i = new_i
                    if detail:
                        problems.append(JsProblem(opened_at,
                                                  "unterminated template literal",
                                                  detail))
                    if another:
                        brackets.append(("${", line))
                    prev_char, prev_word = "`", ""
                    continue
                if opener != expected:
                    problems.append(JsProblem(
                        line, "mismatched bracket",
                        f"{c} closes {opener!r} opened on line {opened_at}",
                    ))
                brackets.pop()
            prev_char, prev_word = c, ""
            i += 1
            continue

        if c.isalnum() or c in "_$":
            word = rest_of_word(i)
            i += len(word)
            prev_char, prev_word = word[-1], word
            continue

        prev_char, prev_word = c, ""
        i += 1

    for opener, opened_at in brackets:
        problems.append(JsProblem(
            opened_at,
            "unterminated template literal" if opener == "${" else "unclosed bracket",
            f"{opener} was never closed",
        ))

    return problems


def _resume_template(source: str, i: int):
    """
    Continue a template literal after its ``${...}`` interpolation closes.

    Returns ``(new index, error detail or "", another_interpolation)``. A
    following ``${`` is consumed here and reported through the third value, so
    the caller re-pushes the context; handing the ``$`` back to the main loop
    would tokenise it as an identifier and lose track of the template.
    """
    length = len(source)
    while i < length:
        ch = source[i]
        if ch == "\\":
            i += 2
            continue
        if source.startswith("${", i):
            return i + 2, "", True
        if ch == "`":
            return i + 1, "", False
        i += 1
    return i, "a ` template reached end of file", False
