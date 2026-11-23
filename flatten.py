#!/usr/bin/env python3
"""Flatten nested folders into flat category folders, trashing exact duplicates.

  flatten.py plan  ROOT SOURCE [SOURCE...] [--fast] [--spillover-log FILE]
  flatten.py apply [PLAN]
  flatten.py selftest

ROOT holds the flat category folders (Documents Music Other Pictures Videos;
created if missing). Each SOURCE is a folder under ROOT to flatten. It can be
an old backup folder, or a category folder itself if it has
subfolders. `plan` moves nothing: it writes plan.tsv in the current directory
and prints a summary. `apply` carries out a plan and then removes the source
folders it left empty.

Per source file:
  OS junk (.DS_Store, Thumbs.db, desktop.ini, ._*) -> trash
  exact copy of a file in the categories or another source file -> trash
  (name, size) listed in --spillover-log, a log of files already moved to an
  overflow drive ("moved <size> <name>" lines) -> trash
  otherwise -> moved into a category: Source/Cat/file keeps its name; anything
  else goes by extension and is renamed <parent>__<file>. Name clashes get " (2)".
  Empty files are never treated as duplicates. Paths containing a tab or
  newline are skipped. apply skips any file changed since the plan was made.

The trash is the freedesktop one for that filesystem (<mount>/.Trash-<uid>, or
~/.local/share/Trash on the home filesystem), inside one restorable
flatten-<date>_<time> folder that keeps the original paths. Nothing is overwritten.
Git repos (folders containing .git) and symlinks are skipped and left alone.
--fast hashes only size plus the first and last 1 MB, then byte-compares
any match in full before trashing it.
"""
import datetime, filecmp, hashlib, os, sys, tempfile
from collections import defaultdict

CATS = ["Documents", "Music", "Other", "Pictures", "Videos"]
EXT = {
    "Documents": "pdf docx doc txt md pptx ppt xlsx xls csv tsv epub rtf pages key numbers odt ods odp",
    "Music": "mp3 m4a wav flac ogg oga aac wma aiff aif",
    "Pictures": "png jpg jpeg heic webp gif bmp tiff tif svg raw cr2 nef aae",
    "Videos": "mp4 mov mkv avi webm flv wmv mpg mpeg m4v 3gp",
}
EXT = {e: cat for cat, exts in EXT.items() for e in exts.split()}
CHUNK = 1 << 20
JUNK = {".DS_Store", "Thumbs.db", "desktop.ini"}


def trash_dir(root):
    """Freedesktop trash on the same filesystem as root, so moves are plain renames."""
    home = os.path.expanduser("~")
    if os.stat(root).st_dev == os.stat(home).st_dev:
        return os.path.join(home, ".local/share/Trash")
    top = os.path.realpath(root)
    while not os.path.ismount(top):
        top = os.path.dirname(top)
    return os.path.join(top, f".Trash-{os.getuid()}")


def walk(path, skipped):
    for dp, dirs, fs in os.walk(path):
        if ".git" in dirs:
            skipped.append(dp)
            dirs.clear()
            continue
        for f in fs:
            p = os.path.join(dp, f)
            if "\t" in p or "\n" in p:
                skipped.append(p)
            elif not os.path.islink(p):
                yield p


_digests = {}  # one process may plan many times (the app): key on mode, size and mtime so a hash is never stale


def digest(path, fast):
    st = os.stat(path)
    key = (path, fast, st.st_size, st.st_mtime_ns)
    if key not in _digests:
        h = hashlib.blake2b()
        with open(path, "rb") as fh:
            if fast:
                h.update(fh.read(CHUNK))
                fh.seek(max(0, os.path.getsize(path) - CHUNK))
                h.update(fh.read(CHUNK))
            else:
                while chunk := fh.read(4 * CHUNK):
                    h.update(chunk)
        _digests[key] = h.hexdigest()
    return _digests[key]


def target(rel):
    """(category, filename) for a unique source file, rel = path relative to ROOT."""
    parts = rel.split(os.sep)
    if len(parts) == 3 and parts[1] in CATS and parts[0] not in CATS:
        return parts[1], parts[2]
    name = parts[-1]
    cat = EXT.get(os.path.splitext(name)[1].lower().lstrip("."), "Other")
    return cat, name if len(parts) == 2 else f"{parts[-2]}__{name}"


