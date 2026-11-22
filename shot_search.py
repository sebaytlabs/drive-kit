#!/usr/bin/env python3
"""Find a screenshot by a word that's in it. Reads the text out of every image in a folder once, then searches it.

  shot_search.py index FOLDER [--all-images]   read text from new or changed images (PNG only unless --all-images)
  shot_search.py find WORDS...                 list images whose text has all the words
  shot_search.py --selftest

PNG only by default. On a real photo library almost every PNG (screenshots) had text and almost no camera photo
did, so this skips hours of work on pictures that have none. The index is kept in ~/.shot-search/index.json and
only new or changed files are read again, so re-running it after adding screenshots is quick.

Needs one OCR engine:  pip install rapidocr_onnxruntime   (works offline, no system install)
                   or  tesseract on your PATH            (apt install tesseract-ocr / brew install tesseract)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

INDEX = Path(os.environ.get("SHOT_SEARCH_HOME", Path.home() / ".shot-search")) / "index.json"
PNG = {".png"}
ALL = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def engine():
    """Returns a function path -> text, or None when no OCR engine is installed."""
    try:
        from rapidocr_onnxruntime import RapidOCR
        ocr = RapidOCR()

        def rapid(path):
            result, _ = ocr(str(path))
            return "\n".join(r[1] for r in result or [])
        return rapid
    except ImportError:
        pass
    if shutil.which("tesseract"):
        def tess(path):
            r = subprocess.run(["tesseract", str(path), "-", "--psm", "3"], capture_output=True, text=True)
            return r.stdout
        return tess
    return None


def load():
    try:
        return json.loads(INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(idx):
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    tmp = INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, INDEX)


def images(folder, exts):
    for dp, dn, fn in os.walk(folder):
        dn[:] = [d for d in dn if not d.startswith(".")]
        for f in fn:
            if Path(f).suffix.lower() in exts:
                yield Path(dp, f).resolve()


def index(folder, read, exts=PNG, idx=None, every=25):
    idx = load() if idx is None else idx
    todo = []
    for p in images(folder, exts):
        st = p.stat()
        sig = f"{st.st_size}:{int(st.st_mtime)}"
        if idx.get(str(p), {}).get("sig") != sig:
            todo.append((p, sig))
    print(f"{len(todo)} images to read")
    for n, (p, sig) in enumerate(todo, 1):
        try:
            text = read(p)
        except Exception as e:   # one bad file shouldn't stop a long run
            print(f"  skipped {p.name}: {e}")
            continue
        idx[str(p)] = {"sig": sig, "text": " ".join(text.split())}
        if n % every == 0:
            save(idx)
            print(f"  {n}/{len(todo)}")
    save(idx)
    return idx


def find(words, idx=None):
    idx = load() if idx is None else idx
    words = [w.lower() for w in words]
    hits = []
    for path, rec in idx.items():
        t = rec["text"].lower()
        if all(w in t for w in words):
            i = t.find(words[0])
            hits.append((path, rec["text"][max(0, i - 40):i + 60]))
    return sorted(hits)


def selftest():
    with tempfile.TemporaryDirectory() as d:
        global INDEX
        INDEX = Path(d, "idx", "index.json")
        Path(d, "a.png").write_bytes(b"1")
        Path(d, "b.PNG").write_bytes(b"22")
        Path(d, "c.jpg").write_bytes(b"3")
        fake = {"a.png": "Your order has shipped", "b.PNG": "Meeting at noon", "c.jpg": "photo"}
        calls = []

        def read(p):
            calls.append(p.name)
            return fake[p.name]
        idx = index(d, read)
        assert sorted(calls) == ["a.png", "b.PNG"], calls
        index(d, read)
        assert len(calls) == 2, "unchanged files must not be read again"
        assert [Path(p).name for p, _ in find(["ORDER", "shipped"])] == ["a.png"]
        assert find(["order", "noon"]) == []
        index(d, read, ALL, idx)
        assert "c.jpg" in calls
    print("selftest ok")


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        sys.exit(__doc__)
    if argv[0] == "--selftest":
        return selftest()
    if argv[0] == "index" and len(argv) >= 2:
        read = engine()
        if not read:
            sys.exit("No OCR engine found. Run:  pip install rapidocr_onnxruntime")
        index(argv[1], read, ALL if "--all-images" in argv else PNG)
    elif argv[0] == "find" and len(argv) >= 2:
        hits = find(argv[1:])
        for path, snip in hits:
            print(f"{path}\n    ...{snip}...")
        print(f"{len(hits)} found")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
