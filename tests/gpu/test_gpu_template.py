"""Chat template from the GGUF metadata: gpu/extract_template.py (the full header reader)
and the shell extraction in gpu/pod_start.sh, on a small synthetic GGUF built here.

The real first file of the pinned build is not redistributed; set GAPCHECK_GGUF_SHARD1 to
its path to check both extractions against the expected sha256 as well."""
import hashlib
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GPU = REPO / "gpu"
sys.path.insert(0, str(GPU))
import extract_template as et  # noqa: E402

RUN1 = json.loads((REPO / "configs" / "run1.json").read_text())
TEMPLATE = ("{%- for message in messages %}<|im_start|>{{ message.role }}\n"
            "{{ message.content }}<|im_end|>\n{%- endfor %}é中\n").encode()


def s(b: bytes | str) -> bytes:
    b = b.encode() if isinstance(b, str) else b
    return struct.pack("<Q", len(b)) + b


def kv(key: str, vtype: int, payload: bytes) -> bytes:
    return s(key) + struct.pack("<I", vtype) + payload


def build_gguf(path: Path, template: bytes = TEMPLATE, with_template: bool = True,
               template_type: int = 8) -> None:
    """A GGUF header with every value type before the template, decoys included."""
    pairs = [
        kv("general.architecture", 8, s("qwen3next")),
        kv("general.u8", 0, struct.pack("<B", 7)), kv("general.i8", 1, struct.pack("<b", -7)),
        kv("general.u16", 2, struct.pack("<H", 7)), kv("general.i16", 3, struct.pack("<h", -7)),
        kv("general.u32", 4, struct.pack("<I", 7)), kv("general.i32", 5, struct.pack("<i", -7)),
        kv("general.f32", 6, struct.pack("<f", 0.5)), kv("general.bool", 7, struct.pack("<B", 1)),
        kv("general.u64", 10, struct.pack("<Q", 7)), kv("general.i64", 11, struct.pack("<q", -7)),
        kv("general.f64", 12, struct.pack("<d", 0.25)),
        # a string array that holds the key's own text, and its bytes after a wrong length
        kv("tokenizer.ggml.tokens", 9, struct.pack("<IQ", 8, 4) + s("a") + s("tokenizer.chat_template")
           + s("b" * 300) + s("\x00tokenizer.chat_template")),
        kv("tokenizer.ggml.scores", 9, struct.pack("<IQ", 6, 3) + struct.pack("<3f", 1, 2, 3)),
        kv("general.nested", 9, struct.pack("<IQ", 9, 2) + struct.pack("<IQ", 4, 2) + struct.pack("<2I", 1, 2)
           + struct.pack("<IQ", 8, 1) + s("x")),
        # a longer key that starts with the same bytes (llama.cpp has tokenizer.chat_template.<name>)
        kv("tokenizer.chat_template.tool_use", 8, s("{{ decoy }}")),
    ]
    if with_template:
        pairs.append(kv("tokenizer.chat_template", template_type,
                        s(template) if template_type == 8 else struct.pack("<I", 1)))
    pairs.append(kv("general.after", 8, s("tail")))
    head = b"GGUF" + struct.pack("<IQQ", 3, 0, len(pairs))
    path.write_bytes(head + b"".join(pairs) + b"\x00" * 64)


def bash_extract(gguf: Path, out: Path, sha: str | None = None, pod_dir: Path | None = None):
    fn = f'template_from_gguf "{gguf}" "{out}" {sha}' if sha else f'extract_template "{gguf}" "{out}"'
    script = f"set -u\nsource {GPU / 'pod_start.sh'}\n{fn}\necho rc=$?\n"
    env = dict(os.environ, POD_START_TEST="1", POD_DIR=str(pod_dir or out.parent))
    p = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
    return p.stdout + p.stderr


def test_reader_finds_the_template(tmp_path):
    g = tmp_path / "m.gguf"
    build_gguf(g)
    assert et.read_string_value(g) == TEMPLATE
    assert et.read_string_value(g, "tokenizer.chat_template.tool_use") == b"{{ decoy }}"
    assert et.read_string_value(g, "general.after") == b"tail"


