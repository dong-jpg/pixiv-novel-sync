from __future__ import annotations

import ast
import builtins
import symtable
from pathlib import Path

SRC = Path(__file__).parents[1] / "src" / "pixiv_novel_sync"

# 模块对象自带、不在 builtins 里的名字
_MODULE_ATTRS = {
    "__file__", "__name__", "__doc__", "__spec__", "__loader__", "__package__",
    "__builtins__", "__path__", "__cached__", "__annotations__",
}
_KNOWN = set(dir(builtins)) | _MODULE_ATTRS


def _undefined_globals(source: str, filename: str) -> set[str]:
    module = symtable.symtable(source, filename, "exec")
    defined = {
        symbol.get_name()
        for symbol in module.get_symbols()
        if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()
    }

    def collect_global_assignments(table: symtable.SymbolTable) -> None:
        for symbol in table.get_symbols():
            if symbol.is_declared_global() and symbol.is_assigned():
                defined.add(symbol.get_name())
        for child in table.get_children():
            collect_global_assignments(child)

    collect_global_assignments(module)

    missing: set[str] = set()

    def check(table: symtable.SymbolTable) -> None:
        for symbol in table.get_symbols():
            if not symbol.is_referenced():
                continue
            if table is not module and not symbol.is_global():
                continue
            name = symbol.get_name()
            if name not in defined and name not in _KNOWN:
                missing.add(name)
        for child in table.get_children():
            check(child)

    check(module)
    return missing


def test_src_has_no_undefined_global_names() -> None:
    """compileall 只做语法检查，查不出未定义名称。

    回归（审查 2026-10-28 A1）：10-20 修复批给 ai/providers.py 加了一句
    ``logger.warning(...)``，文件里却没有 ``logger``，流未正常结束且解码出替换字符时
    抛 NameError。这里用 symtable 做一遍「引用了全局名却没有定义」的检查。
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        if any(
            isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
            for node in ast.walk(tree)
        ):
            continue  # 通配导入无法静态判定
        for name in sorted(_undefined_globals(source, str(path))):
            offenders.append(f"{path.relative_to(SRC).as_posix()}: {name}")

    assert offenders == []
