"""RAPID for Hefesto: syntax check, risk review of a change, taught points, history.

Pure functions (no controller access) so they can be tested and reused by the
`/rapid`, `/ponto` commands and the review the AI gets.
"""

from __future__ import annotations

import difflib
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# ----------------------------------------------------------------------- syntax

BLOCKS = {
    "MODULE": "ENDMODULE",
    "PROC": "ENDPROC",
    "FUNC": "ENDFUNC",
    "TRAP": "ENDTRAP",
    "IF": "ENDIF",
    "FOR": "ENDFOR",
    "WHILE": "ENDWHILE",
    "TEST": "ENDTEST",
    "RECORD": "ENDRECORD",
}
CLOSERS = {end: start for start, end in BLOCKS.items()}


def _code(line: str) -> str:
    """The line without comments and string contents (for structure checks)."""
    out, in_string = [], False
    for char in line:
        if char == '"':
            in_string = not in_string
            out.append(char)
        elif in_string:
            out.append(" ")
        elif char == "!":
            break
        else:
            out.append(char)
    return "".join(out)


def check_syntax(source: str) -> list[str]:
    """Structural problems a `prog load --replace` would reject (it deletes the
    original module first, so catching them here matters). Not a full parser:
    the controller (or the virtual twin) has the final say."""
    problems: list[str] = []
    stack: list[tuple[str, int]] = []
    brackets: list[tuple[str, int]] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    for number, raw in enumerate(source.splitlines(), 1):
        if raw.count('"') % 2:
            problems.append(f"linha {number}: aspas sem fechar")
            continue
        code = _code(raw)
        words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", code)
        upper = [word.upper() for word in words]
        if upper:
            first = upper[0]
            opener = first
            if first in ("LOCAL", "GLOBAL", "SYSMODULE") and len(upper) > 1:
                opener = upper[1]
            if opener in ("PROC", "FUNC", "TRAP", "MODULE", "RECORD", "FOR", "WHILE", "TEST"):
                stack.append((opener, number))
            elif first == "IF" and upper[-1] == "THEN":
                stack.append(("IF", number))
            elif first in CLOSERS:
                expected = CLOSERS[first]
                if any(word == expected for word, _ in stack):
                    # Blocks left open inside this one are the real mistake.
                    while stack[-1][0] != expected:
                        word, line = stack.pop()
                        problems.append(f"linha {line}: {word} sem {BLOCKS[word]}")
                    stack.pop()
                else:
                    problems.append(f"linha {number}: {first} sem {expected}")
        for char in code:
            if char in "([{":
                brackets.append((char, number))
            elif char in ")]}":
                if not brackets or brackets[-1][0] != pairs[char]:
                    problems.append(f"linha {number}: '{char}' sem abertura")
                else:
                    brackets.pop()
    for word, number in stack:
        problems.append(f"linha {number}: {word} sem {BLOCKS[word]}")
    for char, number in brackets[:3]:
        problems.append(f"linha {number}: '{char}' sem fechar")
    return problems


def module_name(source: str) -> str | None:
    match = re.search(r"^\s*MODULE\s+([A-Za-z_][A-Za-z0-9_]*)", source, re.M | re.I)
    return match.group(1) if match else None


# ------------------------------------------------------------------------ risks


@dataclass(frozen=True)
class Risk:
    line: int
    """Line in the new version (0 when the line was removed)."""
    level: str
    """"alto" (moves the robot differently) or "medio"."""
    text: str


_SPEED = re.compile(r"\bv(?:max|\d+)\b|\bv_[A-Za-z0-9_]+\b", re.I)
_ZONE = re.compile(r"\b(?:fine|z\d+)\b", re.I)
_MOVE = re.compile(r"^\s*(Move\w+|SearchL|SearchJ|TriggL|TriggJ|ArcL|ArcC)\b", re.I)
_DATA = re.compile(
    r"^\s*(?:LOCAL\s+|TASK\s+)?(CONST|PERS|VAR)\s+(robtarget|jointtarget|tooldata|wobjdata|loaddata|speeddata|zonedata)\s+(\w+)\s*:=\s*(.*?);",
    re.I,
)
_SETTINGS = re.compile(
    r"^\s*(AccSet|VelSet|ConfL|ConfJ|SingArea|PDispSet|PDispOn|EOffsSet|SpeedRefresh|GripLoad|MotionSup|WorldAccLim|PathAccLim)\b",
    re.I,
)
_IO = re.compile(r"^\s*(SetDO|SetGO|SetAO|PulseDO|WaitDI|WaitDO|WaitUntil|WaitTime)\b", re.I)
_NUMBERS = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


