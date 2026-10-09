#!/usr/bin/env python3
"""Minimal msgfmt: compile a .po file into a .mo file (used when GNU gettext
is not installed). Supports msgctxt, multi-line strings and plural forms."""

import ast
import struct
import sys


def parse(path):
    messages, entry, key = {}, {}, None

    def flush():
        if "msgid" in entry and not entry.get("fuzzy"):
            msgid = entry["msgid"]
            if "msgid_plural" in entry:
                msgid += "\0" + entry["msgid_plural"]
                msgstr = "\0".join(entry.get(f"msgstr[{i}]", "") for i in range(len(entry) )
                                   if f"msgstr[{i}]" in entry)
            else:
                msgstr = entry.get("msgstr", "")
            if "msgctxt" in entry:
                msgid = entry["msgctxt"] + "\x04" + msgid
            if msgstr or msgid == "":
                messages[msgid] = msgstr
        entry.clear()

    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line.startswith("#,") and "fuzzy" in line:
            flush()
            entry["fuzzy"] = True
            continue
        if not line or line.startswith("#"):
            continue
        if line.startswith(("msgid ", "msgctxt ")) and ("msgstr" in entry or any(k.startswith("msgstr[") for k in entry)):
            flush()
        if line.startswith('"'):
            entry[key] += ast.literal_eval(line)
        else:
            key, _, value = line.partition(" ")
            entry[key] = ast.literal_eval(value)
    flush()
    return messages


def write(messages, path):
    keys = sorted(messages)
    ids = b"".join(k.encode() + b"\0" for k in keys)
    strs = b"".join(messages[k].encode() + b"\0" for k in keys)
    n = len(keys)
    koffsets, voffsets, o = [], [], 0
    for k in keys:
        koffsets.append((len(k.encode()), o))
        o += len(k.encode()) + 1
    o = 0
    for k in keys:
        voffsets.append((len(messages[k].encode()), o))
        o += len(messages[k].encode()) + 1
    start = 7 * 4 + 16 * n
    out = struct.pack("Iiiiiii", 0x950412DE, 0, n, 7 * 4, 7 * 4 + 8 * n, 0, start)
    for length, off in koffsets:
        out += struct.pack("ii", length, start + off)
    for length, off in voffsets:
        out += struct.pack("ii", length, start + len(ids) + off)
    with open(path, "wb") as f:
        f.write(out + ids + strs)


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "-o":
        sys.exit("usage: msgfmt.py -o OUTPUT.mo INPUT.po")
    write(parse(sys.argv[3]), sys.argv[2])
