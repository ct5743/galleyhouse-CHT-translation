#!/usr/bin/env python3
"""
godot_pck.py - list, unpack and repack Godot 4 .pck files (format v2 and v3).

  python godot_pck.py list    game.pck
  python godot_pck.py unpack  game.pck [-o outdir] [--verify]
  python godot_pck.py repack  outdir   [-o new.pck] [--align 32]
                              [--format 2|3] [--godot 4.3.0]

Unpacking writes a manifest (_pck_manifest.json) next to the extracted files.
Repacking uses it to restore the original format version, engine version,
flags and file order. Files you add are appended; files you delete are dropped.

Notes:
  * Format v2 = Godot 4.0-4.3, v3 = Godot 4.4+. The repacked PCK keeps the
    original version unless you override it. Use the version your target
    engine understands.
  * PCKs embedded in an .exe / ELF (appended) can be unpacked, but repack
    always writes a standalone .pck.
  * Encrypted PCKs are not supported.
"""
import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

MAGIC = b"GDPC"
MANIFEST = "_pck_manifest.json"

PACK_DIR_ENCRYPTED = 1
PACK_REL_FILEBASE = 2
FILE_ENCRYPTED = 1
FILE_REMOVAL = 2

CHUNK = 1 << 20


def align(n, a):
    return n if a <= 1 else (n + a - 1) // a * a


def die(msg):
    sys.exit(f"error: {msg}")


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
def find_pck_start(f):
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if f.read(4) == MAGIC:
        return 0
    # Embedded: [pck][u64 pck_size]["GDPC"] at the end of the file
    if size >= 12:
        f.seek(size - 4)
        if f.read(4) == MAGIC:
            f.seek(size - 12)
            (pck_size,) = struct.unpack("<Q", f.read(8))
            start = size - 12 - pck_size
            if start >= 0:
                f.seek(start)
                if f.read(4) == MAGIC:
                    return start
    die("no GDPC header found (not a Godot PCK, or not Godot 4)")


def read_pck(f):
    """Parse header + directory. Returns (info, entries)."""
    start = find_pck_start(f)
    f.seek(start + 4)
    version, vmaj, vmin, vrev = struct.unpack("<4I", f.read(16))
    if version not in (2, 3):
        die(f"unsupported pack format version {version} "
            "(Godot 3 PCKs use version 1 and are not supported)")
    flags, file_base = struct.unpack("<IQ", f.read(12))
    dir_offset = None
    if version >= 3:
        (dir_offset,) = struct.unpack("<Q", f.read(8))
    f.read(64)  # 16 reserved uint32

    if flags & PACK_DIR_ENCRYPTED:
        die("encrypted PCK directory is not supported")
    if flags & PACK_REL_FILEBASE:
        file_base += start
    if version >= 3:
        f.seek(dir_offset + (start if flags & PACK_REL_FILEBASE else 0))

    (count,) = struct.unpack("<I", f.read(4))
    entries = []
    for _ in range(count):
        (plen,) = struct.unpack("<I", f.read(4))
        path = f.read(plen).rstrip(b"\0").decode("utf-8")
        ofs, size = struct.unpack("<QQ", f.read(16))
        md5 = f.read(16)
        (fflags,) = struct.unpack("<I", f.read(4))
        entries.append({"path": path, "offset": file_base + ofs, "size": size,
                        "md5": md5, "flags": fflags})

    info = {"format_version": version, "godot": [vmaj, vmin, vrev],
            "flags": flags & ~PACK_REL_FILEBASE}
    return info, entries


def disk_name(pck_path):
    p = pck_path[6:] if pck_path.startswith("res://") else pck_path.replace("://", "/")
    parts = [x for x in p.replace("\\", "/").split("/") if x not in ("", ".")]
    if ".." in parts or (parts and ":" in parts[0]):
        die(f"refusing unsafe path in PCK: {pck_path!r}")
    return "/".join(parts)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def cmd_list(args):
    with open(args.pck, "rb") as f:
        info, entries = read_pck(f)
    print(f"format v{info['format_version']}, Godot "
          f"{'.'.join(map(str, info['godot']))}, {len(entries)} files")
    for e in entries:
        tag = " [encrypted]" if e["flags"] & FILE_ENCRYPTED else ""
        tag += " [removal]" if e["flags"] & FILE_REMOVAL else ""
        print(f"{e['size']:>12}  {e['path']}{tag}")


def cmd_unpack(args):
    src = Path(args.pck)
    out = Path(args.output or src.stem)
    out.mkdir(parents=True, exist_ok=True)

    with open(src, "rb") as f:
        info, entries = read_pck(f)
        manifest_files = []
        bad = 0
        for i, e in enumerate(entries, 1):
            rel = disk_name(e["path"])
            manifest_files.append({"path": e["path"], "disk": rel,
                                   "flags": e["flags"]})
            if e["flags"] & FILE_REMOVAL:
                continue
            if e["flags"] & FILE_ENCRYPTED:
                print(f"skip (encrypted): {e['path']}")
                continue
            dest = out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            md5 = hashlib.md5()
            f.seek(e["offset"])
            left = e["size"]
            with open(dest, "wb") as o:
                while left:
                    buf = f.read(min(CHUNK, left))
                    if not buf:
                        die(f"unexpected end of file while reading {e['path']}")
                    o.write(buf)
                    md5.update(buf)
                    left -= len(buf)
            if args.verify and md5.digest() != e["md5"]:
                bad += 1
                print(f"MD5 mismatch: {e['path']}")
            if not args.quiet:
                print(f"[{i}/{len(entries)}] {rel}")

    info["files"] = manifest_files
    (out / MANIFEST).write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(f"unpacked {len(entries)} files to {out}/"
          + (f" ({bad} MD5 mismatches)" if args.verify else ""))


