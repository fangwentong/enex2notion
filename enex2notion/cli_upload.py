import logging
import threading, copy
from pathlib import Path
from typing import Optional

from enex2notion.enex_parser import count_notes, iter_notes
from enex2notion.enex_types import EvernoteNote
from enex2notion.enex_uploader import upload_note
from enex2notion.enex_uploader_modes import get_notebook_database, get_notebook_page
from enex2notion.note_parser.note import parse_note
from enex2notion.utils_exceptions import NoteUploadFailException
from enex2notion.utils_static import Rules

logger = logging.getLogger(__name__)


class DoneFile(object):
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

        try:
            with open(path, "r") as f:
                self.done_hashes = {line.strip() for line in f}
        except FileNotFoundError:
            self.done_hashes = set()

    def __contains__(self, note_hash):
        self.lock.acquire()
        try:
            return note_hash in self.done_hashes
        finally:
            self.lock.release()

    def add(self, note_hash):
        self.lock.acquire()
        try:
            self.done_hashes.add(note_hash)
            with open(self.path, "a") as f:
                f.write(f"{note_hash}\n")
        finally:
            self.lock.release()


class EnexUploader(object):
    def __init__(self, import_root, mode: str, done_file: Optional[Path], rules: Rules, parallelism: int):
        self.import_root = import_root
        self.mode = mode

        self.rules = rules

        self.done_hashes = DoneFile(done_file) if done_file else set()

        self.notebook_notes_count = None
        self.parallelism = parallelism
        self.semaphore = threading.BoundedSemaphore(self.parallelism)

    def upload_notebook(self, enex_file: Path):
        logger.info(f"Processing notebook '{enex_file.stem}'...")
        self.notebook_notes_count = count_notes(enex_file)

        notebook_root = self._get_notebook_root(enex_file.stem)
        for note_idx, note in enumerate(iter_notes(enex_file), 1):
            self.semaphore.acquire()
            thread = threading.Thread(target=self.upload_note_with_release, args=(notebook_root, note, note_idx))
            thread.start()

        # await all thread done
        for i in range(self.parallelism):
            self.semaphore.acquire()
        self.semaphore.release(self.parallelism)

    def upload_note_with_release(self, notebook_root, note: EvernoteNote, note_idx: int):
        try:
            self.upload_note(notebook_root, copy.deepcopy(note), note_idx)
        finally:
            self.semaphore.release()

    def upload_note(self, notebook_root, note: EvernoteNote, note_idx: int):
        if note.note_hash in self.done_hashes:
            logger.debug(f"Skipping note '{note.title}' (already uploaded)")
            return

        if self.rules.tag and self.rules.tag not in note.tags:
            note.tags.append(self.rules.tag)

        logger.debug(f"Parsing note '{note.title}'")

        note_blocks = self._parse_note(note)
        if not note_blocks:
            logger.debug(f"Skipping note '{note.title}' (no blocks)")
            return

        if notebook_root is not None:
            logger.info(
                f"Uploading note {note_idx}"
                f" out of {self.notebook_notes_count} '{note.title}'"
            )

            _upload_note(notebook_root, note, note_blocks)
            self.done_hashes.add(note.note_hash)

    def _parse_note(self, note):
        try:
            return parse_note(note, self.rules)
        except Exception as e:
            logger.error(f"Failed to parse note '{note.title}'")
            logger.debug(e, exc_info=e)
            return []

    def _get_notebook_root(self, notebook_title):
        if self.import_root is None:
            return None

        if self.mode == "DB":
            return get_notebook_database(self.import_root, notebook_title)

        return get_notebook_page(self.import_root, notebook_title)


def _upload_note(notebook_root, note, note_blocks):
    for attempt in range(5):
        try:
            upload_note(notebook_root, note, note_blocks)
        except NoteUploadFailException:
            if attempt == 4:
                raise
            logger.warning(
                f"Failed to upload note '{note.title}' to Notion! Retrying..."
            )
            continue
        break
