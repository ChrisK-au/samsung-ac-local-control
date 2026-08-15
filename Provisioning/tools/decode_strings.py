#!/usr/bin/env python3
"""Reconstruct Samsung's obfuscated string table and decode strings."""
import re, sys

SMALI = "/tmp/apkx/smali/com/samsung/cac/jungfrau/AirconControlActivity$mDrawerLayout$AccessibilityDelegate.smali"

def parse_array_data(text):
    """Extract ints from a .array-data 4 block."""
    m = re.search(r"\.array-data 4\n(.*?)\.end array-data", text, re.S)
    vals = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("."):
            continue
        line = line.split("#")[0].strip()
        vals.append(int(line, 16) if line.startswith(("0x", "-0x")) else int(line))
    return vals

def get_method_text(smali, name):
    m = re.search(r"\.method public static %s\(\[IIII\)V\n(.*?)\.end method" % re.escape(name), smali, re.S)
    return m.group(1)

def build_table():
    smali = open(SMALI).read()
    arr = [0] * 0x1f06a  # 127082
    calls = [
        ("shiftLeftContainedParsersCount", 2, 0, 0x7ff3),
        ("shiftRightOnShuffleModeChanged", 3, 0x7ff3, 0x7ff3),
        ("toStringIsRtlLocale", 0, 0xffe6, 0x7ff3),
        ("createCubicOnClick", 2, 0x17fd9, 0x7091),
    ]
    for name, srcpos, dstpos, length in calls:
        text = get_method_text(smali, name)
        src = parse_array_data(text)
        arr[dstpos:dstpos+length] = src[srcpos:srcpos+length]
    return arr

def decode(idx, arr):
    """Mirror MediaBrowserCompat$MediaBrowserImplBase$1R.aA(I)."""
    v1 = arr[idx]
    p0 = idx - 1
    v0 = arr[p0]
    p0 -= 1
    v0 ^= v1
    chars = [0] * v0
    v2 = v0
    v0 -= 1
    while v0 >= 0:
        v4 = arr[p0]
        v4 = (v4 - v2) & 0xFFFFFFFF
        v2 = (v4 ^ v1) & 0xFFFFFFFF
        chars[v0] = v2 & 0xFFFF
        v1 = arr[p0]
        p0 -= 1
        v0 -= 1
    return "".join(chr(c) for c in chars)


def decode2(idx, arr):
    """Mirror isVisibleForViewFindSerializationName(I)."""
    v1 = arr[idx]
    p0 = idx + 1
    v2 = arr[p0]
    p0 += 1
    v2 ^= v1
    chars = [0] * v2
    v3 = v2
    v0 = 0
    while v0 < v2:
        v6 = arr[v0 + p0]
        v3 = (v6 - v3) & 0xFFFFFFFF
        v3 = (v3 ^ v1) & 0xFFFFFFFF
        v1 = v6
        chars[v0] = v3 & 0xFFFF
        v0 += 1
    return "".join(chr(c) for c in chars)

if __name__ == "__main__":
    arr = build_table()
    print(f"table built, nonzero entries: {sum(1 for x in arr if x != 0)}", file=sys.stderr)
    args = sys.argv[1:]
    if not args:
        args = ["0x14d3", "0x14d4"]
    dec = decode
    for a in args:
        if a.startswith("+"):
            dec, a = decode2, a[1:]
        i = int(a, 16)
        try:
            print(f"0x{i:x}: {dec(i, arr)!r}")
        except Exception as e:
            print(f"0x{i:x}: ERROR {e}")
