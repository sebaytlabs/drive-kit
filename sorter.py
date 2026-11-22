#!/usr/bin/env python3
"""Media Sorter - view each picture/video, press K to keep or D to delete (to Trash)."""
import json
import configparser
import os
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import urllib.parse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gst", "1.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gst, Gtk  # noqa: E402

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic"}
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp", ".ts"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".opus", ".wma", ".aiff"}
TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".log", ".py", ".js", ".html", ".htm", ".css", ".xml",
            ".yaml", ".yml", ".ini", ".cfg", ".conf", ".sh", ".c", ".h", ".cpp", ".java", ".rs", ".go",
            ".sql", ".tex", ".rtf", ".srt", ".toml", ".svg", ".desktop"}
TEXT_PREVIEW_BYTES = 64 * 1024
STATE_DIR = os.path.join(GLib.get_user_data_dir(), "media-sorter")
STATE_FILE = os.path.join(STATE_DIR, "state.json")


WELCOME = (
    "<span size='xx-large' weight='bold'>Media Sorter</span>\n\n"
    "<span size='x-large'>Pick a folder, then look at each file (pictures, videos, audio, PDFs, text — anything):\n\n"
    "<b>K</b>  =  KEEP  (moves it into a “Kept” folder inside the one you picked)\n"
    "<b>D</b>  =  DELETE  (moves it to the Trash)\n\n"
    "← / →  browse     Space  play/pause     O  open in its own app     U  undo last keep/delete</span>"
)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"kept": [], "last_folder": None}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def scan(folder, recursive, kept_dir, media_only=False):
    """Yield file paths one at a time (hidden files skipped)."""
    if recursive:
        walker = os.walk(folder)
    else:
        try:
            walker = [(folder, [], os.listdir(folder))]
        except OSError:
            walker = []
    for root, dirs, files in walker:
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith(".") and os.path.join(root, d) != kept_dir)
        for name in sorted(files):
            if name.startswith("."):
                continue
            if media_only and os.path.splitext(name)[1].lower() not in IMAGE_EXT | VIDEO_EXT:
                continue
            yield os.path.join(root, name)


def file_kind(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext == ".pdf":
        return "pdf"
    mime = Gio.content_type_guess(path, None)[0]
    if mime.startswith("image/") and "svg" not in mime:
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if ext in TEXT_EXT or mime.startswith("text/"):
        return "text"
    return "other"


def pdf_page_count(path):
    try:
        out = subprocess.run(["pdfinfo", path], capture_output=True, text=True, timeout=15).stdout
        return int(next(l.split()[1] for l in out.splitlines() if l.startswith("Pages:")))
    except (subprocess.SubprocessError, StopIteration, ValueError, OSError):
        return 1


def render_pdf_page(path, page):
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["pdftoppm", "-png", "-singlefile", "-f", str(page), "-l", str(page), "-scale-to", "1400",
                        path, os.path.join(d, "p")], check=True, capture_output=True, timeout=30)
        return GdkPixbuf.Pixbuf.new_from_file(os.path.join(d, "p.png"))


def read_text_preview(path):
    with open(path, "rb") as f:
        data = f.read(TEXT_PREVIEW_BYTES + 1)
    if b"\0" in data[:4096]:
        return None  # binary
    text = data[:TEXT_PREVIEW_BYTES].decode("utf-8", errors="replace")
    return text + ("\n\n… (preview cut off here)" if len(data) > TEXT_PREVIEW_BYTES else "")