def plan(root, sources, fast=False, spillover_log=None, out="plan.tsv"):
    root = os.path.realpath(root)
    if root == "/":
        sys.exit("refusing to use / as ROOT")
    skipped = []
    cat_files = []
    for c in CATS:
        d = os.path.join(root, c)
        if os.path.isdir(d):
            cat_files += [os.path.join(d, f) for f in os.listdir(d)
                          if os.path.isfile(os.path.join(d, f)) and not os.path.islink(os.path.join(d, f))]
    src = []
    for s in sources:
        s = os.path.realpath(os.path.join(root, s))
        if not s.startswith(root + os.sep) or not os.path.isdir(s):
            sys.exit(f"source must be a folder inside ROOT: {s}")
        in_cat = os.path.relpath(s, root) in CATS
        src += [p for p in walk(s, skipped) if not (in_cat and os.path.dirname(p) == s)]
    src.sort()

    evicted = set()
    if spillover_log:
        for line in open(spillover_log, encoding="utf-8"):
            if line.startswith("moved "):
                _, n, name = line.rstrip("\n").split(" ", 2)
                evicted.add((name, int(n)))

    size = {p: os.path.getsize(p) for p in cat_files + src}
    count = defaultdict(int)
    for p in size:
        count[size[p]] += 1
    src_sizes = {size[p] for p in src}
    # Only files whose size collides with a source file get hashed. Nothing is
    # matched against the trash: its only copy would then live in the trash.
    known = defaultdict(list)  # hash -> [(reason, path)]
    for p in cat_files:
        if size[p] and size[p] in src_sizes and count[size[p]] > 1:
            known[digest(p, fast)].append(("dup-of-category", p))

    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    tbase = os.path.join(trash_dir(root), "files", f"flatten-{stamp}")
    taken = {p.lower() for p in cat_files}  # lowercased: NTFS/exFAT are case-insensitive
    rows = []
    for i, p in enumerate(src, 1):
        if i % 2000 == 0:
            print(f"  {i}/{len(src)}", file=sys.stderr)
        rel = os.path.relpath(p, root)
        cat, name = target(rel)
        trash_to = os.path.join(tbase, rel)
        st = os.stat(p)
        stamp = (str(st.st_size), str(st.st_mtime_ns))
        if os.path.basename(p) in JUNK or os.path.basename(p).startswith("._"):
            rows.append(("trash", "junk", p, trash_to) + stamp)
            continue
        if (name, size[p]) in evicted or (os.path.basename(p), size[p]) in evicted:
            rows.append(("trash", "on-spillover", p, trash_to) + stamp)
            continue
        if size[p] and count[size[p]] > 1:
            h = digest(p, fast)
            m = next((k for k in known[h] if not fast or filecmp.cmp(p, k[1], shallow=False)), None)
            if m:
                rows.append(("trash", f"{m[0]}:{m[1]}", p, trash_to) + stamp)
                continue
            known[h].append(("dup-within-sources", p))
        stem, ext = os.path.splitext(name)
        dst, n = os.path.join(root, cat, name), 2
        while dst.lower() in taken:
            dst, n = os.path.join(root, cat, f"{stem} ({n}){ext}"), n + 1
        taken.add(dst.lower())
        rows.append(("move", "unique", p, dst) + stamp)

    with open(out, "w", encoding="utf-8") as fh:
        fh.write(f"#root\t{root}\n#trash\t{tbase}\n")
        for s in sources:
            fh.write(f"#source\t{os.path.realpath(os.path.join(root, s))}\n")
        for r in rows:
            fh.write("\t".join(r) + "\n")
    tally = defaultdict(int)
    for a, reason, *_ in rows:
        tally[(a, reason.split(":")[0])] += 1
    for (a, reason), v in sorted(tally.items()):
        print(f"{v:8}  {a:5}  {reason}")
    for d in skipped:
        print(f"  skipped: {d}")
    print(f"{len(rows):8}  total -> {os.path.abspath(out)}")


def apply(plan_file="plan.tsv"):
    meta, sources, done = {}, [], 0
    for line in open(plan_file, encoding="utf-8"):
        a, b, *rest = line.rstrip("\n").split("\t")
        if a.startswith("#"):
            sources.append(b) if a == "#source" else meta.setdefault(a, b)
            continue
        src, dst, n, mtime = rest
        if not os.path.lexists(src):
            continue
        st = os.stat(src)
        if (st.st_size, st.st_mtime_ns) != (int(n), int(mtime)):
            print(f"  changed since plan, left alone: {src}")
            continue
        if os.path.lexists(dst):
            sys.exit(f"refusing to overwrite {dst}")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)
        done += 1
    tbase = meta["#trash"]
    info = os.path.join(os.path.dirname(os.path.dirname(tbase)), "info", os.path.basename(tbase) + ".trashinfo")
    if os.path.isdir(tbase) and not os.path.exists(info):
        os.makedirs(os.path.dirname(info), exist_ok=True)
        with open(info, "w") as fh:
            fh.write(f"[Trash Info]\nPath={os.path.join(meta['#root'], os.path.basename(tbase))}\n"
                     f"DeletionDate={datetime.datetime.now():%Y-%m-%dT%H:%M:%S}\n")
    for s in sources:  # remove folders left empty, deepest first; keep category folders
        for dp, _, _ in sorted(os.walk(s), key=lambda t: -len(t[0])):
            if dp == s and os.path.relpath(s, meta["#root"]) in CATS:
                continue
            try:
                os.rmdir(dp)
            except OSError:
                pass
    print(f"moved {done}")


