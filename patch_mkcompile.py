#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 4.19 内核 scripts/mkcompile_h 的 LINUX_COMPILER 改成可被环境变量覆盖。

═══ 为什么必须改 ═══

`cat /proc/version` 读的是 `linux_banner` 字符串，它在编译期由
include/generated/compile.h 的 LINUX_COMPILER 宏拼成，而那个宏的内容是
mkcompile_h 跑 `$CC -v` 抓来的：

    echo \\#define LINUX_COMPILER \\"`$CC -v 2>&1 | grep ' version ' | ...`\\"

于是自编内核会把自己的编译环境原样印在 /proc/version 里：

    Linux version 4.19.110-perf+ (jenkins@rd-build-77)
    (clang version 19.1.7 (/home/runner/work/llvm-project/...)) #1 SMP PREEMPT ...

原厂是：

    Linux version 4.19.110-perf+ (jenkins@rd-build-90)
    (clang version 10.0.5 for Android NDK) #1 SMP PREEMPT Mon Jul 12 10:08:43 CST 2021

**SUSFS 的 SPOOF_UNAME 只 hook newuname 系统调用**（实测 susfs419.patch 里
挂的是 SYSCALL_DEFINE1(newuname)），它管不到 /proc/version。
所以要么在编译期就把串造对，要么裸奔。

═══ 改动 ═══

在 mkcompile_h 里插一个分支：KBUILD_FAKE_COMPILER 非空就用它，
否则回落到原始的 `$CC -v` 抓取逻辑。功能零影响 —— 这个宏只在
version.c 里被拼进 banner 一次。

═══ 幂等 ═══

已打过补丁（检测到 KBUILD_FAKE_COMPILER 字样）直接跳过，退出码 0。
可重复执行，不会叠加。

用法：
    python patch_mkcompile.py <内核源码根>            # 打补丁
    python patch_mkcompile.py <内核源码根> --check    # 只检查，不修改
    python patch_mkcompile.py <内核源码根> --revert   # 还原成原始逻辑
"""
import sys
import os

MARKER = 'KBUILD_FAKE_COMPILER'

# mkcompile_h 里那一行原始写法（4.19 与 5.x 一致；末尾空白用 rstrip 容忍）
ORIG_LINE_HEAD = r'  echo \#define LINUX_COMPILER \"'

REPLACEMENT = (
    '  # ── 编译期伪造编译器串（op8t-root）──\n'
    '  # /proc/version 读的就是这个宏；SUSFS 的 SPOOF_UNAME 只 hook newuname，\n'
    '  # 管不到这里。KBUILD_FAKE_COMPILER 由 ④-A2 从原厂 boot.img 解析后注入。\n'
    '  if [ -n "$KBUILD_FAKE_COMPILER" ]; then\n'
    '    KSU_LINUX_COMPILER="$KBUILD_FAKE_COMPILER"\n'
    '  else\n'
    '    KSU_LINUX_COMPILER=`$CC -v 2>&1 | grep \' version \' | sed \'s/[[:space:]]*$//\'`\n'
    '  fi\n'
    '  echo \\#define LINUX_COMPILER \\"$KSU_LINUX_COMPILER\\"\n'
)


def read_text(path):
    with open(path, 'r', encoding='utf-8', errors='surrogateescape') as f:
        return f.read()


def write_text(path, text):
    with open(path, 'w', encoding='utf-8', errors='surrogateescape', newline='') as f:
        f.write(text)


def find_compiler_line(lines):
    """返回 LINUX_COMPILER 那一行的下标（-1 表示没找到）"""
    for i, line in enumerate(lines):
        if 'LINUX_COMPILER' in line and 'echo' in line:
            return i
    return -1


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    flags = set(a for a in sys.argv[1:] if a.startswith('--'))

    if not args:
        print(__doc__)
        return 2

    root = args[0]
    target = os.path.join(root, 'scripts', 'mkcompile_h')
    if not os.path.isfile(target):
        print(f"[FATAL] 找不到 {target}")
        print("        确认第一个参数是内核源码根（含 scripts/ 与 Makefile 的那层）")
        return 1

    text = read_text(target)
    lines = text.split('\n')
    idx = find_compiler_line(lines)

    already = MARKER in text

    if '--check' in flags:
        if already:
            print(f"[OK] 已打过补丁：{target}")
            return 0
        if idx < 0:
            print(f"[FAIL] 没找到 LINUX_COMPILER 行：{target}")
            return 1
        print(f"[--] 未打补丁，LINUX_COMPILER 在第 {idx + 1} 行：")
        print(f"     {lines[idx].strip()[:120]}")
        return 0

    if '--revert' in flags:
        if not already:
            print("[--] 没有可还原的补丁")
            return 0
        # 补丁块是连续的：从「编译期伪造编译器串」注释行，到 echo LINUX_COMPILER 行
        start = end = None
        for i, line in enumerate(lines):
            if start is None and '编译期伪造编译器串' in line:
                start = i
                continue
            if start is not None and 'echo' in line and 'LINUX_COMPILER' in line:
                end = i
                break
        if start is None or end is None:
            print(f"[FATAL] 定位不到补丁块（start={start}, end={end}）")
            return 1
        orig = (
            '  echo \\#define LINUX_COMPILER \\"'
            '`$CC -v 2>&1 | grep \' version \' | sed \'s/[[:space:]]*$//\'`\\"'
        )
        lines[start:end + 1] = [orig]
        write_text(target, '\n'.join(lines))
        print(f"[+] 已还原：{target}（第 {start + 1}–{end + 1} 行 → 1 行）")
        return 0

    if already:
        print(f"[+] 已打过补丁，跳过：{target}")
        return 0

    if idx < 0:
        print(f"[FATAL] 在 {target} 里找不到包含 LINUX_COMPILER 的 echo 行")
        print("        可能是内核版本差异，需人工核对 mkcompile_h")
        return 1

    lines[idx:idx + 1] = REPLACEMENT.rstrip('\n').split('\n')
    write_text(target, '\n'.join(lines))
    print(f"[+] 已改 {target}：LINUX_COMPILER 改为可被 KBUILD_FAKE_COMPILER 覆盖")
    print(f"    原第 {idx + 1} 行已被 7 行分支替换")
    for line in REPLACEMENT.rstrip('\n').split('\n'):
        print(f"    | {line}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
