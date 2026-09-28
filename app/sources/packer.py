"""Dean Edwards packer 解包器。

部分站点（Mangabz 的 ``chapterimage.ashx``）返回形如::

    eval(function(p,a,c,k,e,d){...}('0.1.2',62,7,'a|b|c'.split('|'),0,{}))

的压缩脚本，解包后才是真正的内容（例如 ``var pix="https://…"; var d=["1.jpg",…]``）。
这里实现标准解包算法，纯标准库、无依赖。
"""

from __future__ import annotations

import re

_PACKED_RE = re.compile(r"}\(\s*'(?P<payload>.*)',\s*(?P<radix>\d+),\s*(?P<count>\d+),\s*'(?P<table>.*)'\.split\('\|'\)", re.S)

_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _unescape(payload: str) -> str:
    out: list[str] = []
    index = 0
    length = len(payload)
    while index < length:
        char = payload[index]
        if char == "\\" and index + 1 < length:
            nxt = payload[index + 1]
            if nxt == "\\":
                out.append("\\")
                index += 2
                continue
            if nxt == "'":
                out.append("'")
                index += 2
                continue
            if nxt == "n":
                out.append("\n")
                index += 2
                continue
            if nxt == "r":
                out.append("\r")
                index += 2
                continue
            out.append(nxt)
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _encode(counter: int, radix: int, cache: dict[int, str]) -> str:
    cached = cache.get(counter)
    if cached is not None:
        return cached
    if counter < radix:
        result = _BASE36[counter] if counter < 36 else chr(counter + 29)
    else:
        result = _encode(counter // radix, radix, cache) + (
            _BASE36[counter % radix] if counter % radix < 36 else chr(counter % radix + 29)
        )
    cache[counter] = result
    return result


def unpack(source: str) -> str:
    """解包 packed 脚本；若不是 packed 内容则原样返回。"""
    match = _PACKED_RE.search(source)
    if not match:
        return source

    payload = _unescape(match.group("payload"))
    radix = int(match.group("radix"))
    count = int(match.group("count"))
    table = match.group("table").split("|")

    cache: dict[int, str] = {}
    while count:
        count -= 1
        if count < len(table) and table[count]:
            payload = re.sub(r"\b" + re.escape(_encode(count, radix, cache)) + r"\b", table[count], payload)
    return payload