CIRCULAR = {"MOVEC", "MOVECDO", "MOVECAO", "MOVECGO", "MOVECSYNC", "ARCC", "TRIGGC"}


def _split_top(text: str) -> list[str]:
    """Split on commas that aren't inside brackets or parentheses."""
    parts, depth, current = [], 0, []
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current).strip())
    return parts


def _arguments(line: str) -> tuple[str, str, str]:
    """(speed, zone, tool + wobj) of a move instruction, read by position:
    MoveL ToPoint, Speed, Zone, Tool [\\WObj] (MoveC has CirPoint first)."""
    code = _code(line).strip().rstrip(";")
    name, _, rest = code.partition(" ")
    # Optional arguments (\Conc, \V:=, \Z:=, \WObj:=...) sit between the positional ones.
    pieces = re.split(r"(\\\w+(?:\s*:=\s*[^,\\]+)?)", rest)
    optional = [piece.strip() for piece in pieces if piece.strip().startswith("\\")]
    positional = [part for part in _split_top("".join(p for p in pieces if not p.strip().startswith("\\"))) if part]
    shift = 1 if name.upper() in CIRCULAR else 0
    speed = positional[1 + shift] if len(positional) > 1 + shift else ""
    zone = positional[2 + shift] if len(positional) > 2 + shift else ""
    tool = positional[3 + shift] if len(positional) > 3 + shift else ""
    wobj = next((p.split(":=")[1].strip() for p in optional if p.lower().startswith("\\wobj")), "")
    speed += "".join(f" {p}" for p in optional if re.match(r"\\[VT]\s*:=", p, re.I))
    zone += "".join(f" {p}" for p in optional if re.match(r"\\Z\s*:=", p, re.I))
    return speed, zone, tool + (f" \\WObj:={wobj}" if wobj else "")


def _position(value: str) -> tuple[float, float, float] | None:
    numbers = [float(n) for n in _NUMBERS.findall(value)[:3]]
    return tuple(numbers) if len(numbers) == 3 else None  # type: ignore[return-value]


def review_change(old: str, new: str) -> list[Risk]:
    """What in this change moves the robot differently: speeds, zones, targets,
    tools and work objects, loads, motion settings, I/O and waits."""
    risks: list[Risk] = []
    old_lines, new_lines = old.splitlines(), new.splitlines()
    old_data = {m.group(3).lower(): (m.group(2), m.group(4)) for m in map(_DATA.match, old_lines) if m}
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed, added = old_lines[i1:i2], new_lines[j1:j2]
        for offset, line in enumerate(added):
            number = j1 + offset + 1
            before = removed[offset] if offset < len(removed) else ""
            if data := _DATA.match(line):
                kind, name, value = data.group(2), data.group(3), data.group(4)
                previous = old_data.get(name.lower())
                if previous is None:
                    risks.append(Risk(number, "medio", f"novo {kind} {name}"))
                elif previous[1].replace(" ", "") != value.replace(" ", ""):
                    detail = ""
                    if kind.lower() == "robtarget":
                        a, b = _position(previous[1]), _position(value)
                        if a and b:
                            detail = f" (desloca {math.dist(a, b):.1f} mm)"
                    risks.append(Risk(number, "alto", f"{kind} {name} alterado{detail}"))
                continue
            if _MOVE.match(line):
                if not before or not _MOVE.match(before):
                    risks.append(Risk(number, "alto", f"movimento novo: {line.strip()}"))
                    continue
                (s0, z0, f0), (s1, z1, f1) = _arguments(before), _arguments(line)
                if s0 != s1:
                    risks.append(Risk(number, "alto", f"velocidade {s0 or '?'} → {s1 or '?'}"))
                if z0 != z1:
                    risks.append(Risk(number, "alto", f"zona {z0 or '?'} → {z1 or '?'}"))
                if f0 != f1:
                    risks.append(Risk(number, "alto", f"ferramenta/obra {f0 or '-'} → {f1 or '-'}"))
                if (s0, z0, f0) == (s1, z1, f1) and _code(before).strip() != _code(line).strip():
                    risks.append(Risk(number, "alto", f"movimento alterado: {line.strip()}"))
            elif _SETTINGS.match(line):
                risks.append(Risk(number, "alto", f"ajuste de movimento: {line.strip()}"))
            elif _IO.match(line) and _code(before).strip() != _code(line).strip():
                risks.append(Risk(number, "medio", f"I/O ou espera: {line.strip()}"))
        for line in removed[len(added):]:
            if _MOVE.match(line) or _SETTINGS.match(line):
                risks.append(Risk(0, "alto", f"removido: {line.strip()}"))
            elif _IO.match(line):
                risks.append(Risk(0, "medio", f"removido: {line.strip()}"))
    return risks


