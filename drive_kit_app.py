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
from appkit import BG, DIM, FG, FONT, LINE, PANEL, App, Capture, Job, Page, open_path, write

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
        self.path_field("Copy from", self.src)
        self.path_field("Copy to (backup)", self.dst)
        r = self.row()
        tk.Label(r, text="Stop at", bg=BG, fg=FG, font=(FONT, 11), width=16, anchor="w").pack(side="left")
        tk.Spinbox(r, from_=50, to=98, textvariable=self.cap, width=5, font=(FONT, 11)).pack(side="left")
        tk.Label(r, text="% full (leaves room so the drive stays healthy)", bg=BG, fg=DIM,
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
        self.path_field("Main folder", self.root)
        r = self.row()
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
        self.button(r, "Open the sorter", self.open_sorter, state="normal" if LINUX else "disabled").pack(side="left")
        if not LINUX:
            self.text("The sorter needs Linux (GTK) for now.", 11, DIM)
        self.text("It remembers the last folder and file. If it opens on a folder you don't want, press "
                  "'Choose folder' in its window.", 11, DIM, pady=(14, 0))

    def open_sorter(self):
        subprocess.Popen([sys.executable, str(HERE / "sorter.py")])
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
        r = self.row(pady=10)
        ok = LINUX and shutil.which("ffmpeg")
        self.win_btn = self.button(r, "Record a window", lambda: self.start("--window"),
                                   state="normal" if ok else "disabled")
        self.win_btn.pack(side="left")
        self.full_btn = self.button(r, "Record the whole screen", lambda: self.start("--full"),
                                    state="normal" if ok else "disabled")
        self.full_btn.pack(side="left", padx=8)
        self.stop_btn = tk.Button(r, text="■  Stop", command=self.stop, bg="#e03131", fg="white", relief="flat",
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
        self.stop_btn.config(state="normal")
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
        self.stop_btn.config(state="disabled")
        self.state.config(text=f"Saved in {self.out.get()}", fg=FG)


def main():
    app = App("Drive Kit", "Your files, on your own drives.", ACCENT, ABOUT, class_name="DriveKit")
    app.add_page("backup", "Back up", BackupPage)
    app.add_page("tidy", "Tidy a folder", TidyPage)
    app.add_page("sort", "Sort files", SortPage)
    app.add_page("find", "Find a screenshot", FindPage)
    app.add_page("record", "Record the screen", RecordPage)
    if (HERE / "icon.png").exists():
        app.iconphoto(True, tk.PhotoImage(file=str(HERE / "icon.png")))
    app.run("backup")


if __name__ == "__main__":
    main()
