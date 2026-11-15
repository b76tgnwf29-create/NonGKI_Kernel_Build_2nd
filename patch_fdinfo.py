#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
给 4.19 内核的 fs/notify/fdinfo.c 补上 SUSFS 的 SUS_KSTAT 伪装分支。

═══ 为什么必须补 ═══

SUSFS 补丁里 fs/notify/fdinfo.c 的三个 hunk，在一加 SM8250 源码上**全部被拒**
（.rej 实测 2 个）。旧版 workflow 只插了一个 include 就当作"非致命"放过 —— 这是错的。

`/proc/<pid>/fdinfo/<fd>` 里 inotify 的每一行会打印：

    inotify wd:1 ino:1a2b3c sdev:103 mask:fff ignored_mask:0

其中 `ino` 是 **inode 号**，`sdev` 是块设备号。检测方可以：

    a = stat("/data/local/tmp/fsvc")        → ino=AAA（已被 SUS_KSTAT 伪造成 AAA）
    b = 读某个持有该 fd 的进程的 fdinfo      → ino=BBB（真实 ino）

两边不一致就直接证明"这个文件被动过"。SUSFS 专门提供
`susfs_sus_kstat_spoof_inotify_fdinfo()` 就是为了让 b 也变成 AAA。

═══ 补丁三个 hunk 的命运 ═══

| hunk | 内容 | 状态 |
|---|---|---|
| 1 | `#include <linux/susfs_def.h>` | 能打上（本脚本也会兜底补） |
| 2 | `show_fdinfo` 增加 `struct file *file` 参数 | **被拒**，本脚本不做（侵入大、收益低） |
| 3 | `inotify_fdinfo` 加两段伪装（SUS_KSTAT + SUS_MOUNT） | **被拒** |

本脚本只按 hunk3 的**意图**重写 SUS_KSTAT 那一段，不硬拼 patch 上下文：

- **不做 SUS_MOUNT 那段**：它依赖 hunk2 引入的 `file->f_path`，而 hunk2 被拒；
  且 mnt_id 判断与 NoMount 的 VFS 重定向有职责重叠，收益低风险高。
- **`inotify_mark_user_mask(mark)` 必须换成 `mark->mask & IN_ALL_EVENTS`**：
  前者是 5.x 才有的 helper，4.19 没有，直接用会报 implicit declaration。

═══ 安全性 ═══

宁可不补，也不补坏：

1. 找不到锚点 → 跳过，退出 0（不修改文件）
2. `include/linux/susfs.h` 缺必要符号 → 跳过
3. 已经补过 → 跳过（幂等）
4. 每次真正修改前先备份到 `fdinfo.c.op8t.bak`

用法：
    python patch_fdinfo.py <内核源码根>
    python patch_fdinfo.py <内核源码根> --check
    python patch_fdinfo.py <内核源码根> --revert
