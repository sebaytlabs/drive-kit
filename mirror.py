#!/usr/bin/env python3
"""Copy everything from one drive (or folder) to another that isn't there yet. Compares by content. Never deletes.

  mirror.py SOURCE DEST                  dry run: shows what it would copy, changes nothing
  mirror.py SOURCE DEST --apply          copies, checks every copy byte for byte (sha256), logs it
  options:
    --only NAME [NAME ...]   top-level folders of SOURCE to include (default: all)
    --skip NAME [NAME ...]   folder names to skip anywhere (added to the usual system folders)
    --cap PERCENT            stop before DEST gets fuller than this (default 80)
    --selftest

A file is skipped when DEST already has it at the same path with the same size, or has the same content
anywhere (so files you moved or renamed on DEST don't get copied twice). Otherwise it goes to the same path on DEST.
If that path holds a different file, the copy is named "name (from SOURCE).ext" (then "(from SOURCE 2)", ...).
Nothing is overwritten. Each copy is written beside its place under a temporary name and only takes its real
name once it's complete, so a copy cut short never looks like a finished file.

Names are made safe for exFAT/NTFS drives (: * ? " < > | become -), so a Linux drive can mirror to a drive
that Windows and Mac can read. Log: DEST/logs/mirror-<date>.tsv
"""
import collections
import datetime
import hashlib
import os
import shutil
import sys
import tempfile

SYSTEM = {"$RECYCLE.BIN", "System Volume Information", "lost+found", ".Trash-1000", ".Trashes",
          ".Spotlight-V100", ".fseventsd", "timeshift", "logs"}
BAD_CHARS = str.maketrans({c: "-" for c in ':*?"<>|'})
PART = ".mirror-part"  # a copy in progress: ".name.mirror-part" next to where it goes


def safe(rel):
    return rel.translate(BAD_CHARS)


def free_name(dr, taken, tag):
    """dr, or "name (from tag).ext", "name (from tag 2).ext"... whichever isn't taken. Compared without case:
    exFAT and NTFS drives treat File.txt and file.txt as one name."""
    base, ext = os.path.splitext(dr)
    n = 0
    while dr.lower() in taken:
        n += 1
        dr = f"{base} (from {tag}{'' if n == 1 else f' {n}'}){ext}"
    return dr


def part_path(d):
    return os.path.join(os.path.dirname(d), "." + os.path.basename(d) + PART)


def walk(root, only, skip):
    tops = only or sorted(os.listdir(root))
    for top in tops:
        if top in skip:
            continue
        p0 = os.path.join(root, top)
        if os.path.isfile(p0) and not os.path.islink(p0):
            yield top
            continue
        for dp, dn, fn in os.walk(p0):
            dn[:] = sorted(d for d in dn if d not in skip)
            for f in sorted(fn):
                p = os.path.join(dp, f)
                if not os.path.islink(p) and os.path.isfile(p):
                    yield os.path.relpath(p, root)


class Hasher:
    def __init__(self):
        self.cache = {}

    def __call__(self, p):
        if p not in self.cache:
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for c in iter(lambda: f.read(1 << 22), b""):
                    h.update(c)
            self.cache[p] = h.hexdigest()
        return self.cache[p]


def plan(src, dst, only=None, skip=frozenset()):
    """Returns (stats, [(source rel, dest rel, size)])."""
    skip = SYSTEM | set(skip)
    sha = Hasher()
    by_size, have = collections.defaultdict(list), {}
    for r in walk(dst, None, skip):
        try:
            s = os.path.getsize(os.path.join(dst, r))
        except OSError:
            continue
        if r.endswith(PART):
            continue  # a copy cut short last time: it's written again, never counted as there
        by_size[s].append(r)
        have[r] = s
    taken = {r.lower() for r in have}
    stats, todo = collections.Counter(), []
    tag = os.path.basename(os.path.normpath(src)) or "source"
    for r in walk(src, only, skip):
        sp = os.path.join(src, r)
        try:
            s = os.path.getsize(sp)
        except OSError:
            stats["unreadable"] += 1
            continue
        dr = safe(r)
        if have.get(dr) == s:
            stats["already there"] += 1
            continue
        if any(sha(os.path.join(dst, c)) == sha(sp) for c in by_size.get(s, [])):
            stats["already there under another name"] += 1
            continue
        dr = free_name(dr, taken, tag)  # a different file there, or one this run already sends there
        taken.add(dr.lower())
        todo.append((r, dr, s))
        stats["to copy"] += 1
    return stats, todo


def fullness_after(dst, extra):
    u = shutil.disk_usage(dst)
    return 100 * (u.used + extra) / u.total


