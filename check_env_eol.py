#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 uname.env 的换行与解析行为。

用法：
    python check_env_eol.py <file> [--expect-lf]

输出（每行一个事实，供 shell 断言）：
    CR=<n>            CR 字节数
    LF=<n>            LF 字节数
    BINVAL=<repr>     二进制读 STOCK_LOCALVERSION 的值
    TXTVAL=<repr>     文本模式读同一个值

退出码：
    0 = 符合预期
    1 = 有 CR（当带 --expect-lf 时）
    2 = 文件不存在
"""
import sys


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    expect_lf = '--expect-lf' in sys.argv[2:]

    try:
        data = open(path, 'rb').read()
    except FileNotFoundError:
        print('MISSING')
        return 2
    except OSError as e:
        print(f'IOERR {e}')
        return 2

    cr = data.count(b'\r')
    lf = data.count(b'\n')
    print(f'CR={cr}')
    print(f'LF={lf}')

    KEY_B = b'STOCK_LOCALVERSION='
    binval = None
    binval_polluted = None
    for raw in data.split(b'\n'):
        if raw.startswith(KEY_B):
            # 保留原始字节（含可能的 \r），这才是 runner 真正看到的
            binval_polluted = raw[len(KEY_B):]
            binval = binval_polluted.rstrip(b'\r')
            break
    print(f'BINVAL={binval!r}')
    print(f'BINPOLLUTED={binval_polluted!r}')

    KEY_T = 'STOCK_LOCALVERSION='
    txtval = None
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.rstrip('\n')
                if line.startswith(KEY_T):
                    txtval = line[len(KEY_T):]
                    break
    except OSError:
        pass
    print(f'TXTVAL={txtval!r}')

    if expect_lf and cr > 0:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