"""
import os
import sys

MARKER = 'susfs_sus_kstat_spoof_inotify_fdinfo(&ino'

# 4.19 的 inotify_fdinfo 里，这一对连续行是唯一可靠的锚点。
# 注意 fanotify_fdinfo 里也有 `if (inode)`，但它前面不是这行 igrab。
ANCHOR = '\tinode = igrab(fsnotify_conn_inode(mark->connector));\n\tif (inode) {\n'

BLOCK = '''#ifdef CONFIG_KSU_SUSFS_SUS_KSTAT
\t\t/*
\t\t * SUS_KSTAT: /proc/<pid>/fdinfo/<fd> 里的 ino/sdev 必须与 stat() 一致。
\t\t * 不这样做，检测方拿 stat() 与 fdinfo 交叉比对就能断言文件被动过。
\t\t * 注意：4.19 没有 inotify_mark_user_mask()，等价写法是 mark->mask & IN_ALL_EVENTS。
\t\t */
\t\tif (susfs_is_current_app_uid()) {
\t\t\tbool is_fuse = false;
\t\t\tif (susfs_is_inode_sus_kstat(inode, &is_fuse)) {
\t\t\t\tunsigned long ino = inode->i_ino;
\t\t\t\tdev_t dev = inode->i_sb->s_dev;
\t\t\t\tsusfs_sus_kstat_spoof_inotify_fdinfo(&ino, &dev);
\t\t\t\tseq_printf(m, "inotify wd:%x ino:%lx sdev:%x mask:%x ignored_mask:%x ",
\t\t\t\t\t   inode_mark->wd, ino, dev,
\t\t\t\t\t   mark->mask & IN_ALL_EVENTS, mark->ignored_mask);
\t\t\t\tshow_mark_fhandle(m, inode);
\t\t\t\tseq_putc(m, '\\n');
\t\t\t\tiput(inode);
\t\t\t\treturn;
\t\t\t}
\t\t}
#endif /* CONFIG_KSU_SUSFS_SUS_KSTAT */
'''

INCLUDE_ANCHOR = '#include <linux/exportfs.h>\n'
INCLUDE_BLOCK = ('#ifdef CONFIG_KSU_SUSFS\n'
                 '#include <linux/susfs_def.h>\n'
                 '#endif\n')

DECL_ANCHOR = ('#if defined(CONFIG_INOTIFY_USER) || defined(CONFIG_FANOTIFY)\n')
DECL_BLOCK = ('\n'
              '#ifdef CONFIG_KSU_SUSFS_SUS_KSTAT\n'
              'extern bool susfs_is_inode_sus_kstat(struct inode *inode, bool *out_is_fuse);\n'
              'extern void susfs_sus_kstat_spoof_inotify_fdinfo(unsigned long *out_target_ino, '
              'dev_t *out_target_dev);\n'
              '#endif\n')

# include/linux/susfs.h 里必须具备这两个符号，否则插进去也编不过
NEEDED_SYMS = ('susfs_is_inode_sus_kstat', 'susfs_sus_kstat_spoof_inotify_fdinfo')


def rd(path):
    with open(path, 'r', encoding='utf-8', errors='surrogateescape') as f:
        return f.read()


def wr(path, text):
    with open(path, 'w', encoding='utf-8', errors='surrogateescape', newline='') as f:
        f.write(text)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    flags = set(a for a in sys.argv[1:] if a.startswith('--'))
    if not args:
        print(__doc__)
        return 2

    root = args[0]
    fdi = os.path.join(root, 'fs', 'notify', 'fdinfo.c')
    header = os.path.join(root, 'include', 'linux', 'susfs.h')
    bak = fdi + '.op8t.bak'

    # ── revert ──
    if '--revert' in flags:
        if os.path.isfile(bak):
            wr(fdi, rd(bak))
            os.remove(bak)
            print(f'[+] 已从备份还原：{fdi}')
        else:
            print(f'[--] 没有备份文件 {bak}')
        return 0

    if not os.path.isfile(fdi):
        print(f'[FATAL] 找不到 {fdi}')
        print('        第一个参数应是内核源码根（含 fs/ 与 Makefile 的那层）')
        return 1

    src = rd(fdi)
    done_block = MARKER in src
    done_inc = 'susfs_def.h' in src
    done_decl = 'susfs_sus_kstat_spoof_inotify_fdinfo(unsigned long' in src

    if '--check' in flags:
        print(f'  include susfs_def.h      : {"YES" if done_inc else "no"}')
        print(f'  外部符号声明              : {"YES" if done_decl else "no"}')
        print(f'  SUS_KSTAT 伪装分支        : {"YES" if done_block else "no"}')
        print(f'  锚点存在                  : {"YES" if ANCHOR in src else "NO"}')
        print(f'  include/linux/susfs.h     : {"存在" if os.path.isfile(header) else "缺失"}')
        return 0

    # 需要补功能体时，先确认头文件有符号
    if not done_block:
        if not os.path.isfile(header):
            print(f'[!] 缺 {header}，跳过（不补坏）')
            return 0
        hdr = rd(header)
        missing = [s for s in NEEDED_SYMS if s not in hdr]
        if missing:
            print(f'[!] include/linux/susfs.h 缺少 {missing}，跳过（不补坏）')
            return 0
        if ANCHOR not in src:
            print('[!] 找不到 inotify_fdinfo 的锚点，跳过（不补坏）')
            print(f'    期望的锚点：{ANCHOR!r}')
            return 0

    changed = False
    out = src

    if not done_inc:
        if INCLUDE_ANCHOR in out:
            out = out.replace(INCLUDE_ANCHOR, INCLUDE_ANCHOR + INCLUDE_BLOCK, 1)
            changed = True
            print('[+] 补 susfs_def.h include')
        else:
            print(f'[!] 找不到 include 锚点 {INCLUDE_ANCHOR!r}，跳过 include')

    if not done_decl:
        if DECL_ANCHOR in out:
            out = out.replace(DECL_ANCHOR, DECL_ANCHOR + DECL_BLOCK, 1)
            changed = True
            print('[+] 补 SUS_KSTAT 外部符号声明')
        else:
            print('[!] 找不到声明锚点，跳过声明（若功能体已插入则可能编不过，需人工核对）')

    if not done_block:
        out = out.replace(ANCHOR, ANCHOR + BLOCK, 1)
        changed = True
        print('[+] 插入 SUS_KSTAT 伪装分支')

    if not changed:
        print('[+] 三项都已是目标状态，无需修改（幂等）')
        return 0

    if not os.path.isfile(bak):
        wr(bak, src)
        print(f'[+] 已备份原文件 → {os.path.basename(bak)}')

    wr(fdi, out)
    print(f'[+] {fdi} 已更新')
    return 0


if __name__ == '__main__':
    sys.exit(main())
