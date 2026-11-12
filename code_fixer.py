#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代码扫描修复小工具：扫描 Python 代码中的常见问题，并可安全自动修复其中一部分。"""

from __future__ import annotations

import argparse
import ast
import difflib
import io
import json
import os
import sys
import tokenize
from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional

VERSION = "1.0.0"

SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
    ".idea",
    ".vscode",
}

# ---------------------------------------------------------------------------
# 控制台编码（Windows 下正确打印中文）
# ---------------------------------------------------------------------------


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Issue:
    file: str
    line: int
    column: int
    code: str
    message: str
    fixable: bool = False
    fixed: bool = False


@dataclass
class FileResult:
    path: str
    issues: list[Issue] = field(default_factory=list)
    error: Optional[str] = None
    fixed_text: Optional[str] = None
    wrote: bool = False
    backup: Optional[str] = None


# ---------------------------------------------------------------------------
# 文件读写与换行处理
# ---------------------------------------------------------------------------


def detect_encoding(raw: bytes) -> tuple[str, bytes]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", raw[3:]
    for enc in ("utf-8", "gbk", "gb18030", "latin-1"):
        try:
            raw.decode(enc)
            return enc, raw
        except UnicodeDecodeError:
            continue
    return "utf-8", raw


def read_file(path: str) -> tuple[str, str]:
    with open(path, "rb") as f:
        raw = f.read()
    enc, data = detect_encoding(raw)
    return data.decode(enc), enc


def write_file(path: str, text: str, encoding: str) -> None:
    with open(path, "w", encoding=encoding, newline="") as f:
        f.write(text)


def split_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    if line.endswith("\r"):
        return line[:-1], "\r"
    return line, ""


def iter_lines(text: str) -> list[tuple[str, str]]:
    if not text:
        return []
    parts: list[tuple[str, str]] = []
    buf: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\r":
            body = "".join(buf)
            buf = []
            if i + 1 < n and text[i + 1] == "\n":
                parts.append((body, "\r\n"))
                i += 2
                continue
            parts.append((body, "\r"))
            i += 1
            continue
        if ch == "\n":
            parts.append(("".join(buf), "\n"))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        parts.append(("".join(buf), ""))
    elif text.endswith("\n") or text.endswith("\r"):
        # 文本以换行结束即可，不再追加空行
        pass
    return parts


def join_lines(parts: Iterable[tuple[str, str]]) -> str:
    return "".join(body + ending for body, ending in parts)


def predominant_newline(text: str) -> str:
    crlf = text.count("\r\n")
    # 单独的 \n（不含 \r\n 中的那个）
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    best = max(crlf, lf, cr)
    if best == 0:
        return "\n"
    if crlf == best:
        return "\r\n"
    if lf == best:
        return "\n"
    return "\r"


def line_start_offsets(text: str) -> list[int]:
    """offsets[lineno] 为该行（1-based）在 text 中的起始下标。"""
    starts = [0, 0]
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\r":
            i += 2 if i + 1 < n and text[i + 1] == "\n" else 1
            starts.append(i)
            continue
        if ch == "\n":
            i += 1
            starts.append(i)
            continue
        i += 1
    return starts


def pos_to_index(starts: list[int], row: int, col: int) -> int:
    if row < 1 or row >= len(starts):
        # tokenize 有时会给出最后一行之后的位置
        return starts[-1] if starts else 0
    return starts[row] + col


# ---------------------------------------------------------------------------
# 收集 Python 文件
# ---------------------------------------------------------------------------


def collect_py_files(paths: list[str]) -> tuple[list[str], list[str]]:
    files: list[str] = []
    errors: list[str] = []
    for raw in paths:
        path = os.path.abspath(raw)
        if not os.path.exists(path):
            errors.append(f"路径不存在：{raw}")
            continue
        if os.path.isfile(path):
            if path.endswith(".py"):
                files.append(path)
            else:
                errors.append(f"不是 Python 文件，已跳过：{raw}")
            continue
        if os.path.isdir(path):
            for root, dirs, names in os.walk(path):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
                for name in names:
                    if name.endswith(".py"):
                        files.append(os.path.join(root, name))
            continue
        errors.append(f"无法处理的路径：{raw}")
    # 去重并保持稳定顺序
    seen: set[str] = set()
    unique: list[str] = []
    for f in files:
        key = os.path.normcase(os.path.normpath(f))
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique, errors


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------


