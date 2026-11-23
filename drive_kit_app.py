#!/usr/bin/env python3
"""Drive Kit app: back up a drive, tidy a messy folder, sort files, find a screenshot, record the screen.

  python3 drive_kit_app.py

Every page says what it does and asks before it changes anything. Nothing is ever deleted for good:
what's removed goes to the Trash.
"""
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import flatten
import mirror
import shot_search
from appkit import BG, DIM, FG, FONT, FROZEN, App, Capture, Job, Page, handle_cli, open_path, write

ACCENT = "#2b8a3e"
HERE = Path(__file__).resolve().parent
LINUX = sys.platform.startswith("linux")
ABOUT = ("Drive Kit\n\nKeep your files on your own drives. One source of truth, as many backups as you like.\n"
         "Nothing here deletes for good: what's removed goes to the Trash.\n\nFree and open source (MIT), "
         "by Sebayt Labs.")


def captured(fn):
    """Run fn() with its print output sent to the job log."""
    def run(log):
        cap = Capture(log)
        with contextlib.redirect_stdout(cap), contextlib.redirect_stderr(cap):
            try:
                return fn(log)
            finally:
                cap.flush()
    return run


# ------------------------------------------------------------------ Back up
class BackupPage(Page):
    HELP = ("Back up\n\nCopies whatever is missing from one drive or folder to another.\n\n"
            "1. Copy from: your main drive or folder.\n2. Copy to: the backup drive.\n"
            "3. Press Check. Nothing is copied yet; you see how much would be copied and how full the backup gets.\n"
            "4. Press Copy now.\n\nIt compares by content, so files you renamed or moved on the backup aren't copied "
            "again. Nothing on the backup is overwritten or deleted. Each copy is checked byte for byte and logged "
            "in the backup's logs folder. It stops before the backup gets fuller than the limit.")
    HINT = "Pick both drives, then press Check. Nothing is copied until you press Copy now."

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.title("Back up a drive", "Copies what's missing from one drive to another. Never deletes, never "
                                      "overwrites, checks every copy.")
        self.steps(["Pick what to copy from, and the backup drive to copy to.",
                    "Press Check to see what would be copied. Nothing changes yet.",
                    "Press Copy now. Run it again any time; only new files are copied."])
        self.src, self.dst, self.cap = tk.StringVar(), tk.StringVar(), tk.IntVar(value=80)
        self.fields = tk.Frame(self.body, bg=BG)
        self.fields.pack(fill="x")
        self.path_field("Copy from", self.src, parent=self.fields)
        self.path_field("Copy to (backup)", self.dst, parent=self.fields)
        r = self.row()
        tk.Label(r, text="Stop at", bg=BG, fg=FG, font=(FONT, 11), width=16, anchor="w").pack(side="left")
        tk.Spinbox(r, from_=50, to=98, textvariable=self.cap, width=5, font=(FONT, 11)).pack(side="left")
        tk.Label(r, text="% full (leaves room for the next backup)", bg=BG, fg=DIM,
                 font=(FONT, 10)).pack(side="left", padx=8)
        r = self.row(pady=10)
        self.check_btn = self.button(r, "Check", self.check)
        self.check_btn.pack(side="left")
        self.copy_btn = self.button(r, "Copy now", self.copy, state="disabled")
        self.copy_btn.pack(side="left", padx=8)
        self.log = self.log_box()
        self.todo = None

    def ok_paths(self):
        s, d = self.src.get(), self.dst.get()
        if not (os.path.isdir(s) and os.path.isdir(d)):
            messagebox.showinfo("Back up", "Pick both folders first.")
            return False
        if os.path.realpath(s) == os.path.realpath(d):
            messagebox.showinfo("Back up", "Copy from and Copy to are the same place.")
            return False
        return True

    def check(self):
        if not self.ok_paths():
            return
        self.check_btn.config(state="disabled")
        self.copy_btn.config(state="disabled")
        write(self.log, "Comparing the two drives. Big drives take a while…\n")

        src, dst, cap = self.src.get(), self.dst.get(), self.cap.get()

        def work(log):
            stats, todo = mirror.plan(src, dst)
            mirror.report(stats, todo, dst, cap)
            return todo

        def done(todo):
            self.check_btn.config(state="normal")
            if isinstance(todo, BaseException):
                write(self.log, f"Couldn't check: {todo}\n")
                return
            self.todo = todo
            if todo:
                self.copy_btn.config(state="normal")
                write(self.log, "Press Copy now to copy these.\n")
            else:
                write(self.log, "Nothing to copy: the backup already has everything.\n")
        Job(self, self.log, captured(work), done)

    def copy(self):
        if not self.todo:
            return
        self.copy_btn.config(state="disabled")
        self.check_btn.config(state="disabled")
        write(self.log, f"Copying {len(self.todo)} files…\n")

        def done(r):
            self.check_btn.config(state="normal")
            write(self.log, f"Problem: {r}\n" if isinstance(r, BaseException) else "Done.\n")
            self.todo = None
        src, dst, cap, todo = self.src.get(), self.dst.get(), self.cap.get(), self.todo
        Job(self, self.log, captured(lambda log: mirror.apply(src, dst, todo, cap)), done)