def cmd_repack(args):
    root = Path(args.directory)
    if not root.is_dir():
        die(f"{root} is not a directory")

    manifest = {}
    mpath = root / MANIFEST
    if mpath.exists():
        manifest = json.loads(mpath.read_text(encoding="utf-8"))

    version = args.format or manifest.get("format_version", 3)
    if version not in (2, 3):
        die("--format must be 2 or 3")
    if args.godot:
        try:
            godot = [int(x) for x in args.godot.split(".")]
            godot += [0] * (3 - len(godot))
            godot = godot[:3]
        except ValueError:
            die("--godot must look like 4.3.0")
    else:
        godot = manifest.get("godot", [4, 4, 0])
    flags = manifest.get("flags", 0) & ~(PACK_DIR_ENCRYPTED | PACK_REL_FILEBASE)
    if version >= 3:
        flags |= PACK_REL_FILEBASE
    alignment = args.align

    # Collect files on disk
    on_disk = {}
    for p in root.rglob("*"):
        if p.is_file() and p.name != MANIFEST:
            on_disk[p.relative_to(root).as_posix()] = p

    items = []  # dicts: path, flags, file(Path|None), size, md5
    seen = set()
    for m in manifest.get("files", []):
        d = m["disk"]
        if m["flags"] & FILE_REMOVAL:
            items.append({"path": m["path"], "flags": m["flags"], "file": None})
        elif d in on_disk and not (m["flags"] & FILE_ENCRYPTED):
            items.append({"path": m["path"], "flags": m["flags"], "file": on_disk[d]})
            seen.add(d)
    for d in sorted(on_disk.keys() - seen):
        items.append({"path": "res://" + d, "flags": 0, "file": on_disk[d]})

    # Pass 1: sizes and md5
    for it in items:
        if it["file"] is None:
            it["size"], it["md5"] = 0, b"\0" * 16
            continue
        h = hashlib.md5()
        n = 0
        with open(it["file"], "rb") as fh:
            while True:
                buf = fh.read(CHUNK)
                if not buf:
                    break
                h.update(buf)
                n += len(buf)
        it["size"], it["md5"] = n, h.digest()

    # Layout
    def enc_path(p):
        b = p.encode("utf-8")
        return b + b"\0" * ((4 - len(b) % 4) % 4)

    for it in items:
        it["pb"] = enc_path(it["path"])
    dir_size = 4 + sum(4 + len(it["pb"]) + 8 + 8 + 16 + 4 for it in items)
    header_len = 4 + 16 + 4 + 8 + (8 if version >= 3 else 0) + 64

    if version == 2:
        file_base = align(header_len + dir_size, alignment)
    else:
        file_base = align(header_len, alignment)

    ofs = 0
    for it in items:
        it["offset"] = ofs
        ofs = align(ofs + it["size"], alignment) if it["file"] else ofs
    data_end = file_base + ofs
    dir_offset = data_end if version >= 3 else header_len

    out = Path(args.output or (root.name + ".pck"))
    with open(out, "wb") as o:
        o.write(MAGIC)
        o.write(struct.pack("<4I", version, *godot))
        o.write(struct.pack("<IQ", flags, file_base))
        if version >= 3:
            o.write(struct.pack("<Q", dir_offset))
        o.write(b"\0" * 64)

        def write_dir():
            o.write(struct.pack("<I", len(items)))
            for it in items:
                o.write(struct.pack("<I", len(it["pb"])))
                o.write(it["pb"])
                o.write(struct.pack("<QQ", it["offset"], it["size"]))
                o.write(it["md5"])
                o.write(struct.pack("<I", it["flags"]))

        if version == 2:
            write_dir()
        o.write(b"\0" * (file_base - o.tell()))

        for it in items:
            if it["file"] is None:
                continue
            with open(it["file"], "rb") as fh:
                while True:
                    buf = fh.read(CHUNK)
                    if not buf:
                        break
                    o.write(buf)
            o.write(b"\0" * (align(it["size"], alignment) - it["size"]))

        if version >= 3:
            assert o.tell() == data_end
            write_dir()

    print(f"wrote {out} (format v{version}, Godot {'.'.join(map(str, godot))}, "
          f"{len(items)} files)")


def main():
    ap = argparse.ArgumentParser(description="Godot 4 PCK unpack/repack tool")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list files in a PCK")
    p.add_argument("pck")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("unpack", help="extract a PCK")
    p.add_argument("pck")
    p.add_argument("-o", "--output", help="output directory (default: PCK name)")
    p.add_argument("--verify", action="store_true", help="check MD5 of each file")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(fn=cmd_unpack)

    p = sub.add_parser("repack", help="build a PCK from a directory")
    p.add_argument("directory")
    p.add_argument("-o", "--output", help="output .pck (default: <dir>.pck)")
    p.add_argument("--align", type=int, default=32, help="file alignment (default 32)")
    p.add_argument("--format", type=int, choices=(2, 3),
                   help="pack format version (default: same as original)")
    p.add_argument("--godot", help="engine version to write, e.g. 4.3.0 "
                                   "(default: same as original)")
    p.set_defaults(fn=cmd_repack)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