def unified_diff(old: str, new: str, name: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            f"{name} (controlador)",
            f"{name} (editado)",
        )
    )


def review_markdown(name: str, old: str, new: str, title: str = "Revisão") -> str:
    """Diff, syntax problems and risks, as shown to the human and to the AI."""
    lines = [f"## {title}: {name}", ""]
    problems = check_syntax(new)
    if problems:
        lines += ["**Erros de estrutura (não carregue assim):**", *[f"- {p}" for p in problems], ""]
    risks = review_change(old, new)
    if risks:
        lines.append("**O que muda o movimento ou a segurança:**")
        for risk in risks:
            where = f"linha {risk.line}" if risk.line else "removido"
            mark = "🔴" if risk.level == "alto" else "🟡"
            lines.append(f"- {mark} {where}: {risk.text}")
        lines.append("")
    elif not problems:
        lines += ["Nenhuma mudança de movimento, velocidade, zona, alvo, ferramenta ou I/O.", ""]
    diff = unified_diff(old, new, name)
    lines += ["```diff", diff.rstrip() or "(sem diferenças)", "```"]
    return "\n".join(lines)


# ----------------------------------------------------------------------- points


def robtarget_declaration(name: str, robtarget: str, tool: str, wobj: str) -> str:
    """`CONST robtarget pNome:=[...];` with a comment saying how it was taught."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", name):
        raise ValueError("nome RAPID inválido (letra, depois letras, números ou _; até 32)")
    value = re.sub(r"\s+", "", robtarget)
    return f"    CONST robtarget {name}:={value}; ! gravado pelo Hefesto (tool {tool}, wobj {wobj})"


def insert_declaration(source: str, declaration: str, name: str) -> str:
    """Put a declaration after the module's last data declaration (or replace the
    existing one with the same name)."""
    lines = source.splitlines()
    pattern = re.compile(rf"^\s*(?:LOCAL\s+|TASK\s+)?(CONST|PERS|VAR)\s+robtarget\s+{re.escape(name)}\b", re.I)
    for index, line in enumerate(lines):
        if pattern.match(line):
            lines[index] = declaration
            return "\n".join(lines) + "\n"
    insert_at = None
    for index, line in enumerate(lines):
        stripped = line.strip().upper()
        if re.match(r"^(LOCAL\s+|TASK\s+)?(CONST|PERS|VAR)\s", stripped):
            insert_at = index + 1
        elif re.match(r"^(LOCAL\s+)?(PROC|FUNC|TRAP)\s", stripped):
            if insert_at is None:
                insert_at = index
            break
    if insert_at is None:
        insert_at = 1 if lines and lines[0].strip().upper().startswith("MODULE") else 0
    lines.insert(insert_at, declaration)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- history


class History:
    """A private git repository per controller (never pushed anywhere)."""

    def __init__(self, folder: Path):
        self.folder = folder

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.folder), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} falhou")
        return result.stdout

    def ensure(self) -> None:
        if not (self.folder / ".git").exists():
            self.folder.mkdir(parents=True, exist_ok=True)
            self._git("init", "-q")
            self._git("config", "user.name", "Hefesto")
            self._git("config", "user.email", "hefesto@localhost")
            (self.folder / "LEIA.txt").write_text(
                "Histórico local dos módulos RAPID deste controlador (Hefesto).\n"
                "Dados da fábrica: não envie este repositório para nenhum lugar.\n",
                encoding="utf-8",
            )
            self._git("add", "LEIA.txt")
            self._git("commit", "-q", "-m", "Início do histórico")

    def save(self, module: str, source: str, message: str) -> str | None:
        """Commit `module` if it changed; returns the short commit id (or None)."""
        self.ensure()
        path = self.folder / f"{module}.mod"
        path.write_text(source, encoding="utf-8", newline="\n")
        self._git("add", path.name)
        if not self._git("status", "--porcelain", path.name).strip():
            return None
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "--short", "HEAD").strip()

    def log(self, module: str, count: int = 15) -> list[tuple[str, str, str]]:
        """[(id, date, message)] newest first."""
        if not (self.folder / ".git").exists():
            return []
        out = self._git("log", f"-{count}", "--format=%h\t%ad\t%s", "--date=format:%d/%m %H:%M", "--", f"{module}.mod")
        return [tuple(line.split("\t", 2)) for line in out.splitlines() if line.count("\t") == 2]  # type: ignore[misc]

    def show(self, module: str, commit: str) -> str:
        return self._git("show", f"{commit}:{module}.mod")


# -------------------------------------------------------------------- templates

TEMPLATES_DIR = Path(__file__).parent / "data" / "rapid"


def templates() -> dict[str, Path]:
    return {path.stem: path for path in sorted(TEMPLATES_DIR.glob("*.mod"))}


# ----------------------------------------------------------------- highlighting


def register_lexer() -> None:
    """Teach Textual's highlighter the "rapid" language (and .mod/.sys files)."""
    import textual.highlight as highlight

    if getattr(highlight, "_hefesto_rapid", False):
        return
    lexer_class = rapid_lexer()
    original_by_name = highlight.get_lexer_by_name
    original_for_file = highlight.guess_lexer_for_filename

    def get_lexer_by_name(name, **options):
        if str(name).lower() in ("rapid", "mod", "sys", "prg"):
            return lexer_class(**options)
        return original_by_name(name, **options)

    def guess_lexer_for_filename(filename, code, **options):
        if str(filename).lower().endswith((".mod", ".sys", ".prg")):
            return lexer_class(**options)
        return original_for_file(filename, code, **options)

    highlight.get_lexer_by_name = get_lexer_by_name
    highlight.guess_lexer_for_filename = guess_lexer_for_filename
    highlight._hefesto_rapid = True