# ------------------------------------------------------------------ Tidy
class TidyPage(Page):
    HELP = ("Tidy a folder\n\nTurns deep, messy folders into five flat ones: Documents, Music, Pictures, Videos "
            "and Other.\n\n1. Main folder: where the five folders live (they're made if missing).\n"
            "2. Add the messy folders inside it to tidy.\n3. Press Make a plan. Nothing moves yet; you see what "
            "would happen.\n4. Press Do it.\n\nFiles that came from a subfolder are renamed <old folder>__<file>, so "
            "you can always tell where something came from. Exact duplicates and system junk go to the Trash, in "
            "one dated folder you can restore from. Git projects are left alone.")
    HINT = "Pick the main folder, add the messy folders, then press Make a plan."

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.title("Tidy a folder", "Deep, messy folders become five flat ones. Duplicates go to the Trash.")
        self.steps(["Pick the main folder. The five folders (Documents, Music, Pictures, Videos, Other) go here.",
                    "Add the messy folders inside it that you want tidied.",
                    "Press Make a plan and read it. Then press Do it."])
        self.root = tk.StringVar()
        self.fields = tk.Frame(self.body, bg=BG)
        self.fields.pack(fill="x")
        self.path_field("Main folder", self.root, parent=self.fields)
        r = self.row(self.fields)
        tk.Label(r, text="Folders to tidy", bg=BG, fg=FG, font=(FONT, 11), width=16, anchor="nw").pack(side="left",
                                                                                                    anchor="n")
        self.list = tk.Listbox(r, height=4, font=(FONT, 11), relief="solid", bd=1)
        self.list.pack(side="left", fill="x", expand=True)
        col = tk.Frame(r, bg=BG)
        col.pack(side="left", padx=8, anchor="n")
        self.button(col, "Add…", self.add, primary=False).pack(fill="x")
        self.button(col, "Remove", lambda: [self.list.delete(i) for i in reversed(self.list.curselection())],
                    primary=False).pack(fill="x", pady=4)
        self.fast = tk.BooleanVar(value=True)
        tk.Checkbutton(self.body, text="Fast duplicate check (any match is still compared in full before it's "
                                       "trashed)", variable=self.fast, bg=BG, font=(FONT, 10)).pack(anchor="w")
        r = self.row(pady=8)
        self.plan_btn = self.button(r, "Make a plan", self.make_plan)
        self.plan_btn.pack(side="left")
        self.do_btn = self.button(r, "Do it", self.do_it, state="disabled")
        self.do_btn.pack(side="left", padx=8)
        self.log = self.log_box(9)
        self.plan_file = Path(tempfile.gettempdir()) / f"drive-kit-plan-{os.getuid() if hasattr(os, 'getuid') else 0}.tsv"

    def add(self):
        p = filedialog.askdirectory(initialdir=self.root.get() or None)
        if p:
            self.list.insert("end", p)

    def make_plan(self):
        root, srcs = self.root.get(), list(self.list.get(0, "end"))
        if not (os.path.isdir(root) and srcs):
            messagebox.showinfo("Tidy", "Pick the main folder and add at least one folder to tidy.")
            return
        outside = [s for s in srcs if not os.path.realpath(s).startswith(os.path.realpath(root) + os.sep)]
        if outside:
            messagebox.showinfo("Tidy", "The folders to tidy must be inside the main folder:\n" + "\n".join(outside))
            return
        self.plan_btn.config(state="disabled")
        write(self.log, "Making a plan. Nothing is moved yet…\n")

        def done(r):
            self.plan_btn.config(state="normal")
            if isinstance(r, BaseException):
                write(self.log, f"Couldn't make a plan: {r}\n")
            else:
                write(self.log, "That's the plan. Press Do it to carry it out.\n")
                self.do_btn.config(state="normal")
        fast, out = self.fast.get(), str(self.plan_file)
        Job(self, self.log, captured(lambda log: flatten.plan(root, srcs, fast=fast, out=out)), done)

    def do_it(self):
        if not messagebox.askyesno("Tidy", "Move the files as planned? Duplicates go to the Trash, where you can "
                                           "restore them."):
            return
        self.do_btn.config(state="disabled")

        def done(r):
            write(self.log, f"Problem: {r}\n" if isinstance(r, BaseException) else "Done.\n")
        Job(self, self.log, captured(lambda log: flatten.apply(str(self.plan_file))), done)


