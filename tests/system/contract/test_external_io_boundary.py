"""SDK imports and raw HTTP belong to explicit drivers, including diagnostics."""

import ast
from pathlib import Path

from tests.helpers.shared.python_source import source_tree


def test_external_io_imports_have_one_designated_owner():
    root = Path(__file__).resolve().parents[3]
    owners = {
        "requests": "src/trader/infra/http_transport.py",
        "urllib3": "src/trader/infra/http_transport.py",
        "urllib.request": "src/trader/infra/http_transport.py",
        "http.client": "src/trader/infra/http_transport.py",
        "httpx": "src/trader/infra/http_transport.py",
        "aiohttp": "src/trader/infra/http_transport.py",
        "akshare": "src/trader/download/infra/akshare_calendar.py",
        "baostock": "src/trader/download/infra/baostock_session.py",
        "tushare": "src/trader/download/infra/tushare_records.py",
    }
    violations = []
    for directory in ("src", "scripts", "tests"):
        for path in (root / directory).rglob("*.py"):
            relative = path.relative_to(root).as_posix()
            for node in ast.walk(source_tree(path)):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                    names.extend(f"{node.module}.{alias.name}" for alias in node.names)
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                ):
                    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                        names = [node.args[0].value]
                for name in names:
                    if (
                        name == "trader.infra.http_transport"
                        and relative != "src/trader/bootstrap.py"
                        and not relative.startswith("tests/")
                        and not (
                            isinstance(node, ast.ImportFrom)
                            and {alias.name for alias in node.names} <= {"HttpRequestScope"}
                        )
                    ):
                        violations.append(f"{relative}:{node.lineno}:composition_root_bypass")
                    for sdk, owner in owners.items():
                        if (
                            sdk == "urllib.request" and isinstance(node, ast.ImportFrom)
                            and node.module == "urllib.request"
                            and {alias.name for alias in node.names} == {"url2pathname"}
                        ):
                            # URI decoding does not expose a network operation.
                            continue
                        if (name == sdk or name.startswith(sdk + ".")) and relative != owner:
                            violations.append(f"{relative}:{node.lineno}:{name}")
    assert violations == []