def restore_from_trash(orig):
    """Move the most recently trashed copy of `orig` back where it was. Raises OSError if not found."""
    top = os.path.dirname(orig)
    while not os.path.ismount(top) and top != "/":
        top = os.path.dirname(top)
    uid = os.getuid()
    trash_dirs = [(os.path.join(GLib.get_user_data_dir(), "Trash"), "/"),
                  (os.path.join(top, f".Trash-{uid}"), top),
                  (os.path.join(top, ".Trash", str(uid)), top)]
    best = None
    for tdir, base in trash_dirs:
        info_dir = os.path.join(tdir, "info")
        if not os.path.isdir(info_dir):
            continue
        for name in os.listdir(info_dir):
            cp = configparser.RawConfigParser()
            cp.optionxform = str
            try:
                cp.read(os.path.join(info_dir, name))
                path = urllib.parse.unquote(cp["Trash Info"]["Path"])
                when = cp["Trash Info"]["DeletionDate"]
            except (configparser.Error, KeyError, OSError):
                continue
            if os.path.normpath(os.path.join(base, path)) == orig and (best is None or when > best[0]):
                best = (when, tdir, name)
    if not best:
        raise FileNotFoundError("not found in the Trash (maybe it was emptied)")
    _, tdir, name = best
    if os.path.exists(orig):
        raise FileExistsError("a file with that name already exists there")
    shutil.move(os.path.join(tdir, "files", name[:-len(".trashinfo")]), orig)
    os.remove(os.path.join(tdir, "info", name))


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class Sorter(Gtk.Window):
    def __init__(self, folder=None):
        super().__init__(title="Media Sorter")
        self.set_default_size(1100, 750)
        self.state = load_state()
        self.files = []
        self.index = 0
        self.history = []  # this session's keeps/deletes, for undo
        self.held = set()  # action keys currently held down (ignore auto-repeat)
        self.armed = False  # actions allowed only once the current file is on screen
        self.arm_token = 0
        self.shown_ok = False
        self.pixbuf = None
        self.scan_id = 0
        self.scanning = False
        self.confirm = None
        self.pdf_page = self.pdf_pages = 1

        Gst.init(None)
        self.player = Gst.ElementFactory.make("playbin", "player")
        sink = Gst.ElementFactory.make("gtksink", "sink")
        self.player.set_property("video-sink", sink)
        bus = self.player.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_bus)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(box)

        header = Gtk.HeaderBar(show_close_button=True, title="Media Sorter")
        self.set_titlebar(header)
        pick = Gtk.Button(label="Choose folder…")
        pick.connect("clicked", lambda *_: self.choose_folder())
        self.recursive = Gtk.CheckButton(label="Include subfolders", active=True)
        self.recursive.connect("toggled", lambda *_: self.reload())
        self.media_only = Gtk.CheckButton(label="Only pictures & videos")
        self.media_only.connect("toggled", lambda *_: self.reload())
        for w in (pick, self.recursive, self.media_only):
            header.pack_start(w)

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.image = Gtk.Image()
        self.image_area = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                             vscrollbar_policy=Gtk.PolicyType.NEVER)
        self.image_area.add(self.image)
        self.image_area.connect("size-allocate", lambda *_: self.fit_image())
        self.stack.add_named(self.image_area, "image")
        self.stack.add_named(sink.props.widget, "video")
        self.textview = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, can_focus=False,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=16, right_margin=16,
                                     top_margin=12, bottom_margin=12)
        text_area = Gtk.ScrolledWindow()
        text_area.add(self.textview)
        self.stack.add_named(text_area, "text")
        self.message = Gtk.Label(justify=Gtk.Justification.CENTER, wrap=True, selectable=False)
        self.message.set_markup(WELCOME)
        self.stack.add_named(self.message, "message")
        box.pack_start(self.stack, True, True, 0)

        bar = Gtk.Box(spacing=12, margin=10)
        delete = Gtk.Button()
        delete.add(Gtk.Label(use_markup=True, label="<span size='x-large' weight='bold'>D  =  DELETE</span>\n<span size='small'>moves to Trash</span>"))
        delete.get_style_context().add_class("destructive-action")
        delete.connect("clicked", lambda *_: self.delete())
        keep = Gtk.Button()
        keep.add(Gtk.Label(use_markup=True, label="<span size='x-large' weight='bold'>K  =  KEEP</span>\n<span size='small'>moves to Kept folder</span>"))
        keep.get_style_context().add_class("suggested-action")
        keep.connect("clicked", lambda *_: self.keep())
        self.info = Gtk.Label(xalign=0, ellipsize=3)
        hint = Gtk.Label(label="← / →  browse     Space  play/pause     Shift+← / →  seek\nU  undo last keep/delete     O  open in its app     M  mute\nPage Up / Down  turn PDF pages", justify=Gtk.Justification.RIGHT)
        hint.get_style_context().add_class("dim-label")
        bar.pack_start(delete, False, False, 0)
        bar.pack_start(keep, False, False, 0)
        bar.pack_start(self.info, True, True, 0)
        bar.pack_end(hint, False, False, 0)
        box.pack_start(bar, False, False, 0)

        self.connect("key-press-event", self.on_key)
        self.connect("key-release-event", lambda _w, ev: self.held.discard(Gdk.keyval_name(ev.keyval).lower()))
        self.connect("focus-out-event", lambda *_: self.held.clear())
        self.connect("destroy", self.on_quit)
        self.show_all()
        self.stack.set_visible_child_name("message")

        start = folder or self.state.get("last_folder")
        if folder or (start and os.path.isdir(start)):
            self.open_folder(start)
        else:
            GLib.idle_add(self.choose_folder)

    # ---- folder / list handling
    def choose_folder(self):
        dlg = Gtk.FileChooserDialog(title="Choose a folder to sort", parent=self,
                                    action=Gtk.FileChooserAction.SELECT_FOLDER)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Open", Gtk.ResponseType.OK)
        if self.state.get("last_folder"):
            dlg.set_current_folder(self.state["last_folder"])
        if dlg.run() == Gtk.ResponseType.OK:
            self.open_folder(dlg.get_filename())
        dlg.destroy()
        return False

    def open_folder(self, folder):
        self.folder = folder
        self.state["last_folder"] = folder
        save_state(self.state)
        self.reload()

    def kept_dir(self):
        return os.path.join(self.folder, "Kept")

    @staticmethod
    def move_unique(src, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        base, ext = os.path.splitext(os.path.basename(src))
        dest, n = os.path.join(dest_dir, base + ext), 1
        while os.path.exists(dest):
            dest = os.path.join(dest_dir, f"{base} ({n}){ext}")
            n += 1
        shutil.move(src, dest)
        return dest

    def reload(self):
        if not getattr(self, "folder", None):
            return
        self.scan_id += 1
        self.files = []
        self.index = 0
        self.scanning = True
        self.stop_video()
        self.message.set_markup("<span size='xx-large'>Looking for files…</span>")
        self.stack.set_visible_child_name("message")
        self.info.set_text(f"Scanning {self.folder} …")
        threading.Thread(target=self.scan_worker, daemon=True,
                         args=(self.scan_id, self.folder, self.recursive.get_active(), self.kept_dir(),
                               self.media_only.get_active())).start()

    def scan_worker(self, scan_id, folder, recursive, kept_dir, media_only):
        batch = []
        for path in scan(folder, recursive, kept_dir, media_only):
            batch.append(path)
            if len(batch) >= 200:
                GLib.idle_add(self.on_batch, scan_id, batch, False)
                batch = []
        GLib.idle_add(self.on_batch, scan_id, batch, True)

    def on_batch(self, scan_id, batch, done):
        if scan_id != self.scan_id:
            return False
        first = not self.files
        self.files.extend(batch)
        if done:
            self.scanning = False
        if first and self.files:
            self.show()  # show the first file right away, keep scanning behind it
        elif done and not self.files:
            self.message.set_markup(
                "<span size='xx-large'>No files found in this folder</span>\n\n"
                f"<span size='large'>{GLib.markup_escape_text(self.folder)}\n"
                "Try “Include subfolders”, or “Choose folder…” again.</span>")
            self.stack.set_visible_child_name("message")
            self.info.set_text(self.folder)
        elif self.files:
            self.update_info()
        return False

    # ---- display
    def stop_video(self):
        self.player.set_state(Gst.State.NULL)

    def disarm(self):
        self.armed = False
        self.arm_token += 1
        GLib.timeout_add(200, self.arm, self.arm_token)

    def arm(self, token):
        if token == self.arm_token:
            self.armed = True
        return False

    def update_info(self):
        path = self.files[self.index]
        try:
            size = human(os.path.getsize(path))
        except OSError:
            size = "?"
        more = "+" if self.scanning else ""
        self.info.set_text(f"{self.index + 1} / {len(self.files)}{more}   "
                           f"{os.path.relpath(path, self.folder)}   ({size})"
                           + ("   — still scanning…" if self.scanning else ""))

    def show(self):
        self.stop_video()
        if not self.files and self.scanning:
            return
        if not self.files:
            self.message.set_markup("<span size='xx-large'>All done — nothing left to sort 🎉</span>\n\n<span size='large'>Use “Choose folder…” to sort another one.</span>")
            self.stack.set_visible_child_name("message")
            self.info.set_text(self.folder)
            self.set_title("Media Sorter")
            return
        self.index = max(0, min(self.index, len(self.files) - 1))
        path = self.files[self.index]
        self.disarm()
        self.shown_ok = True
        self.confirm = None
        kind = file_kind(path)
        self.update_info()
        self.set_title(f"Media Sorter — {os.path.basename(path)}")
        try:
            getattr(self, "show_" + kind)(path)
        except (GLib.Error, OSError, subprocess.SubprocessError) as e:
            self.shown_ok = False
            self.show_card(path, f"Can't preview this file\n{GLib.markup_escape_text(str(getattr(e, 'message', e)))[:200]}")

    def show_video(self, path):
        self.stack.set_visible_child_name("video")
        self.player.set_property("uri", Gio.File.new_for_path(path).get_uri())
        self.player.set_state(Gst.State.PLAYING)

    def show_audio(self, path):
        self.show_card(path, "♪  Playing audio — Space to pause")
        self.player.set_property("uri", Gio.File.new_for_path(path).get_uri())
        self.player.set_state(Gst.State.PLAYING)

    def show_pixbuf(self, pixbuf):
        self.pixbuf = pixbuf.apply_embedded_orientation() or pixbuf
        self._last = None  # new picture: always redraw, even if same size as the last
        self.stack.set_visible_child_name("image")
        self.fit_image()

    def show_image(self, path):
        self.show_pixbuf(GdkPixbuf.Pixbuf.new_from_file(path))

    def show_pdf(self, path, page=1):
        if page == 1:
            self.pdf_pages = pdf_page_count(path)
        self.pdf_page = page
        self.show_pixbuf(render_pdf_page(path, page))
        if self.pdf_pages > 1:
            self.info.set_text(self.info.get_text() + f"   — page {page} of {self.pdf_pages}  (Page Up / Page Down to turn pages)")

    def turn_page(self, delta):
        path = self.files[self.index] if self.files else None
        if not path or file_kind(path) != "pdf" or self.stack.get_visible_child_name() != "image":
            return
        page = max(1, min(self.pdf_pages, self.pdf_page + delta))
        if page != self.pdf_page:
            self.update_info()
            try:
                self.show_pdf(path, page)
            except (GLib.Error, OSError, subprocess.SubprocessError) as e:
                self.info.set_text(f"Could not show page {page}: {getattr(e, 'message', e)}")

    def show_text(self, path):
        text = read_text_preview(path)
        if text is None:
            return self.show_other(path)
        self.textview.get_buffer().set_text(text)
        self.stack.set_visible_child_name("text")

    def show_other(self, path):
        self.show_card(path, "No preview for this kind of file — press O to open it in its own app")

    def show_card(self, path, note):
        mime = Gio.content_type_guess(path, None)[0]
        try:
            st = os.stat(path)
            facts = f"{human(st.st_size)}   ·   modified {time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}"
        except OSError:
            facts = ""
        self.message.set_markup(
            f"<span size='xx-large' weight='bold'>{GLib.markup_escape_text(os.path.basename(path))}</span>\n\n"
            f"<span size='x-large'>{GLib.markup_escape_text(Gio.content_type_get_description(mime))}\n{facts}</span>\n\n"
            f"<span size='large'>{note}</span>")
        self.stack.set_visible_child_name("message")

    def open_external(self):
        if self.files:
            try:
                Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(self.files[self.index]).get_uri(), None)
            except GLib.Error as e:
                self.info.set_text(f"Could not open: {e.message}")

    def fit_image(self):
        if not self.pixbuf or self.stack.get_visible_child_name() != "image":
            return
        a = self.image_area.get_allocation()
        w, h = self.pixbuf.get_width(), self.pixbuf.get_height()
        scale = min(a.width / w, a.height / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        if (nw, nh) != (getattr(self, "_last", None) or (0, 0)):
            self._last = (nw, nh)
            self.image.set_from_pixbuf(self.pixbuf.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR))

    def on_bus(self, _bus, msg):
        if msg.type == Gst.MessageType.EOS:  # loop videos
            self.player.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)
        elif msg.type == Gst.MessageType.ERROR:
            err, _ = msg.parse_error()
            self.info.set_text(f"Video error: {err.message}")

    # ---- actions
    def blocked(self, what):
        """Guard for K/D: only act on a file that is actually on screen."""
        if not self.files:
            return True
        if not self.armed:
            return True  # picture not painted yet — ignore the keypress
        if not self.shown_ok and self.confirm != what:
            self.confirm = what
            self.info.set_text(f"Couldn't show this file — press {what.upper()} again to {('delete' if what == 'd' else 'keep')} it unseen")
            return True
        return False

    def keep(self):
        if self.blocked("k"):
            return
        path = self.files[self.index]
        self.stop_video()
        try:
            dest = self.move_unique(path, self.kept_dir())
        except OSError as e:
            self.info.set_text(f"Could not move to Kept: {e}")
            return
        self.files.pop(self.index)
        self.history.append(("keep", path, dest))
        self.show()

    def delete(self):
        if self.blocked("d"):
            return
        path = self.files[self.index]
        self.stop_video()
        try:
            Gio.File.new_for_path(path).trash(None)
        except GLib.Error as e:
            self.info.set_text(f"Could not delete: {e.message}")
            return
        self.files.pop(self.index)
        self.history.append(("delete", path, None))
        self.show()

    def undo(self):
        if not self.history:
            self.info.set_text("Nothing to undo")
            return
        kind, orig, dest = self.history[-1]
        self.stop_video()
        try:
            if kind == "keep":
                shutil.move(dest, orig)
            else:
                restore_from_trash(orig)
        except OSError as e:
            self.info.set_text(f"Could not undo: {e}")
            return
        self.history.pop()
        self.files.insert(self.index, orig)
        self.show()
        self.info.set_text(("Restored from Trash: " if kind == "delete" else "Moved back from Kept: ") + self.info.get_text())

    def on_key(self, _w, ev):
        key = Gdk.keyval_name(ev.keyval).lower()
        shift = ev.state & Gdk.ModifierType.SHIFT_MASK
        if key in ("k", "d", "u"):
            if key in self.held:  # key is being held down: act once only
                return True
            self.held.add(key)
        if key != getattr(self, "confirm", None):
            self.confirm = None
        if key == "k":
            self.keep()
        elif key == "d":
            self.delete()
        elif key == "u":
            self.undo()
        elif key == "o":
            self.open_external()
        elif key in ("page_down", "page_up"):
            self.turn_page(1 if key == "page_down" else -1)
        elif key in ("right", "left"):
            if shift and self.stack.get_visible_child_name() == "video":
                ok, pos = self.player.query_position(Gst.Format.TIME)
                if ok:
                    delta = 5 * Gst.SECOND * (1 if key == "right" else -1)
                    self.player.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                                            max(0, pos + delta))
            elif self.files:
                self.index = (self.index + (1 if key == "right" else -1)) % len(self.files)
                self.show()
        elif key == "space":
            if self.stack.get_visible_child_name() == "video":
                _, st, _ = self.player.get_state(0)
                self.player.set_state(Gst.State.PAUSED if st == Gst.State.PLAYING else Gst.State.PLAYING)
        elif key == "m":
            self.player.set_property("mute", not self.player.get_property("mute"))
        else:
            return False
        return True

    def on_quit(self, *_):
        self.stop_video()
        Gtk.main_quit()


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]) else None
    Sorter(arg)
    Gtk.main()
