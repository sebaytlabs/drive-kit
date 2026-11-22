# Drive Kit

Tools for people who keep their files on their own drives instead of someone else's cloud.

The rule behind all of them: **one source of truth, as many backups as you like.** A backup is a copy you only read, so it can't drift away from the original. A second copy you keep editing will drift. These tools never delete for good and never overwrite. Anything they remove goes to the Trash, where you can restore it.

| Tool | What it does | Runs on |
|---|---|---|
| `mirror.py` | Copies whatever is missing from one drive to another. Compares by content, checks every copy and keeps a log. Stops before the backup drive gets too full. | Windows, Mac, Linux |
| `flatten.py` | Turns a deep folder tree into five flat folders (Documents, Music, Pictures, Videos, Other) and moves exact duplicates to the Trash. Plans first, then applies. | Linux, Mac |
| `sorter.py` | Shows each file full-window. **K** keeps it, **D** sends it to the Trash, **U** undoes. Works with pictures, video, audio, PDFs and text. | Linux (GTK) |
| `shot_search.py` | Reads the text in your screenshots once, so you can find one by a word that was on screen. | Windows, Mac, Linux |
| `record.sh` | Records the screen or one window: hardware H.264 when your graphics card has it, desktop audio included, and a file that survives a crash. | Linux (X11) |

## The app

```
python3 drive_kit_app.py
```

There's one window for all of it: **Back up**, **Tidy a folder**, **Sort files**, **Find a screenshot** and **Record the screen**. Each page explains itself, shows what would happen first and asks before it changes anything. Everything below also works from the command line.

## mirror.py

```
python3 mirror.py /media/me/Main /media/me/Backup               # dry run: what would be copied, and how full it gets
python3 mirror.py /media/me/Main /media/me/Backup --apply       # copy, check each copy (sha256), log to Backup/logs/
python3 mirror.py /media/me/Main /media/me/Backup --only Documents Pictures --cap 90
```

- **Skipped:** a file the backup already has at the same path and size, or with the same content under any name. Files you renamed or moved on the backup aren't copied again.
- **Never overwritten:** if the path holds a different file, the copy is saved as `name (from Main).ext`.
- **Safe names:** characters that exFAT and NTFS can't store (`: * ? " < > |`) become `-`, so a Linux drive can be mirrored to a drive Windows and Mac can read.
- **Space limit:** it stops before the destination passes `--cap` percent full (default 80). Leaving that room keeps a drive healthy and leaves space for the next sync.
- **Both ways:** run it in each direction to keep two drives in step without ever deleting anything.

## flatten.py

```
python3 flatten.py plan  ~/Drive OldBackup "Phone dump"     # writes plan.tsv, moves nothing
python3 flatten.py apply                                     # carries out plan.tsv
```

- **Where files go:** each file is sorted by type into one of the five folders. Files that came from a subfolder are renamed `<old-folder>__<file>`, so you can always tell where something came from (`ls Documents | grep '^taxes__'`).
- **What goes to the Trash:** exact duplicates and system junk such as `.DS_Store`, `Thumbs.db` and `desktop.ini`. They land in one dated folder that keeps the original paths, so they're easy to restore.
- **What it leaves alone:** git repos and symlinks.
- **Safety check:** a file that changed after the plan was made is skipped.
- **Speed:** `--fast` hashes only the start and end of each file, then compares any match in full before trashing it.

## sorter.py

```
python3 sorter.py
```

Pick a folder, then go through it one file at a time. **K** moves the file to a `Kept` folder, **D** sends it to the Trash and **U** undoes the last one. **Space** plays video and **O** opens the file in its own app. It remembers where you stopped.

On Debian, Ubuntu or Mint, install the dependencies with `sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-gst-plugins-base-1.0 gstreamer1.0-plugins-good`.

## shot_search.py

```
pip install rapidocr_onnxruntime        # offline OCR, nothing else to install (or put tesseract on your PATH)
python3 shot_search.py index ~/Pictures/Screenshots
python3 shot_search.py find tracking number
```

- **PNG only by default.** On a real library almost every screenshot had text and almost no camera photo did, so this skips hours of wasted work. Add `--all-images` to read every image anyway.
- **Quick to re-run.** Only new or changed files are read again.

## record.sh

```
./record.sh                # click a window to record it
./record.sh --full         # the whole screen
./record.sh --no-audio --out ~/Recordings
```

- **Frame rate:** it records at your monitor's refresh rate, capped at 60.
- **Encoding:** it uses VAAPI hardware encoding when your graphics card supports it and falls back to x264 when it doesn't.
- **Stopping:** press **q** or **Ctrl-C**.

## Tests

```
python3 mirror.py --selftest
python3 flatten.py selftest
python3 shot_search.py --selftest
```

## License

MIT. Made by [Sebayt Labs](https://sebaytlabs.com).
