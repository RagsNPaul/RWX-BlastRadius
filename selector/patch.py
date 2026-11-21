"""A unified diff -> the files it touches, their hunks with old line numbers, and the new text."""
import ast


class FilePatch:
    """One file of a patch. `old` and `new` are repo paths; None stands for /dev/null.

    `hunks` is a list of hunks, each a list of rows (kind, old line, text). kind is " ", "-" or
    "+"; for a "+" row the old line is the one the row is inserted before.
    """

    __slots__ = ("old", "new", "hunks", "binary")

    def __init__(self, old=None, new=None):
        self.old, self.new, self.hunks, self.binary = old, new, [], False


def _unquote(name):
    if name.startswith('"'):  # git writes unusual names as a C string of UTF-8 bytes
        name = ast.literal_eval(name).encode("latin-1").decode("utf-8")
    return name


def _path(field):
    name = _unquote(field.split("\t")[0])  # plain `diff -u` appends a tab and a timestamp
    if name == "/dev/null":
        return None
    return name[2:] if name[:2] in ("a/", "b/") else name


def _range(field):  # "-12,7" -> (12, 7); the count is 1 when it is left out
    start, _, count = field[1:].partition(",")
    return int(start), int(count) if count else 1


def parse(text):
    """-> [FilePatch], in the order of the diff.

    Hunk bodies are read by the line counts of their `@@` header, never by how a line starts:
    a removed SQL comment is written `--- ...` and must not be taken for a file header.
    """
    files, current, lines = [], None, text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        i += 1
        if line.startswith("diff --git "):
            # These paths only matter when no ---/+++ or rename line follows (mode change, binary)
            old, _, new = line[11:].partition(" b/")
            current = FilePatch(_path(old) if new else None, new or None)
            files.append(current)
        elif line.startswith("--- ") and i < n and lines[i].startswith("+++ "):
            if current is None or current.hunks:  # a diff without `diff --git` lines
                current = FilePatch()
                files.append(current)
            current.old, current.new = _path(line[4:]), _path(lines[i][4:])
            i += 1
        elif current is None:
            continue  # text before the first file
        elif line.startswith("rename from "):
            current.old = _unquote(line[12:])
        elif line.startswith("rename to "):
            current.new = _unquote(line[10:])
        elif line.startswith("new file mode"):
            current.old = None
        elif line.startswith("deleted file mode"):
            current.new = None
        elif line.startswith(("Binary files ", "GIT binary patch")):
            current.binary = True
        elif line.startswith("@@ "):
            fields = line.split(" ")
            old, old_left = _range(fields[1])
            new_left = _range(fields[2])[1]
            if not old_left:
                old += 1  # "-5,0" removes nothing: the rows go in after line 5
            rows = []
            while old_left > 0 or new_left > 0:
                if i >= n:
                    raise ValueError("the last hunk is cut short")
                row = lines[i]
                i += 1
                kind = row[:1] or " "  # some tools strip the blank of an empty context line
                if kind == "\\":
                    continue  # "\ No newline at end of file"
                if kind == " ":
                    old_left -= 1
                    new_left -= 1
                elif kind == "-":
                    old_left -= 1
                elif kind == "+":
                    new_left -= 1
                else:
                    raise ValueError(f"patch line {i} is not a hunk row")
                rows.append((kind, old, row[1:]))
                if kind != "+":
                    old += 1
            if old_left or new_left:
                raise ValueError(f"the hunk ending on patch line {i} does not match its header")
            current.hunks.append(rows)
    return files


def apply(text, hunks):
    """The file's text after the hunks. Raises ValueError when a hunk does not fit `text`.

    Whether the last line ends with a newline is kept from the old text; Python parses both alike.
    """
    old, new, position = text.split("\n"), [], 1
    for rows in hunks:
        if not rows:
            continue
        first = rows[0][1]
        if first < position:
            raise ValueError(f"hunks overlap at line {first}")
        new += old[position - 1 : first - 1]
        position = first
        for kind, number, row in rows:
            if kind == "+":
                new.append(row)
                continue
            if number > len(old) or old[number - 1] != row:
                raise ValueError(f"line {number} is not what the patch expects")
            if kind == " ":
                new.append(row)
            position = number + 1
    new += old[position - 1 :]
    return "\n".join(new)
