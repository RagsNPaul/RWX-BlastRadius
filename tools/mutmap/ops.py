"""Up to three single-token mutations of one source line, in a fixed priority."""
from __future__ import annotations

import io
import tokenize


def mutations(source_line: str) -> list[tuple[str, str]]:
    """Return at most three (op, new_line) pairs. Each op is applied to the original line."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, edit in _OPS:
        new_line = edit(source_line)
        if new_line is None or new_line == source_line or new_line in seen:
            continue
        seen.add(new_line)
        found.append((name, new_line))
        if len(found) == 3:
            break
    return found


def _tokens(line: str) -> list[tokenize.TokenInfo] | None:
    try:
        return list(tokenize.generate_tokens(io.StringIO(line).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None


def _replace_span(line: str, start: int, end: int, text: str) -> str:
    return line[:start] + text + line[end:]


def _swap_and_or(line: str) -> str | None:
    tokens = _tokens(line)
    if not tokens:
        return None
    for tok in tokens:
        if tok.type == tokenize.NAME and tok.string in {"and", "or"}:
            flipped = "or" if tok.string == "and" else "and"
            return _replace_span(line, tok.start[1], tok.end[1], flipped)
    return None


def _swap_eq(line: str) -> str | None:
    tokens = _tokens(line)
    if not tokens:
        return None
    for tok in tokens:
        if tok.type == tokenize.OP and tok.string in {"==", "!="}:
            flipped = "!=" if tok.string == "==" else "=="
            return _replace_span(line, tok.start[1], tok.end[1], flipped)
    return None


def _swap_cmp(line: str) -> str | None:
    tokens = _tokens(line)
    if not tokens:
        return None
    flips = {"<": "<=", "<=": "<", ">": ">=", ">=": ">"}
    for tok in tokens:
        if tok.type == tokenize.OP and tok.string in flips:
            return _replace_span(line, tok.start[1], tok.end[1], flips[tok.string])
    return None


def _swap_is(line: str) -> str | None:
    """Flip `is` and `is not`. Never rewrites `is` as `==`."""
    tokens = _tokens(line)
    if not tokens:
        return None
    significant = [tok for tok in tokens if tok.type not in {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.ENCODING, tokenize.ENDMARKER}]
    for index, tok in enumerate(significant):
        if tok.type != tokenize.NAME or tok.string != "is":
            continue
        nxt = significant[index + 1] if index + 1 < len(significant) else None
        if nxt is not None and nxt.type == tokenize.NAME and nxt.string == "not":
            gap_start = tok.end[1]
            return _replace_span(line, gap_start, nxt.end[1], "")
        return _replace_span(line, tok.start[1], tok.end[1], "is not")
    return None


def _swap_bool(line: str) -> str | None:
    tokens = _tokens(line)
    if not tokens:
        return None
    for tok in tokens:
        if tok.type == tokenize.NAME and tok.string in {"True", "False"}:
            flipped = "False" if tok.string == "True" else "True"
            return _replace_span(line, tok.start[1], tok.end[1], flipped)
    return None


def _drop_not(line: str) -> str | None:
    tokens = _tokens(line)
    if not tokens:
        return None
    for tok in tokens:
        if tok.type == tokenize.NAME and tok.string == "not":
            end = tok.end[1]
            if end < len(line) and line[end] == " ":
                end += 1
            return _replace_span(line, tok.start[1], end, "")
    return None


def _bump_int(line: str) -> str | None:
    tokens = _tokens(line)
    if not tokens:
        return None
    for index, tok in enumerate(tokens):
        if tok.type != tokenize.NUMBER or not tok.string.isdigit():
            continue
        value = int(tok.string)
        sign = -1 if index > 0 and tokens[index - 1].type == tokenize.OP and tokens[index - 1].string == "-" else 1
        updated = sign * value + 1
        if sign < 0:
            if updated < 0:
                return _replace_span(line, tok.start[1], tok.end[1], str(abs(updated)))
            # -1 → 0 drops the minus. 0 is written over the minus and the digits.
            return _replace_span(line, tokens[index - 1].start[1], tok.end[1], str(updated))
        return _replace_span(line, tok.start[1], tok.end[1], str(updated))
    return None


def _bump_seq_get(line: str) -> str | None:
    tokens = _tokens(line)
    if not tokens:
        return None
    significant = [tok for tok in tokens if tok.type not in {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.ENCODING, tokenize.ENDMARKER}]
    for index in range(len(significant) - 5):
        name, lpar, args, comma, num, rpar = significant[index : index + 6]
        if not (
            name.type == tokenize.NAME
            and name.string == "seq_get"
            and lpar.string == "("
            and args.type == tokenize.NAME
            and args.string == "args"
            and comma.string == ","
            and num.type == tokenize.NUMBER
            and num.string.isdigit()
            and rpar.string == ")"
        ):
            continue
        return _replace_span(line, num.start[1], num.end[1], str(int(num.string) + 1))
    return None


def _append_x(line: str) -> str | None:
    tokens = _tokens(line)
    if not tokens:
        return None
    for tok in tokens:
        if tok.type != tokenize.STRING:
            continue
        raw = line[tok.start[1] : tok.end[1]]
        prefix = raw
        for quote in ('"""', "'''", '"', "'"):
            if quote in raw:
                body_at = raw.find(quote)
                prefix = raw[:body_at]
                closer = quote
                break
        else:
            continue
        if "f" in prefix.lower():
            continue
        insert_at = tok.end[1] - len(closer)
        return _replace_span(line, insert_at, insert_at, "X")
    return None


def _swap_copy(line: str) -> str | None:
    tokens = _tokens(line)
    if not tokens:
        return None
    significant = [tok for tok in tokens if tok.type not in {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.ENCODING, tokenize.ENDMARKER}]
    for index in range(len(significant) - 2):
        name, eq, val = significant[index : index + 3]
        if name.type == tokenize.NAME and name.string == "copy" and eq.string == "=" and val.type == tokenize.NAME and val.string in {"True", "False"}:
            flipped = "False" if val.string == "True" else "True"
            return _replace_span(line, val.start[1], val.end[1], flipped)
    return None


def _return_none(line: str) -> str | None:
    tokens = _tokens(line)
    if not tokens:
        return None
    for index, tok in enumerate(tokens):
        if tok.type != tokenize.NAME or tok.string != "return":
            continue
        rest = tokens[index + 1 :]
        value = [item for item in rest if item.type not in {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.ENCODING, tokenize.ENDMARKER}]
        if not value:
            return None
        if len(value) == 1 and value[0].type == tokenize.NAME and value[0].string == "None":
            return None
        comment = next((item for item in rest if item.type == tokenize.COMMENT), None)
        end = comment.start[1] if comment else len(line.rstrip("\n"))
        newline = "\n" if line.endswith("\n") else ""
        tail = line[end:] if comment else newline
        return line[: tok.end[1]] + " None" + ((" " + tail) if comment else tail)
    return None


_OPS = (
    ("and_or", _swap_and_or),
    ("eq", _swap_eq),
    ("cmp", _swap_cmp),
    ("is", _swap_is),
    ("bool", _swap_bool),
    ("not", _drop_not),
    ("int", _bump_int),
    ("seq_get", _bump_seq_get),
    ("str", _append_x),
    ("copy", _swap_copy),
    ("return", _return_none),
)