# ------------------------------------------------------------------ Sort
class SortPage(Page):
    HELP = ("Sort files\n\nOpens the sorter in its own window. Pick a folder, then look at each file full size:\n"
            "K keeps it (moves it into a Kept folder inside the one you picked)\nD sends it to the Trash\n"
            "U undoes the last one\nLeft/Right browse, Space plays a video, O opens the file in its own app.\n\n"
            "It remembers where you stopped. Linux only for now.")
    HINT = "Press Open the sorter, then pick a folder in it."

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.title("Sort files", "Look at each picture, video or document full size and decide: keep or delete.")
        self.steps(["Press Open the sorter. It opens in its own window.",
                    "Pick a folder in it.",
                    "K keeps, D sends to the Trash, U undoes. Left and Right browse."])
        r = self.row(pady=10)
        self.open_btn = self.button(r, "Open the sorter", self.open_sorter, state="normal" if LINUX else "disabled")
        self.open_btn.pack(side="left")
        if not LINUX:
            self.text("The sorter needs Linux (GTK) for now.", 11, DIM)
        self.text("It remembers the last folder and file. If it opens on a folder you don't want, press "
                  "'Choose folder' in its window.", 11, DIM, pady=(14, 0))

    def open_sorter(self):
        # The sorter is GTK, which a download build doesn't carry, so it runs on the system's own Python there.
        python = shutil.which("python3") if FROZEN else sys.executable
        if not python:
            messagebox.showinfo("Sort", "The sorter needs Python 3 with GTK: sudo apt install python3-gi "
                                        "gir1.2-gtk-3.0 gir1.2-gst-plugins-base-1.0 gstreamer1.0-plugins-good")
            return
        subprocess.Popen([python, str(HERE / "sorter.py")])
        self.app.say("The sorter is opening in its own window.")