def rapid_lexer():
    """A Pygments lexer for RAPID (Pygments has none), for diffs and code views."""
    from pygments.lexer import RegexLexer, words
    from pygments.token import Comment, Keyword, Name, Number, Operator, Punctuation, String, Text

    class RapidLexer(RegexLexer):
        name = "RAPID"
        aliases = ["rapid"]
        filenames = ["*.mod", "*.sys", "*.prg"]
        flags = re.IGNORECASE | re.MULTILINE
        tokens = {
            "root": [
                (r"!.*$", Comment.Single),
                (r'"(\\\\|\\"|[^"])*"', String),
                (words((*BLOCKS, *CLOSERS, "LOCAL", "TASK", "CONST", "PERS", "VAR", "THEN", "ELSE",
                        "ELSEIF", "DO", "FROM", "TO", "STEP", "CASE", "DEFAULT", "RETURN", "ERROR",
                        "BACKWARD", "UNDO", "RETRY", "TRYNEXT", "RAISE", "EXIT", "STOP", "GOTO", "AND",
                        "OR", "NOT", "XOR", "DIV", "MOD", "TRUE", "FALSE", "CONNECT", "WITH", "SYSMODULE",
                        "NOSTEPIN", "VIEWONLY", "READONLY", "NOVIEW"), suffix=r"\b"), Keyword),
                (words(("robtarget", "jointtarget", "tooldata", "wobjdata", "loaddata", "speeddata",
                        "zonedata", "num", "dnum", "bool", "string", "pos", "orient", "pose",
                        "confdata", "extjoint", "signaldi", "signaldo", "intnum", "clock"), suffix=r"\b"),
                 Keyword.Type),
                (r"\b(Move\w+|Search\w+|Trigg\w+|Set\w*|Reset|PulseDO|Wait\w+|TP\w+|AccSet|VelSet|"
                 r"Conf[LJ]|SingArea|Offs|RelTool|CRobT|CJointT|ISignal\w+|IDelete|IWatch|ISleep)\b",
                 Name.Builtin),
                (r"\\\w+", Name.Attribute),
                (r"-?\d+(\.\d+)?([eE][-+]?\d+)?", Number),
                (r":=|<>|<=|>=|[-+*/<>=]", Operator),
                (r"[()\[\],;:{}]", Punctuation),
                (r"\w+", Name),
                (r"\s+", Text),
            ]
        }

    return RapidLexer