def test_reader_errors(tmp_path):
    g = tmp_path / "m.gguf"
    build_gguf(g, with_template=False)
    with pytest.raises(et.GGUFError, match="no key"):
        et.read_string_value(g)
    build_gguf(g, template_type=4)
    with pytest.raises(et.GGUFError, match="not string"):
        et.read_string_value(g)
    g.write_bytes(b"GGML" + b"\x00" * 40)
    with pytest.raises(et.GGUFError, match="not a GGUF"):
        et.read_string_value(g)
    build_gguf(g)
    g.write_bytes(g.read_bytes()[:200])
    with pytest.raises(et.GGUFError, match="end of file"):
        et.read_string_value(g)


def test_cli_writes_only_on_a_matching_sha256(tmp_path):
    g, out = tmp_path / "m.gguf", tmp_path / "t.jinja"
    build_gguf(g)
    sha = hashlib.sha256(TEMPLATE).hexdigest()
    cmd = [sys.executable, "-I", str(GPU / "extract_template.py"), str(g), "--out", str(out)]
    p = subprocess.run(cmd + ["--sha256", "0" * 64], capture_output=True, text=True)
    assert p.returncode == 1 and not out.exists() and "nothing written" in p.stderr
    p = subprocess.run(cmd + ["--sha256", sha], capture_output=True, text=True)
    assert p.returncode == 0 and out.read_bytes() == TEMPLATE
    assert json.loads(p.stdout)["bytes"] == len(TEMPLATE)
    # default expected value: the run config's
    p = subprocess.run(cmd, capture_output=True, text=True)
    assert p.returncode == 1 and RUN1["model"]["chat_template"]["sha256"] in p.stderr


def test_pod_shell_extraction_matches_the_reader(tmp_path):
    g, out = tmp_path / "m.gguf", tmp_path / "bash.jinja"
    build_gguf(g)
    res = bash_extract(g, out)
    assert "rc=0" in res, res
    assert out.read_bytes() == et.read_string_value(g)


def test_pod_refuses_a_wrong_template(tmp_path):
    g, out = tmp_path / "m.gguf", tmp_path / "chat-template.jinja"
    build_gguf(g)
    res = bash_extract(g, out, "0" * 64, tmp_path)
    assert "rc=1" in res and "FATAL chat template sha256" in res, res
    assert not out.exists() and not Path(str(out) + ".part").exists()
    assert "chat template sha256" in (tmp_path / "logs" / "FATAL").read_text()
    (tmp_path / "logs" / "FATAL").unlink()
    res = bash_extract(g, out, hashlib.sha256(TEMPLATE).hexdigest(), tmp_path)
    assert "rc=0" in res and out.read_bytes() == TEMPLATE, res
    assert not (tmp_path / "logs" / "FATAL").exists()


def test_pod_refuses_a_file_without_template(tmp_path):
    g, out = tmp_path / "m.gguf", tmp_path / "chat-template.jinja"
    build_gguf(g, with_template=False)
    res = bash_extract(g, out, "0" * 64, tmp_path)
    assert "rc=1" in res and "no tokenizer.chat_template" in (tmp_path / "logs" / "FATAL").read_text()


@pytest.mark.skipif(not os.environ.get("GAPCHECK_GGUF_SHARD1"),
                    reason="set GAPCHECK_GGUF_SHARD1 to the first file of the pinned GGUF build")
def test_real_first_file(tmp_path):
    g = Path(os.environ["GAPCHECK_GGUF_SHARD1"])
    want = RUN1["model"]["chat_template"]
    data = et.read_string_value(g)
    assert hashlib.sha256(data).hexdigest() == want["sha256"] and len(data) == want["bytes"]
    out = tmp_path / "chat-template.jinja"
    res = bash_extract(g, out, want["sha256"], tmp_path)
    assert "rc=0" in res and out.read_bytes() == data, res
