"""Extract the chat template embedded in a GGUF file and check its sha256.

The chat template of run 1 is not redistributed: it is the string value of the metadata
key `tokenizer.chat_template` in the first file of the pinned GGUF build, and
`gpu/pod_start.sh` extracts it the same way on the pod before starting llama-server.
This script is the reference reader (pure Python, standard library) for doing it locally.

GGUF header (little-endian): magic b"GGUF", u32 version, u64 n_tensors, u64 n_kv, then
n_kv pairs of (key: string, u32 value type, value). A string is a u64 byte length and the
bytes (UTF-8, no terminator). Value types: 0 u8, 1 i8, 2 u16, 3 i16, 4 u32, 5 i32, 6 f32,
7 bool, 8 string, 9 array (u32 element type, u64 count, elements), 10 u64, 11 i64, 12 f64.

Usage:
  python gpu/extract_template.py <file 1 of the GGUF> --out <template file>
      [--sha256 HEX] [--key tokenizer.chat_template] [--config configs/run1.json]
The expected sha256 defaults to model.chat_template.sha256 in the run config. On a
mismatch nothing is written and the exit status is 1.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
from pathlib import Path
from typing import BinaryIO

ROOT = Path(__file__).resolve().parents[1]
KEY = "tokenizer.chat_template"
FIXED = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
STRING, ARRAY = 8, 9


class GGUFError(ValueError):
    pass


def _read(f: BinaryIO, n: int) -> bytes:
    b = f.read(n)
    if len(b) != n:
        raise GGUFError("unexpected end of file in the GGUF header")
    return b


def _u32(f: BinaryIO) -> int:
    return struct.unpack("<I", _read(f, 4))[0]


def _u64(f: BinaryIO) -> int:
    return struct.unpack("<Q", _read(f, 8))[0]


def _string(f: BinaryIO) -> bytes:
    return _read(f, _u64(f))


def _skip(f: BinaryIO, vtype: int) -> None:
    if vtype in FIXED:
        f.seek(FIXED[vtype], os.SEEK_CUR)
    elif vtype == STRING:
        f.seek(_u64(f), os.SEEK_CUR)
    elif vtype == ARRAY:
        etype, count = _u32(f), _u64(f)
        if etype in FIXED:
            f.seek(FIXED[etype] * count, os.SEEK_CUR)
        else:
            for _ in range(count):
                _skip(f, etype)
    else:
        raise GGUFError(f"unknown GGUF value type {vtype}")


def read_string_value(path: str | Path, key: str = KEY) -> bytes:
    """The raw bytes of the string value stored under `key` in a GGUF header."""
    with open(path, "rb") as f:
        if _read(f, 4) != b"GGUF":
            raise GGUFError(f"{path}: not a GGUF file")
        version = _u32(f)
        if version < 2:
            raise GGUFError(f"{path}: GGUF version {version} is not supported")
        _u64(f)  # tensor count
        n_kv = _u64(f)
        want = key.encode()
        for _ in range(n_kv):
            k = _string(f)
            vtype = _u32(f)
            if k == want:
                if vtype != STRING:
                    raise GGUFError(f"{key} has value type {vtype}, not string")
                return _string(f)
            _skip(f, vtype)
    raise GGUFError(f"{path}: no key {key}")


def default_sha256(config: Path) -> str:
    return json.loads(config.read_text())["model"]["chat_template"]["sha256"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("gguf")
    ap.add_argument("--out", required=True)
    ap.add_argument("--sha256", help="expected sha256 of the template bytes")
    ap.add_argument("--key", default=KEY)
    ap.add_argument("--config", default=str(ROOT / "configs" / "run1.json"))
    a = ap.parse_args()
    want = a.sha256 or default_sha256(Path(a.config))
    try:
        data = read_string_value(a.gguf, a.key)
    except (OSError, GGUFError) as e:
        sys.exit(f"extract_template: {e}")
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        print(f"extract_template: sha256 {got} != expected {want}; nothing written", file=sys.stderr)
        sys.exit(1)
    out = Path(a.out)
    tmp = out.with_name(out.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, out)
    print(json.dumps({"key": a.key, "bytes": len(data), "sha256": got, "out": out.name}))


if __name__ == "__main__":
    main()