def selftest():
    """Build a tiny tree in a temp dir, plan + apply, check the result."""
    global trash_dir
    real = trash_dir
    try:
        _selftest()
    finally:
        trash_dir = real
    print("selftest ok")


def _selftest():
    global trash_dir
    with tempfile.TemporaryDirectory() as t:
        trash_dir = lambda root: os.path.join(t, ".Trash-test")
        def w(rel, data):
            os.makedirs(os.path.dirname(os.path.join(t, rel)), exist_ok=True)
            open(os.path.join(t, rel), "w").write(data)
        w("Music/song.mp3", "AAA")
        w("Mirror/Music/song.mp3", "AAA")    # dup of category -> trash
        w("Mirror/Music/other.mp3", "BBB")   # unique -> Music/other.mp3
        w("Mirror/stuff/pic.jpg", "CCC")     # loose -> Pictures/stuff__pic.jpg
        w("Mirror/stuff/zcopy.jpg", "CCC")   # dup within sources -> trash
        w("Mirror/repo/.git/HEAD", "x")      # git repo -> left alone
        w("Documents/sub/a.txt", "DDD")      # nested category -> Documents/sub__a.txt
        w("Documents/top.txt", "EEE")        # already flat -> untouched
        w("Mirror/a/empty.txt", "")          # empty files are not dups of each other
        w("Mirror/b/empty.txt", "")
        w("Mirror/c/.DS_Store", "j")         # junk -> trash
        w("Mirror/d/only.jpg", "TTT")        # its only copy is in the trash -> still moved
        w(".Trash-test/files/old/only.jpg", "TTT")
        w("Mirror/f/v1.mov", "A" * (3 * CHUNK))  # same size, head and tail as v2
        w("Mirror/f/v2.mov", "A" * CHUNK + "B" * CHUNK + "A" * CHUNK)
        w("Nest/Videos/clip.mp4", "V")          # source named like a category -> still removed
        out = os.path.join(t, "plan.tsv")
        plan(t, ["Mirror", "Documents", "Nest/Videos"], fast=True, out=out)
        apply(out)
        ex = lambda rel: os.path.exists(os.path.join(t, rel))
        assert ex("Music/other.mp3") and ex("Pictures/stuff__pic.jpg")
        assert ex("Documents/sub__a.txt") and ex("Documents/top.txt")
        assert not ex("Documents/sub") and ex("Mirror/repo/.git/HEAD")
        assert not ex("Mirror/Music") and not ex("Mirror/stuff")
        assert not ex("Nest/Videos") and ex("Videos/clip.mp4") and ex("Documents")
        assert ex("Documents/a__empty.txt") and ex("Documents/b__empty.txt")
        assert ex("Pictures/d__only.jpg") and ex("Videos/f__v1.mov") and ex("Videos/f__v2.mov")
        tb = [d for d in os.listdir(os.path.join(t, ".Trash-test/files")) if d.startswith("flatten-")][0]
        trashed = sorted(f for _, _, fs in os.walk(os.path.join(t, ".Trash-test/files", tb)) for f in fs)
        assert trashed == [".DS_Store", "song.mp3", "zcopy.jpg"], trashed
        assert os.listdir(os.path.join(t, ".Trash-test/info"))
        v1, v2 = os.path.join(t, "Videos/f__v1.mov"), os.path.join(t, "Videos/f__v2.mov")
        assert digest(v1, True) == digest(v2, True) and digest(v1, False) != digest(v2, False)  # fast hash never reused
        st = os.stat(v1)
        open(v1, "w").write("Z" * (3 * CHUNK))
        os.utime(v1, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        assert digest(v1, False) != digest(v2, False) and digest(v1, True) != digest(v2, True)  # an edit is seen


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] not in ("plan", "apply", "selftest"):
        sys.exit(__doc__)
    if args[0] == "selftest":
        selftest()
    elif args[0] == "apply":
        apply(*args[1:2])
    else:
        fast = "--fast" in args
        args = [a for a in args if a != "--fast"]
        log = None
        if "--spillover-log" in args:
            i = args.index("--spillover-log")
            log = args[i + 1]
            del args[i:i + 2]
        if len(args) < 3:
            sys.exit(__doc__)
        plan(args[1], args[2:], fast=fast, spillover_log=log)
