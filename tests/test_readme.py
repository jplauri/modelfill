"""The README's examples run as written."""

import importlib.util
import re
import sys
from pathlib import Path

README = Path(__file__).parent.parent / "README.md"


def test_readme_examples_run(tmp_path, capsys):
    blocks = re.findall(r"```python\n(.*?)```", README.read_text(encoding="utf-8"), flags=re.S)
    # All but the API signatures, in order, as one module whose validators inspect can read.
    source = "\n\n".join(block for block in blocks if "**fixed_fields" not in block)
    path = tmp_path / "readme_examples.py"
    path.write_text(f"{source}\n\ntest_shift_rules()\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("readme_examples", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["readme_examples"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules["readme_examples"]
    assert "start=17, duration=4 conflict with end <= 18" in capsys.readouterr().out
