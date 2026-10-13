#!/usr/bin/env python3
"""验证 scripts/check_repo_layout.py 真的会失败，而且不误报。

用法：

```bash
python3 scripts/test_check_repo_layout.py
```

为什么值得单独写一个自检：这个检查器最早的一版用 `git ls-files`（没加 `-z`）枚举，
中文路径被读成转义串，77 份文档里只有 36 份真正被打开，于是它报了"零缺失"。**一个
永远通过的检查比没有检查更糟** —— 它会让人以为已经查过了。所以这里在一个临时仓库里
分别制造四种问题，确认每一条都能被抓到，同时确认不该报的不报。

临时仓库里没有基线文件，所以"好"的那份状态必须一条断链都没有。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check_repo_layout.py"

GOOD_FILES: dict[str, bytes] = {
    ".gitignore": b"intro-page/\n/.venv/\n/life-circle-demo/node_modules\n",
    ".gitattributes": b"*.bat text eol=crlf\n*.command text eol=lf\n",
    "docs/存在.md": "# 存在\n".encode(),
    # 同一个目录里的链接，目标用 URL 编码写：中文路径要能解析。
    "docs/好.md": "# 好\n\n- [存在](./%E5%AD%98%E5%9C%A8.md)\n".encode(),
    "docs/中文 目录/好.md": "# 好\n\n- [存在](../存在.md)\n".encode(),
    # 代码块里的链接不是引用：不该报。
    "docs/代码块.md": "```\n[缺失](../没有这个文件.md)\n```\n".encode(),
    "docs/architecture/HYBRID_ISOCHRONE_DESIGN.md": "# 设计\n".encode(),
    # 代码里的模板链接（按生成文档所在目录写）：正确的那份不该报。
    "backend/tools/好模板.py": 'TEXT = "见 [设计](../../docs/architecture/HYBRID_ISOCHRONE_DESIGN.md)"\n'.encode(),
    "life-circle-demo/package.json": b'{"name": "life-circle-demo"}\n',
    "start.bat": "@echo off\r\n".encode(),
    "start.command": b"#!/bin/sh\n",
}

BAD_FILES: dict[str, bytes] = {
    # 中文 + 空格的路径上有一条断链。
    "docs/中文 目录/坏.md": "# 坏\n\n- [缺失](../没有这个文件.md)\n".encode(),
    # 目标只在本机存在、没有纳入 Git：克隆的人看不到它。
    "docs/未跟踪.md": "# 未跟踪\n\n- [仅本机](./仅本机.md)\n".encode(),
    # 文档搬走了，代码里的模板链接还指着旧路径。
    "backend/tools/模板.py": 'TEXT = "见 [设计](../../HYBRID_ISOCHRONE_DESIGN.md)"\n'.encode(),
}


def run(repo: Path, *args: str, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), cwd=repo, check=check, capture_output=True, text=True, input=input)


def add_absolute_symlink(repo: Path, name: str, target: str) -> None:
    """把一条绝对路径符号链接写进索引。

    这里不用 os.symlink：本仓库的检出设了 core.symlinks=false，而且自检要能在不支持
    符号链接的文件系统上照样跑。`--cacheinfo` 只写索引、与文件系统无关 —— 而索引正是
    PR #55 那次提交留下问题的地方。
    """
    digest = run(repo, "git", "hash-object", "-w", "--stdin", input=target).stdout.strip()
    run(repo, "git", "update-index", "--add", "--cacheinfo", f"120000,{digest},{name}")


def build(repo: Path, files: dict[str, bytes], *, bad: bool, bat_attr: bool = True) -> None:
    run(repo, "git", "init", "-q", "-b", "main")
    run(repo, "git", "config", "user.email", "selftest@example.invalid")
    run(repo, "git", "config", "user.name", "layout selftest")
    (repo / "scripts").mkdir()
    shutil.copy(CHECKER, repo / "scripts" / "check_repo_layout.py")
    payload = dict(files)
    if not bat_attr:
        # 没有 eol 属性时，CRLF 会被原样存进索引 —— 正是要抓的那种情况。
        del payload[".gitattributes"]
        payload["start.bat"] = b"@echo off\r\n"
    for name, data in payload.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    run(repo, "git", "add", "-A")
    run(repo, "git", "commit", "-q", "-m", "selftest")
    if bad:
        for name, data in BAD_FILES.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        run(repo, "git", "add", *BAD_FILES)
        run(repo, "git", "commit", "-q", "-m", "bad links")
        # 只在本机存在、没有纳入 Git 的目标。
        (repo / "docs" / "仅本机.md").write_text("# 仅本机\n", encoding="utf-8")
        # 一个被提交成 gitlink 的嵌套仓库。
        nested = repo / "nested"
        nested.mkdir()
        run(nested, "git", "init", "-q")
        run(nested, "git", "config", "user.email", "selftest@example.invalid")
        run(nested, "git", "config", "user.name", "layout selftest")
        (nested / "inner.txt").write_text("inner\n", encoding="utf-8")
        run(nested, "git", "add", "-A")
        run(nested, "git", "commit", "-q", "-m", "inner")
        run(repo, "git", "add", "nested")


def check(repo: Path) -> tuple[int, str]:
    result = run(repo, sys.executable, "scripts/check_repo_layout.py", check=False)
    return result.returncode, result.stdout + result.stderr


def main() -> int:
    failures: list[str] = []

    def expect(condition: bool, message: str) -> None:
        print(("  ✓ " if condition else "  ✗ ") + message)
        if not condition:
            failures.append(message)

    with tempfile.TemporaryDirectory(prefix="layout-selftest-") as directory:
        base = Path(directory)

        print("用例一：干净状态必须通过")
        good = base / "good"
        good.mkdir()
        build(good, GOOD_FILES, bad=False)
        code, output = check(good)
        expect(code == 0, f"退出码 0（实际 {code}）")
        if code != 0:
            print(output)

        print("用例二：四种问题都要抓到，且不误报")
        bad = base / "bad"
        bad.mkdir()
        build(bad, GOOD_FILES, bad=True)
        code, output = check(bad)
        expect(code == 1, f"退出码 1（实际 {code}）")
        expect("docs/中文 目录/坏.md" in output, "中文＋空格路径上的断链被抓到")
        expect("docs/未跟踪.md" in output, "只在本机存在的目标被抓到")
        expect("backend/tools/模板.py" in output, "代码里指向旧路径的模板链接被抓到")
        expect("gitlink" in output, "gitlink 被抓到")
        expect("docs/代码块.md" not in output, "代码块里的链接不误报")
        expect("docs/好.md" not in output, "正常链接不误报")
        expect("backend/tools/好模板.py" not in output, "正确的模板链接不误报")

        print("用例三：行尾不符合 .gitattributes 时失败")
        endings = base / "endings"
        endings.mkdir()
        build(endings, GOOD_FILES, bad=False, bat_attr=False)
        code, output = check(endings)
        expect(code == 1 and "start.bat" in output, f"索引里的 CRLF 被抓到（实际退出码 {code}）")

        print("用例四：依赖目录被提交时必须失败，只在本机安装时必须通过")
        deps = base / "deps"
        deps.mkdir()
        build(deps, GOOD_FILES, bad=False)
        modules = deps / "life-circle-demo" / "node_modules"
        # 真实的本地安装：一棵未跟踪的目录树，检查器不该把它当成仓库内容。
        (modules / "left-pad").mkdir(parents=True)
        (modules / "left-pad" / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
        code, output = check(deps)
        expect(code == 0, f"未跟踪的依赖目录不误报（实际 {code}）")
        if code != 0:
            print(output)

        # (a) 普通文件：`dir/` 形式的忽略规则挡不住它 —— 这正是 PR #55 的情形。
        shutil.rmtree(modules)
        modules.write_text("/home/someone/else/node_modules", encoding="utf-8")
        run(deps, "git", "add", "-f", "life-circle-demo/node_modules")
        run(deps, "git", "commit", "-q", "-m", "commit the installed dependencies")
        code, output = check(deps)
        expect(code == 1 and "依赖目录不该入库" in output,
               f"被提交的依赖被抓到（实际退出码 {code}）")

        # (b) 索引里的绝对路径符号链接：目标只在提交者机器上存在。
        run(deps, "git", "rm", "--cached", "-q", "life-circle-demo/node_modules")
        modules.unlink()
        add_absolute_symlink(deps, "life-circle-demo/node_modules", "/home/someone/else/node_modules")
        code, output = check(deps)
        expect(code == 1 and "绝对路径符号链接" in output,
               f"绝对路径符号链接被抓到（实际退出码 {code}）")

        print("用例五：忽略规则退回 `dir/` 形式时必须失败（#55 的原始情形）")
        trailing = base / "trailing"
        trailing.mkdir()
        # 实测过 git 的语义：`dir/` 只匹配**目录** —— 普通文件、符号链接、以及不存在的
        # 路径都匹配不到；不带尾斜杠的裸路径四种都匹配。所以这里放一个普通文件，
        # 规则一旦写回 `node_modules/`，这个未跟踪的依赖就不再被忽略。
        broken = dict(GOOD_FILES)
        broken[".gitignore"] = b"intro-page/\n/.venv/\n/life-circle-demo/node_modules/\n"
        build(trailing, broken, bad=False)
        (trailing / "life-circle-demo" / "node_modules").write_text(
            "/home/someone/else/node_modules", encoding="utf-8")
        code, output = check(trailing)
        expect(code == 1 and "不再被 .gitignore 忽略" in output,
               f"退回 `dir/` 规则时被抓到（实际退出码 {code}）")

    print()
    if failures:
        print(f"自检失败 {len(failures)} 项：" + "；".join(failures))
        return 1
    print("自检通过：检查器会失败，也不会误报。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