# ------------------------------------------------------------------ Find screenshot
class FindPage(Page):
    HELP = ("Find a screenshot\n\n1. Pick your screenshots folder and press Read the text (once; later runs only "
            "read new pictures).\n2. Type words you remember seeing and press Search.\n3. Double-click a result to "
            "open it.\n\nOnly PNG files are read unless you tick 'every picture'. Camera photos rarely have text, "
            "so skipping them saves hours. Reading text needs an OCR engine: the page offers to set one up.")
    HINT = "Read the text in a folder once, then search for any word you remember."

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.title("Find a screenshot", "Find a screenshot by a word that was on the screen.")
        self.steps(["Pick your screenshots folder and press Read the text. Once is enough; later only new ones are read.",
                    "Type words you remember and press Search.",
                    "Double-click a result to open it."])
        self.folder = tk.StringVar(value=str(Path.home() / "Pictures"))
        self.path_field("Screenshots folder", self.folder)
        self.every = tk.BooleanVar(value=False)
        r = self.row()
        self.read_btn = self.button(r, "Read the text", self.read)
        self.read_btn.pack(side="left")
        tk.Checkbutton(r, text="every picture, not just PNG screenshots", variable=self.every, bg=BG,
                       font=(FONT, 10)).pack(side="left", padx=10)
        r = self.row(pady=(14, 4))
        tk.Label(r, text="Search for", bg=BG, fg=FG, font=(FONT, 11), width=16, anchor="w").pack(side="left")
        self.q = tk.Entry(r, font=(FONT, 12), relief="solid", bd=1)
        self.q.pack(side="left", fill="x", expand=True, ipady=4)
        self.q.bind("<Return>", lambda e: self.search())
        self.button(r, "Search", self.search).pack(side="left", padx=8)
        self.search_row = r
        self.results = tk.Listbox(self.body, font=(FONT, 11), relief="solid", bd=1, height=6)
        self.results.pack(fill="both", expand=True, pady=6)
        self.results.bind("<Double-Button-1>", self.open_hit)
        self.log = self.log_box(3)
        self.hits = []
        n = len(shot_search.load())
        write(self.log, f"{n} pictures read so far.\n" if n else "No pictures read yet.\n")

    def read(self):
        if not os.path.isdir(self.folder.get()):
            messagebox.showinfo("Find", "Pick a folder first.")
            return
        engine = shot_search.engine()
        if not engine and FROZEN:  # the download carries rapidocr; this only happens if it couldn't load
            write(self.log, "The text reader couldn't start. Install tesseract (apt install tesseract-ocr / "
                            "brew install tesseract / winget install UB-Mannheim.TesseractOCR) and try again.\n")
            return
        if not engine:
            if not messagebox.askyesno("Find", "Reading text needs an OCR engine (rapidocr, about 60 MB). "
                                               "Install it now?"):
                return
            write(self.log, "Installing the OCR engine…\n")
            r = subprocess.run([sys.executable, "-m", "pip", "install", "--user", "rapidocr_onnxruntime"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                write(self.log, "Couldn't install it here. In a terminal run:\n  pip install rapidocr_onnxruntime\n"
                                "or install tesseract (apt install tesseract-ocr / brew install tesseract).\n")
                return
            engine = shot_search.engine()
        self.read_btn.config(state="disabled")
        exts = shot_search.ALL if self.every.get() else shot_search.PNG

        def done(r):
            self.read_btn.config(state="normal")
            write(self.log, f"Problem: {r}\n" if isinstance(r, BaseException) else "Done. Search away.\n")
        folder = self.folder.get()
        Job(self, self.log, captured(lambda log: shot_search.index(folder, engine, exts)), done)

    def search(self):
        words = self.q.get().split()
        if not words:
            return
        self.hits = shot_search.find(words)
        self.results.delete(0, "end")
        for path, snip in self.hits:
            self.results.insert("end", f"{Path(path).name}   …{snip}…")
        self.app.say(f"{len(self.hits)} found. Double-click one to open it." if self.hits else
                     "Nothing found. Try fewer or different words, or read the folder first.")

    def open_hit(self, _):
        sel = self.results.curselection()
        if sel:
            open_path(self.hits[sel[0]][0])


# ------------------------------------------------------------------ Record
class RecordPage(Page):
    HELP = ("Record the screen\n\nRecord a window: press it, then click the window you want.\nRecord the whole "
            "screen: starts straight away.\nPress Stop when you're done. The file goes in the folder shown, named by "
            "the date and time.\n\nIt matches your screen's refresh rate (up to 60 fps), uses your graphics card when "
            "it can, and writes a file that survives a crash. Linux (X11).")
    HINT = "Pick a window or the whole screen. Press Stop when you're done."

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.proc = None
        self.title("Record the screen", "A window or the whole screen, with the computer's sound.")
        self.steps(["Choose: one window, or the whole screen.",
                    "For a window, click on it when asked.",
                    "Press Stop when you're done. The recording is saved in the folder below."])
        self.out = tk.StringVar(value=str(Path.home() / "Videos"))
        self.path_field("Save recordings in", self.out)
        self.audio = tk.BooleanVar(value=True)
        tk.Checkbutton(self.body, text="Include the computer's sound", variable=self.audio, bg=BG,
                       font=(FONT, 11)).pack(anchor="w", pady=4)
        r = self.buttons = self.row(pady=10)
        ok = LINUX and shutil.which("ffmpeg")
        self.win_btn = self.button(r, "Record a window", lambda: self.start("--window"),
                                   state="normal" if ok else "disabled")
        self.win_btn.pack(side="left")
        self.full_btn = self.button(r, "Record the whole screen", lambda: self.start("--full"),
                                    state="normal" if ok else "disabled")
        self.full_btn.pack(side="left", padx=8)
        self.stop_btn = tk.Button(r, text="■  Stop", command=self.stop, bg="#e4e7ec", fg=DIM, relief="flat",
                                  activebackground="#c92a2a", activeforeground="white", disabledforeground="#adb5bd",
                                  font=(FONT, 11, "bold"), padx=16, pady=7, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.button(r, "Open the folder", lambda: open_path(self.out.get()), primary=False).pack(side="right")
        self.state = self.text("" if ok else "Needs Linux with ffmpeg (sudo apt install ffmpeg x11-utils).", 13,
                               bold=True)

    def start(self, mode):
        Path(self.out.get()).mkdir(parents=True, exist_ok=True)
        cmd = ["bash", str(HERE / "record.sh"), mode, "--out", self.out.get()] + ([] if self.audio.get() else
                                                                                   ["--no-audio"])
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, start_new_session=True)
        for b in (self.win_btn, self.full_btn):
            b.config(state="disabled")
        self.stop_btn.config(state="normal", bg="#e03131", fg="white")
        self.state.config(text="Click the window to record…" if mode == "--window" else "● Recording", fg="#e03131")
        if mode == "--window":
            self.after(1500, lambda: self.proc and self.proc.poll() is None and
                       self.state.config(text="● Recording (after you click the window)"))
        self.watch()

    def watch(self):
        if self.proc and self.proc.poll() is None:
            self.after(500, self.watch)
        elif self.proc:
            self.finish()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGINT)   # ffmpeg finishes the file cleanly on Ctrl-C

    def finish(self):
        self.proc = None
        for b in (self.win_btn, self.full_btn):
            b.config(state="normal")
        self.stop_btn.config(state="disabled", bg="#e4e7ec", fg=DIM)
        self.state.config(text=f"Saved in {self.out.get()}", fg=FG)


# ------------------------------------------------------------------ the tutorial
PRACTICE = Path.home() / "Drive Kit practice"


def make_practice():
    """Small throwaway folders for the tutorial, so nobody learns on their real drives."""
    files = {
        "Main/Documents/taxes 2025.txt": "practice file\n",
        "Main/Documents/recipes/soup.txt": "two onions, one pot\n",
        "Main/Pictures/beach.txt": "pretend this is a photo\n",
        "Main/Music/song.txt": "la la la\n",
        "Backup/Documents/taxes 2025.txt": "practice file\n",          # the backup already has this one
        "Backup/Old/soup (renamed).txt": "two onions, one pot\n",     # and this one, under another name
        "Tidy/Old laptop/Desktop/New folder/letter.txt": "Dear bank,\n",
        "Tidy/Old laptop/Desktop/New folder (2)/letter copy.txt": "Dear bank,\n",  # an exact duplicate
        "Tidy/Old laptop/.DS_Store": "junk",
        "Tidy/Phone dump/DCIM/100APPLE/IMG_0001.jpg": "pretend photo",
        "Tidy/Phone dump/Voice memos/memo 1.m4a": "pretend audio",
    }
    for rel, text in files.items():
        f = PRACTICE / rel
        if not f.exists():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
    return PRACTICE


def tutorial(app):
    pg = app.page
    log = lambda key: pg(key).log.get("1.0", "end")
    grew = lambda key, words: (lambda old: any(w in log(key)[len(old.rstrip()):] for w in words))

    def fill_backup():
        make_practice()
        p = pg("backup")
        p.src.set(str(PRACTICE / "Main"))
        p.dst.set(str(PRACTICE / "Backup"))
        return log("backup")

    def fill_tidy():
        make_practice()
        p = pg("tidy")
        p.root.set(str(PRACTICE / "Tidy"))
        p.list.delete(0, "end")
        for d in ("Old laptop", "Phone dump"):
            if (PRACTICE / "Tidy" / d).exists():
                p.list.insert("end", str(PRACTICE / "Tidy" / d))
        return log("tidy")

    return [
        {"title": "Welcome to Drive Kit", "target": lambda: app.tour_button,
         "text": "Drive Kit is for people who keep their files on their own drives. One rule runs through all of "
                 "it: one source of truth, as many backups as you like. A backup is a copy you only read, so it can't "
                 "drift. Nothing here deletes for good. What's removed goes to the Trash.\n\nYou'll practice on "
                 "small throwaway folders, not your real files. About four minutes.",
         "do": "click Next."},
        {"title": "Back up: pick two folders", "page": "backup", "target": lambda: pg("backup").fields,
         "text": f"Copy from is the drive you work on. Copy to is the backup. We've filled in two practice folders "
                 f"in '{PRACTICE.name}' in your home folder. For real use, press Browse.",
         "do": "look at the two folders, then click Next.", "start": fill_backup},
        {"title": "Check first", "page": "backup", "target": lambda: pg("backup").check_btn,
         "text": "Check compares the two by content, not by name, and copies nothing yet. The practice backup "
                 "already has the soup recipe under another name, so it won't be copied twice.",
         "do": "click Check and read what it would copy.",
         "start": lambda: log("backup"), "check": grew("backup", ["Press Copy now", "Nothing to copy"])},
        {"title": "Copy now", "page": "backup", "target": lambda: pg("backup").copy_btn,
         "text": "Each copy is checked byte for byte and listed in a log on the backup. A file that would land on a "
                 "different file with the same name gets ' (from Main)' added instead of overwriting.",
         "do": "click Copy now.", "start": lambda: log("backup"), "check": grew("backup", ["Done."])},
        {"title": "Run it again any time", "page": "backup", "target": lambda: pg("backup").check_btn,
         "text": "Only new files are ever copied, so you can run it after every work session. It also stops before "
                 "the backup gets too full.",
         "do": "click Check again. This time there's nothing to copy.",
         "start": lambda: log("backup"), "check": grew("backup", ["Nothing to copy"])},
        {"title": "Tidy a folder", "page": "tidy", "target": lambda: pg("tidy").fields,
         "text": "Old laptops and phone dumps leave folders inside folders inside folders. Tidy turns them into five "
                 "flat folders: Documents, Music, Pictures, Videos and Other. We've filled in a practice mess.",
         "do": "click Next.", "start": fill_tidy},
        {"title": "Make a plan", "page": "tidy", "target": lambda: pg("tidy").plan_btn,
         "text": "The plan lists every move first, and nothing moves yet. A file from a subfolder keeps where it came "
                 "from in its name, like 'New folder__letter.txt'. Exact duplicates and junk like .DS_Store go to "
                 "the Trash.",
         "do": "click Make a plan and read it.", "start": lambda: log("tidy"),
         "check": grew("tidy", ["That's the plan"])},
        {"title": "Do it", "page": "tidy", "target": lambda: pg("tidy").do_btn,
         "text": "It asks once more, then moves the files. A file that changed since the plan is skipped.",
         "do": "click Do it, then Yes.", "start": lambda: log("tidy"), "check": grew("tidy", ["Done.", "Problem"])},
        {"title": "Sort files", "page": "sort", "target": lambda: pg("sort").open_btn,
         "text": "For a pile of photos and videos: each file full size, K to keep, D to Trash, U to undo. It "
                 "remembers where you stopped, so a thousand photos can take a few evenings.",
         "do": "try it later on a real folder. Click Next."},
        {"title": "Find a screenshot", "page": "find", "target": lambda: pg("find").search_row,
         "text": "It reads the text in your screenshots once. After that you can find one by any word that was on "
                 "the screen, like an order number, a name or an error message.",
         "do": "pick your screenshots folder and press Read the text when you have a few minutes. Click Next."},
        {"title": "Record the screen", "page": "record", "target": lambda: pg("record").buttons,
         "text": "One window or the whole screen, with the computer's sound. It uses your graphics card when it can, "
                 "and the file survives a crash.",
         "do": "click Next."},
        {"title": "You're ready", "target": lambda: app.tour_button,
         "text": f"The practice folders are in '{PRACTICE}'. Look inside to see what Back up and Tidy did, then "
                 "delete them whenever you like.\n\nPress F1 on any page for help with that page.",
         "do": "click Done."},
    ]


def main():
    app = App("Drive Kit", "Your files, on your own drives.", ACCENT, ABOUT, class_name="DriveKit")
    app.add_page("backup", "Back up", BackupPage)
    app.add_page("tidy", "Tidy a folder", TidyPage)
    app.add_page("sort", "Sort files", SortPage)
    app.add_page("find", "Find a screenshot", FindPage)
    app.add_page("record", "Record the screen", RecordPage)
    if (HERE / "icon.png").exists():
        app.iconphoto(True, tk.PhotoImage(file=str(HERE / "icon.png")))
    app.set_tutorial(lambda: tutorial(app), Path.home() / ".drive-kit" / "settings.json")
    app.run("backup")


if __name__ == "__main__":
    handle_cli()
    main()