def scan_physical(path: str, text: str, max_line: int, check_print: bool) -> list[Issue]:
    issues: list[Issue] = []
    parts = iter_lines(text)
    has_crlf = "\r\n" in text
    # 是否存在“单纯 LF”（不是 CRLF 的一部分）
    stripped = text.replace("\r\n", "")
    has_lf = "\n" in stripped
    has_cr = "\r" in stripped
    if sum(bool(x) for x in (has_crlf, has_lf, has_cr)) >= 2:
        issues.append(
            Issue(path, 1, 0, "mixed-newlines", "文件同时包含 CRLF 与 LF（或 CR）换行符", False)
        )

    used_tab_indent = False
    used_space_indent = False
    for lineno, (body, ending) in enumerate(parts, start=1):
        # 行尾空白（不含换行本身）
        if body.endswith(" ") or body.endswith("\t"):
            col = len(body.rstrip(" \t"))
            issues.append(
                Issue(path, lineno, col, "trailing-whitespace", "行尾存在多余空白字符", True)
            )
        # 过长行
        if max_line > 0 and len(body) > max_line:
            issues.append(
                Issue(
                    path,
                    lineno,
                    max_line,
                    "line-too-long",
                    f"行长度为 {len(body)}，超过限制 {max_line}",
                    False,
                )
            )
        # 缩进
        i = 0
        while i < len(body) and body[i] in " \t":
            i += 1
        if i > 0 and body[i:].strip() and not body.lstrip().startswith("#"):
            indent = body[:i]
            if "\t" in indent and " " in indent:
                issues.append(
                    Issue(path, lineno, 0, "mixed-indent-line", "同一行的缩进同时包含 Tab 和空格", True)
                )
            if "\t" in indent:
                used_tab_indent = True
            if " " in indent:
                used_space_indent = True

    if used_tab_indent and used_space_indent:
        issues.append(
            Issue(path, 1, 0, "mixed-indent-file", "文件中同时使用了 Tab 和空格缩进", True)
        )
    elif used_tab_indent:
        issues.append(
            Issue(path, 1, 0, "tab-indent", "文件使用 Tab 缩进，建议改为 4 个空格", True)
        )

    if text and not text.endswith(("\n", "\r")):
        last = len(parts)
        issues.append(
            Issue(path, last, len(parts[-1][0]) if parts else 0, "missing-final-newline", "文件末尾缺少换行符", True)
        )

    return issues


def scan_syntax(path: str, text: str) -> tuple[Optional[ast.AST], list[Issue]]:
    try:
        tree = ast.parse(text, filename=path)
        return tree, []
    except SyntaxError as exc:
        line = exc.lineno or 1
        col = exc.offset or 0
        msg = exc.msg or str(exc)
        return None, [
            Issue(path, line, col, "syntax-error", f"语法错误：{msg}", False)
        ]


def _annotation_names(node: Optional[ast.AST]) -> set[str]:
    names: set[str] = set()
    if node is None:
        return names
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            sub = ast.parse(node.value, mode="eval")
        except SyntaxError:
            return names
        for child in ast.walk(sub):
            if isinstance(child, ast.Name):
                names.add(child.id)
        return names
    return names


class UsageVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.used: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        return

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        return

    def visit_Name(self, node: ast.Name) -> None:
        self.used.add(node.id)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.AST) -> None:
        self.used |= _annotation_names(getattr(node, "returns", None))
        args = node.args  # type: ignore[attr-defined]
        for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
            self.used |= _annotation_names(arg.annotation)
        if args.vararg is not None:
            self.used |= _annotation_names(args.vararg.annotation)
        if args.kwarg is not None:
            self.used |= _annotation_names(args.kwarg.annotation)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.used |= _annotation_names(node.annotation)
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        self.used |= _annotation_names(node.annotation)
        self.generic_visit(node)


