"""文档事实护栏：README / docs 里的数字与文件引用必须与仓库现实一致。

背景：README 曾出现"快速开始写 64 个测试、实际已 109 个"的漂移，引用
不存在的文件则会让读者按图索骥扑空。两条断言把漂移挡在 CI 里。
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 明显是示例/占位的假路径前缀（当前仓库并不存在，纯防御性跳过）
SKIP_MARKERS = ("your_", "test_", "example_", "dummy_")


def test_readme_test_count_claim():
    """README 宣称的测试数 == pytest 实际收集数（本护栏自身不计入）。"""
    cmd = [sys.executable, "-m", "pytest", "tests/", "-q", "--co",
           "--ignore=tests/test_docs_consistency.py"]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    m = re.search(r"(\d+) tests? collected", proc.stdout)
    assert m, f"无法从 pytest 收集输出解析测试数:\n{proc.stdout[-400:]}"
    actual = int(m.group(1))

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    claim = (re.search(r"(\d+)测试全绿", readme)
             or re.search(r"(\d+) 测试", readme))
    assert claim, "README 中未找到 'N测试全绿' / 'N 测试' 形式的测试数宣称"

    claimed = int(claim.group(1))
    assert claimed == actual, (
        f"README 宣称 {claimed} 个测试，实际收集 {actual} 个——"
        f"请把 README 的数字同步为 {actual}")


def test_docs_reference_real_files():
    """docs/*.md 与 README 中出现的 app/**.py / static/* / docs/*.html 路径必须存在。"""
    pat = re.compile(r"app/[a-z_/]+\.py|static/[a-z_.]+|docs/[a-z\-]+\.html")
    docs = sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md"]

    refs = set()
    for doc in docs:
        refs.update(pat.findall(doc.read_text(encoding="utf-8")))

    assert refs, "未提取到任何文件引用——提取正则可能失效了"
    missing = [r for r in sorted(refs)
               if not any(marker in r for marker in SKIP_MARKERS)
               and not (ROOT / r).exists()]
    assert not missing, f"文档引用了不存在的文件: {missing}"