def apply(src, dst, todo, cap):
    os.makedirs(os.path.join(dst, "logs"), exist_ok=True)
    log = os.path.join(dst, "logs", f"mirror-{datetime.date.today()}.tsv")
    new = not os.path.exists(log)
    sha, copied, bad = Hasher(), 0, 0
    with open(log, "a", encoding="utf-8") as L:
        if new:
            L.write("result\tsource\tdest\tsha256\n")
        for r, dr, s in todo:
            if fullness_after(dst, s) > cap:
                print(f"STOPPED: copying {dr} would put {dst} over {cap}% full. {len(todo) - copied} files left.")
                break
            d = os.path.join(dst, dr)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            part = part_path(d)
            shutil.copy2(os.path.join(src, r), part)
            if os.path.exists(d):  # appeared since the plan was made: leave it be
                os.remove(part)
                L.write(f"SKIPPED (now taken)\t{r}\t{dr}\t\n")
                copied += 1
                continue
            os.replace(part, d)
            ok = sha(d) == sha(os.path.join(src, r))
            bad += not ok
            copied += 1
            L.write(f"{'copied' if ok else 'MISMATCH'}\t{r}\t{dr}\t{sha(d)}\n")
            L.flush()
    print(f"Copied {copied} files, {bad} mismatches. Log: {log}")
    return copied, bad


def report(stats, todo, dst, cap):
    total = sum(t[2] for t in todo)
    print(", ".join(f"{k}: {v}" for k, v in stats.items()) or "nothing found")
    print(f"To copy: {len(todo)} files, {total / 1e9:.2f} GB. {dst} would be "
          f"{fullness_after(dst, total):.0f}% full (cap {cap}%).")
    tops = collections.Counter()
    for r, _, s in todo:
        tops[r.split(os.sep)[0]] += s
    for k, v in tops.most_common(15):
        print(f"  {v / 1e9:8.2f} GB  {k}")


def selftest():
    with tempfile.TemporaryDirectory() as t:
        a, b = os.path.join(t, "A"), os.path.join(t, "B")
        colon = os.name != "nt"  # Windows can't name a file with ':' (it would write a hidden stream instead)
        files = [("A/docs/x.txt", "one"), ("A/same.txt", "same"), ("A/clash.txt", "new"), ("A/lost+found/junk", "j"),
                 ("B/moved/renamed.txt", "same"), ("B/clash.txt", "old!")] + colon * [("A/docs/what: now.txt", "two")]
        for p, data in files:
            os.makedirs(os.path.dirname(os.path.join(t, p)), exist_ok=True)
            with open(os.path.join(t, p), "w") as f:
                f.write(data)
        stats, todo = plan(a, b)
        got = sorted(d for _, d, _ in todo)
        want = ["clash (from A).txt", os.path.join("docs", "x.txt")] + colon * [os.path.join("docs", "what- now.txt")]
        assert got == sorted(want), got
        assert stats["already there under another name"] == 1
        apply(a, b, todo, cap=100)
        assert open(os.path.join(b, "clash.txt")).read() == "old!"
        assert plan(a, b)[1] == []
        _, todo = plan(a, b, only=["docs"])
        assert todo == []
        assert not [f for _, _, fs in os.walk(b) for f in fs if f.endswith(PART)]
        # never overwritten: a "(from A)" name already holding something else, and two names that become one
        w = lambda p, data: open(os.path.join(t, p), "w").write(data)
        w("A/clash.txt", "newer")
        w("B/clash (from A).txt", "taken!")
        w("A/docs/x.txt", "changed")
        if colon:
            w("A/docs/a-b.txt", "dash")
            w("A/docs/a:b.txt", "colon")
        stats, todo = plan(a, b)
        got = sorted(d for _, d, _ in todo)
        want = ["clash (from A 2).txt", os.path.join("docs", "x (from A).txt")] + colon * [
            os.path.join("docs", "a-b.txt"), os.path.join("docs", "a-b (from A).txt")]
        assert got == sorted(want), got
        apply(a, b, todo, cap=100)
        assert open(os.path.join(b, "clash (from A).txt")).read() == "taken!"
        if colon:
            assert sorted(open(os.path.join(b, "docs", n)).read() for n in ("a-b.txt", "a-b (from A).txt")) == \
                ["colon", "dash"]
        open(part_path(os.path.join(b, "cut.txt")), "w").write("half")  # a copy cut short is ignored, then redone
        w("A/cut.txt", "whole")
        assert [d for _, d, _ in plan(a, b)[1]] == ["cut.txt"]
    print("selftest ok")


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="Copy what's missing from SOURCE to DEST. Never deletes.")
    ap.add_argument("source", nargs="?")
    ap.add_argument("dest", nargs="?")
    ap.add_argument("--apply", action="store_true", help="copy (default is a dry run)")
    ap.add_argument("--only", nargs="+", default=[], metavar="NAME", help="top-level folders to include")
    ap.add_argument("--skip", nargs="+", default=[], metavar="NAME", help="folder names to skip")
    ap.add_argument("--cap", type=float, default=80, help="max percent full for DEST (default 80)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not (a.source and a.dest):
        ap.error("need SOURCE and DEST")
    stats, todo = plan(a.source, a.dest, a.only or None, a.skip)
    report(stats, todo, a.dest, a.cap)
    if a.apply and todo:
        apply(a.source, a.dest, todo, a.cap)
    elif todo:
        print("Dry run. Add --apply to copy.")


if __name__ == "__main__":
    main(sys.argv[1:])