def _names_in_all(tree: ast.AST) -> set[str]:
    result: set[str] = set()
    for node in tree.body if isinstance(tree, ast.Module) else []:
        targets = []
        values = None
        if isinstance(node, ast.Assign):
            targets, values = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, values = [node.target], node.value
        if not targets or values is None:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "__all__":
                elts = None
                if isinstance(values, (ast.List, ast.Tuple, ast.Set)):
                    elts = values.elts
                if elts:
                    for elt in elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            result.add(elt.value)
    return result


@dataclass
class ImportRecord:
    bound: str
    node: ast.AST
    alias: ast.alias
    is_future: bool
    is_star: bool


def collect_imports(tree: ast.AST) -> list[ImportRecord]:
    records: list[ImportRecord] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                records.append(ImportRecord(bound, node, alias, False, False))
        elif isinstance(node, ast.ImportFrom):
            is_future = node.module == "__future__"
            for alias in node.names:
                if alias.name == "*":
                    records.append(ImportRecord("*", node, alias, is_future, True))
                    continue
                bound = alias.asname or alias.name
                records.append(ImportRecord(bound, node, alias, is_future, False))
    return records


def is_mutable_default(node: ast.AST) -> bool:
    if isinstance(node, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in {"list", "dict", "set"}:
            return True
    return False


def scan_ast(path: str, tree: ast.AST, check_print: bool) -> list[Issue]:
    issues: list[Issue] = []
    visitor = UsageVisitor()
    visitor.visit(tree)
    used = visitor.used | _names_in_all(tree)

    for rec in collect_imports(tree):
        if rec.is_future or rec.is_star:
            continue
        if rec.bound not in used:
            issues.append(
                Issue(
                    path,
                    rec.alias.lineno or rec.node.lineno,
                    rec.alias.col_offset or 0,
                    "unused-import",
                    f"未使用的导入：{rec.bound}",
                    True,
                )
            )

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                issues.append(
                    Issue(
                        path,
                        node.lineno,
                        node.col_offset,
                        "bare-except",
                        "使用了裸 except，建议改为 except Exception",
                        True,
                    )
                )
        if isinstance(node, ast.Compare):
            comparators = list(node.comparators)
            ops = list(node.ops)
            lefts = [node.left] + comparators[:-1]
            for left, op, right in zip(lefts, ops, comparators):
                none_left = isinstance(left, ast.Constant) and left.value is None
                none_right = isinstance(right, ast.Constant) and right.value is None
                if not (none_left or none_right):
                    continue
                if isinstance(op, ast.Eq):
                    issues.append(
                        Issue(
                            path,
                            getattr(op, "lineno", node.lineno),
                            getattr(op, "col_offset", node.col_offset),
                            "eq-none",
                            "应使用 is None，而不是 == None",
                            True,
                        )
                    )
                elif isinstance(op, ast.NotEq):
                    issues.append(
                        Issue(
                            path,
                            getattr(op, "lineno", node.lineno),
                            getattr(op, "col_offset", node.col_offset),
                            "ne-none",
                            "应使用 is not None，而不是 != None",
                            True,
                        )
                    )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            # positional defaults 对齐到 args 末尾
            defaults = args.defaults
            positional = list(args.posonlyargs) + list(args.args)
            for arg, default in zip(positional[-len(defaults) :], defaults):
                if default is not None and is_mutable_default(default):
                    issues.append(
                        Issue(
                            path,
                            default.lineno,
                            default.col_offset,
                            "mutable-default",
                            f"使用了可变对象作为默认参数：{arg.arg}",
                            False,
                        )
                    )
            for arg, default in zip(args.kwonlyargs, args.kw_defaults):
                if default is not None and is_mutable_default(default):
                    issues.append(
                        Issue(
                            path,
                            default.lineno,
                            default.col_offset,
                            "mutable-default",
                            f"使用了可变对象作为默认参数：{arg.arg}",
                            False,
                        )
                    )
        if check_print and isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "print":
                issues.append(
                    Issue(
                        path,
                        node.lineno,
                        node.col_offset,
                        "debug-print",
                        "可能是调试残留的 print 调用",
                        False,
                    )
                )
    return issues


def scan_text(path: str, text: str, max_line: int, check_print: bool) -> list[Issue]:
    issues = scan_physical(path, text, max_line, check_print)
    tree, syn = scan_syntax(path, text)
    issues.extend(syn)
    if tree is not None:
        issues.extend(scan_ast(path, tree, check_print))
    issues.sort(key=lambda it: (it.line, it.column, it.code))
    return issues


# ---------------------------------------------------------------------------
# 修复
# ---------------------------------------------------------------------------


def fix_trailing_whitespace(text: str) -> str:
    return join_lines((body.rstrip(" \t"), ending) for body, ending in iter_lines(text))


def fix_indent_tabs(text: str, tab_size: int = 4) -> str:
    out: list[tuple[str, str]] = []
    for body, ending in iter_lines(text):
        i = 0
        col = 0
        new_indent: list[str] = []
        while i < len(body) and body[i] in " \t":
            if body[i] == "\t":
                spaces = tab_size - (col % tab_size)
                new_indent.append(" " * spaces)
                col += spaces
            else:
                new_indent.append(" ")
                col += 1
            i += 1
        out.append(("".join(new_indent) + body[i:], ending))
    return join_lines(out)


def fix_final_newline(text: str) -> str:
    if not text:
        return text
    if text.endswith(("\n", "\r")):
        return text
    return text + predominant_newline(text)


def _safe_tokens(text: str) -> Optional[list[tokenize.TokenInfo]]:
    try:
        return list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None


def _apply_span_replacements(text: str, reps: list[tuple[int, int, str]]) -> str:
    """reps 为 (start, end, new) 字符下标，左闭右开。从后往前应用。"""
    if not reps:
        return text
    reps = sorted(reps, key=lambda r: (r[0], r[1]), reverse=True)
    pieces = []
    last = len(text)
    for start, end, new in reps:
        if start < 0 or end > last or start > end:
            continue
        pieces.append(text[end:last])
        pieces.append(new)
        last = start
    pieces.append(text[:last])
    return "".join(reversed(pieces))


def fix_none_comparisons(text: str) -> str:
    tokens = _safe_tokens(text)
    if not tokens:
        return text
    starts = line_start_offsets(text)
    reps: list[tuple[int, int, str]] = []

    def is_none(tok: tokenize.TokenInfo) -> bool:
        return tok.type == tokenize.NAME and tok.string == "None"

    def is_op(tok: tokenize.TokenInfo, op: str) -> bool:
        return tok.string == op and tok.type == tokenize.OP

    def idx(tok: tokenize.TokenInfo, use_end: bool = False) -> int:
        row, col = tok.end if use_end else tok.start
        return pos_to_index(starts, row, col)

    # 过滤掉编码/注释/空白之外的有效 token 序列，直接在全量 token 上滑窗即可
    # generate_tokens 不产生空格 token，相邻比较运算符与操作数是相邻的。
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        # X == None  /  X != None
        if is_op(tok, "==") or is_op(tok, "!="):
            if i + 1 < n and is_none(tokens[i + 1]):
                new = "is" if tok.string == "==" else "is not"
                start = idx(tok)
                end = idx(tokens[i + 1], use_end=False)
                before = text[start - 1] if start > 0 else ""
                if before and before not in " \t":
                    new = " " + new
                # 替换掉运算符到 None 之前的部分，保证 None 前至少有一个空格
                if not new.endswith(" "):
                    new = new + " "
                reps.append((start, end, new))
                i += 2
                continue
        # None == X  /  None != X
        if is_none(tok) and i + 1 < n and (is_op(tokens[i + 1], "==") or is_op(tokens[i + 1], "!=")):
            op = tokens[i + 1]
            new = "is" if op.string == "==" else "is not"
            start, end = idx(op), idx(op, use_end=True)
            # 无空格的写法 None==x 需要在替换后补空格，避免变成 None isx
            before = text[start - 1] if start > 0 else ""
            after = text[end] if end < len(text) else ""
            if before and before not in " \t":
                new = " " + new
            if after and after not in " \t\r\n":
                new = new + " "
            reps.append((start, end, new))
            i += 2
            continue
        i += 1
    return _apply_span_replacements(text, reps)


def fix_bare_except(text: str) -> str:
    tokens = _safe_tokens(text)
    if not tokens:
        return text
    starts = line_start_offsets(text)
    reps: list[tuple[int, int, str]] = []
    for i, tok in enumerate(tokens):
        if not (tok.type == tokenize.NAME and tok.string == "except"):
            continue
        # 跳过 NL / COMMENT，看下一个有效符号是否为冒号
        j = i + 1
        while j < len(tokens) and tokens[j].type in {tokenize.NL, tokenize.COMMENT, tokenize.NEWLINE}:
            # except 和冒号之间一般不会有 NEWLINE（那会语法错误），保险起见只允许 COMMENT？
            # 实际上 `except:` 中间最多是空格，tokenize 不输出空格。若有注释则是 `except:  #`
            break
        if j < len(tokens) and tokens[j].type == tokenize.OP and tokens[j].string == ":":
            # 在 except 和 : 之间插入 Exception
            end_of_except = pos_to_index(starts, tok.end[0], tok.end[1])
            reps.append((end_of_except, end_of_except, " Exception"))
    return _apply_span_replacements(text, reps)


def _line_has_noqa(body: str) -> bool:
    lower = body.lower()
    return "# noqa" in lower or "# type: ignore" in lower or "# noinspection" in lower


def fix_unused_imports(text: str, path: str = "<fix>") -> str:
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError:
        return text

    visitor = UsageVisitor()
    visitor.visit(tree)
    used = visitor.used | _names_in_all(tree)
    records = collect_imports(tree)

    unused_by_node: dict[int, list[ImportRecord]] = {}
    node_map: dict[int, ast.AST] = {}
    for rec in records:
        if rec.is_future or rec.is_star:
            continue
        if rec.bound not in used:
            key = id(rec.node)
            unused_by_node.setdefault(key, []).append(rec)
            node_map[key] = rec.node

    if not unused_by_node:
        return text

    parts = iter_lines(text)
    # 记录哪些物理行要替换或删除（1-based），从后往前改
    line_changes: dict[int, Optional[str]] = {}  # None 表示删除该行

    for key, unused_recs in unused_by_node.items():
        node = node_map[key]
        start = node.lineno
        end = getattr(node, "end_lineno", start) or start
        # 只处理单行导入
        if end != start:
            continue
        if start < 1 or start > len(parts):
            continue
        body, ending = parts[start - 1]
        if ";" in body:
            continue
        if _line_has_noqa(body):
            continue
        stripped = body.strip()
        if not (stripped.startswith("import ") or stripped.startswith("from ")):
            continue

        if isinstance(node, ast.Import):
            remaining = [a for a in node.names if (a.asname or a.name.split(".")[0]) in used]
            prefix = body[: len(body) - len(body.lstrip())]
            comment = ""
            if "#" in body:
                # 只在不属于引号时粗略处理：简单导入行很少有引号
                hash_pos = body.find("#")
                comment = body[hash_pos:]
            if not remaining:
                line_changes[start] = None
            else:
                bits = []
                for a in remaining:
                    bits.append(f"{a.name} as {a.asname}" if a.asname else a.name)
                new_body = prefix + "import " + ", ".join(bits)
                if comment and not remaining:
                    pass
                elif comment:
                    new_body = new_body + "  " + comment.lstrip()
                line_changes[start] = new_body
        elif isinstance(node, ast.ImportFrom):
            remaining = [a for a in node.names if (a.asname or a.name) in used or a.name == "*"]
            prefix = body[: len(body) - len(body.lstrip())]
            comment = ""
            if "#" in body:
                comment = body[body.find("#") :]
            dots = "." * (node.level or 0)
            module = node.module or ""
            if not remaining:
                line_changes[start] = None
            else:
                bits = []
                for a in remaining:
                    bits.append(f"{a.name} as {a.asname}" if a.asname else a.name)
                new_body = prefix + f"from {dots}{module} import " + ", ".join(bits)
                if comment:
                    new_body = new_body + "  " + comment.lstrip()
                line_changes[start] = new_body

    if not line_changes:
        return text

    new_parts: list[tuple[str, str]] = []
    for lineno, (body, ending) in enumerate(parts, start=1):
        if lineno not in line_changes:
            new_parts.append((body, ending))
            continue
        new_body = line_changes[lineno]
        if new_body is None:
            continue
        new_parts.append((new_body, ending))
    return join_lines(new_parts)


def apply_fixes(text: str, path: str = "<fix>") -> str:
    text = fix_trailing_whitespace(text)
    text = fix_indent_tabs(text)
    text = fix_none_comparisons(text)
    text = fix_bare_except(text)
    text = fix_unused_imports(text, path)
    text = fix_final_newline(text)
    return text


# ---------------------------------------------------------------------------
# 差异与输出
# ---------------------------------------------------------------------------


def unified_diff(original: str, fixed: str, path: str) -> str:
    a = original.splitlines(keepends=True)
    b = fixed.splitlines(keepends=True)
    # 保证 splitlines 在无换行末尾时也能出 diff
    diff = difflib.unified_diff(a, b, fromfile=f"a/{path}", tofile=f"b/{path}", n=3)
    return "".join(diff)


def rel_display(path: str, roots: list[str]) -> str:
    abs_path = os.path.abspath(path)
    best = abs_path
    for root in roots:
        root_abs = os.path.abspath(root)
        if os.path.isdir(root_abs):
            try:
                cand = os.path.relpath(abs_path, root_abs)
            except ValueError:
                cand = abs_path
            if not cand.startswith("..") and len(cand) < len(best):
                best = cand
        elif os.path.isfile(root_abs) and os.path.normcase(abs_path) == os.path.normcase(root_abs):
            best = os.path.basename(abs_path)
    return best


def print_issues(results: list[FileResult], roots: list[str]) -> None:
    for res in results:
        display = rel_display(res.path, roots)
        if res.error:
            print(f"错误  {display}: {res.error}")
            continue
        for it in res.issues:
            loc = f"{display}:{it.line}"
            if it.column:
                loc = f"{loc}:{it.column}"
            flag = ""
            if it.fixed:
                flag = "  [已修复]"
            elif it.fixable:
                flag = "  [可修复]"
            print(f"{loc}: [{it.code}] {it.message}{flag}")


def print_summary(results: list[FileResult], scanned: int, will_fix: bool, dry_run: bool) -> None:
    issues = [it for r in results for it in r.issues]
    found = len(issues)
    fixed = sum(1 for it in issues if it.fixed)
    errors = sum(1 for r in results if r.error)
    print()
    print("======== 扫描摘要 ========")
    print(f"扫描文件：{scanned}")
    if errors:
        print(f"读取失败：{errors}")
    print(f"发现问题：{found}")
    if will_fix or dry_run:
        label = "将修复" if dry_run and not will_fix else "已修复"
        if dry_run:
            label = "可自动修复"
        print(f"{label}：{fixed}")
        leftover = found - fixed
        if leftover:
            print(f"需人工处理：{leftover}")
    else:
        fixable = sum(1 for it in issues if it.fixable)
        if fixable:
            print(f"其中可自动修复：{fixable}（加上 --fix 即可修复）")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def process_file(
    path: str,
    max_line: int,
    check_print: bool,
    do_fix: bool,
    dry_run: bool,
    no_backup: bool,
) -> FileResult:
    res = FileResult(path=path)
    try:
        text, encoding = read_file(path)
    except OSError as exc:
        res.error = f"无法读取文件：{exc}"
        return res

    issues = scan_text(path, text, max_line, check_print)
    res.issues = issues

    if not (do_fix or dry_run):
        return res

    fixed = apply_fixes(text, path)
    res.fixed_text = fixed

    # 用修复后的扫描结果，标记哪些问题消失了
    after = scan_text(path, fixed, max_line, check_print)
    after_keys = {(it.line, it.column, it.code, it.message) for it in after}
    # 行号可能因删除导入而变化，改用 code+message 的数量对比更稳妥
    after_bag: dict[tuple[str, str], int] = {}
    for it in after:
        k = (it.code, it.message)
        after_bag[k] = after_bag.get(k, 0) + 1
    for it in issues:
        k = (it.code, it.message)
        if after_bag.get(k, 0) <= 0:
            if it.fixable:
                it.fixed = True
        else:
            after_bag[k] -= 1
            # 还在，未修复（例如 noqa 的未使用导入）
            if it.fixable and it.code in {
                "unused-import",
            }:
                it.fixed = False

    if fixed == text:
        return res

    if dry_run or not do_fix:
        return res

    if not no_backup:
        backup = path + ".bak"
        try:
            if os.path.exists(backup):
                os.remove(backup)
            # 用原始字节备份更稳妥，这里按文本+原编码写即可
            write_file(backup, text, encoding)
            res.backup = backup
        except OSError as exc:
            res.error = f"无法写入备份文件：{exc}"
            return res
    try:
        write_file(path, fixed, encoding)
        res.wrote = True
    except OSError as exc:
        res.error = f"无法写入文件：{exc}"
    return res


def build_report(results: list[FileResult], scanned: int) -> dict:
    issues = []
    for r in results:
        for it in r.issues:
            item = asdict(it)
            issues.append(item)
    return {
        "files_scanned": scanned,
        "issues_found": sum(len(r.issues) for r in results),
        "issues_fixed": sum(1 for r in results for it in r.issues if it.fixed),
        "files": [
            {
                "path": r.path,
                "error": r.error,
                "wrote": r.wrote,
                "backup": r.backup,
                "issue_count": len(r.issues),
            }
            for r in results
        ],
        "issues": issues,
    }


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="code_fixer.py",
        description="扫描 Python 代码中的常见问题，并可安全自动修复其中一部分。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python code_fixer.py 示例_有问题的代码.py\n"
            "  python code_fixer.py --dry-run 示例_有问题的代码.py\n"
            "  python code_fixer.py --fix 示例_有问题的代码.py\n"
            "  python code_fixer.py --fix --report report.json somedir\n"
        ),
    )
    p.add_argument("paths", nargs="+", help="要扫描的 Python 文件或目录（目录会递归查找 .py 文件）")
    p.add_argument("--fix", action="store_true", help="自动修复安全项，并在修改前写入 .bak 备份")
    p.add_argument("--dry-run", action="store_true", help="只显示将要进行的修改（unified diff），不写入文件")
    p.add_argument("--no-backup", action="store_true", help="修复时不创建 .bak 备份（请谨慎使用）")
    p.add_argument("--report", metavar="FILE", help="把扫描结果写成 JSON 报告")
    p.add_argument("--max-line", type=int, default=120, metavar="N", help="行长度限制，默认 120；设为 0 可关闭")
    p.add_argument("--check-print", action="store_true", help="额外检查可能是调试残留的 print() 调用")
    p.add_argument("--version", action="version", version=f"code_fixer {VERSION}")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    configure_stdio()
    args = parse_args(argv)
    files, collect_errors = collect_py_files(args.paths)
    for msg in collect_errors:
        print(msg, file=sys.stderr)

    if not files:
        print("没有找到可扫描的 .py 文件。", file=sys.stderr)
        return 2

    results: list[FileResult] = []
    for path in files:
        results.append(
            process_file(
                path=path,
                max_line=args.max_line,
                check_print=args.check_print,
                do_fix=args.fix,
                dry_run=args.dry_run,
                no_backup=args.no_backup,
            )
        )

    print_issues(results, args.paths)

    if args.dry_run:
        any_diff = False
        for res in results:
            if res.fixed_text is None:
                continue
            try:
                original, _ = read_file(res.path)
            except OSError:
                continue
            if res.fixed_text == original:
                continue
            diff = unified_diff(original, res.fixed_text, rel_display(res.path, args.paths))
            if diff:
                if not any_diff:
                    print()
                    print("-------- 预览修改（不会写入） --------")
                any_diff = True
                print(diff, end="" if diff.endswith("\n") else "\n")
        if not any_diff:
            print()
            print("没有可自动修复的内容。")

    print_summary(results, scanned=len(files), will_fix=args.fix, dry_run=args.dry_run)

    if args.fix and not args.dry_run:
        written = [r for r in results if r.wrote]
        if written:
            print(f"已写入 {len(written)} 个文件。")
            backed = [r for r in written if r.backup]
            if backed:
                print("已创建备份：" + "、".join(r.backup for r in backed))

    if args.report:
        report = build_report(results, scanned=len(files))
        try:
            with open(args.report, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            print(f"已写出报告：{args.report}")
        except OSError as exc:
            print(f"无法写出报告：{exc}", file=sys.stderr)
            return 1

    # 有语法错误时返回 1，仅有风格问题返回 0（方便作为检查工具使用时区分）
    if any(it.code == "syntax-error" for r in results for it in r.issues) or any(r.error for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
