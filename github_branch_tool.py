"""
GitHub Branch Clone Tool
========================

A small desktop utility with a simple Qt interface that:

1. Accepts a GitHub clone URL (the SSH or HTTPS address used by ``git clone``,
   not a browser page URL).
2. Lets you pick a local folder where the repository will be cloned.
3. Fetches the remote branch list with ``git ls-remote``.
4. Lets you select one of those branches, or type a branch name yourself.
5. Lets you choose a *new* local branch name.
6. Clones that remote branch (``git clone --branch`` already checks it
   out) and renames the local branch. There is no second switch: rename
   keeps HEAD on the same branch, only the name changes.

Every Git action is a real command-line process (``git.exe`` via
``subprocess``). The console pane shows both the exact command and a short
explanation of what that command does, so watching the log is itself a
small lesson in Git.

Run:
    python github_branch_tool.py
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

# Git branch names cannot contain spaces, "..", "~", "^", ":", "?", "*",
# "[", "\\", or start/end with "/" or ".". This is a practical subset of
# the rules documented in ``git check-ref-format``.
_BRANCH_NAME_RE = re.compile(r"^(?!/)(?!.*\.\.)(?!.*//)(?!.*@\{)[A-Za-z0-9._/-]+(?<!/)(?<!\.lock)$")


def timestamp() -> str:
    """Return a short local timestamp used as a prefix on every log line."""
    return datetime.now().strftime("%H:%M:%S")


def looks_like_clone_url(url: str) -> bool:
    """
    Accept the two forms Git itself accepts for ``git clone``:

    * HTTPS:  https://github.com/owner/repo.git
    * SSH:    git@github.com:owner/repo.git

    A browser URL such as ``https://github.com/owner/repo`` (no ``.git``)
    is also accepted, because GitHub's clone endpoint serves it the same
    way. What we reject is a page that is clearly not a repository root,
    for example a ``/tree/``, ``/blob/``, ``/pull/`` or ``/issues/`` path.
    """
    text = url.strip()
    if not text:
        return False

    lowered = text.lower()
    browser_page_markers = ("/tree/", "/blob/", "/pull/", "/pulls", "/issues", "/commit/", "/actions")
    if any(marker in lowered for marker in browser_page_markers):
        return False

    https_ok = lowered.startswith("https://") or lowered.startswith("http://")
    ssh_ok = lowered.startswith("git@") or lowered.startswith("ssh://")
    return https_ok or ssh_ok


def repo_folder_name(url: str) -> str:
    """
    Derive the directory name Git would create.

    ``git clone`` uses the last path segment of the URL, without a trailing
    ``.git``. Example: ``https://github.com/octo/hello.git`` -> ``hello``.
    """
    cleaned = url.strip().rstrip("/")
    # SSH form is ``git@host:owner/repo.git`` — the path is after the colon.
    if ":" in cleaned and "@" in cleaned.split(":", 1)[0]:
        cleaned = cleaned.split(":", 1)[1]
    name = cleaned.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name or "repository"


def is_valid_branch_name(name: str) -> bool:
    """Return True when ``name`` is a reasonable Git branch name."""
    if not name or name in {".", ".."}:
        return False
    return _BRANCH_NAME_RE.match(name) is not None


class GitWorker(QProcess):
    """
    One Git command, run as a child process.

    QProcess is used instead of ``subprocess`` so the GUI thread stays
    responsive: stdout and stderr arrive as Qt signals and are appended to
    the console as the command runs. Only one worker is active at a time;
    the window disables the action buttons while it is running.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Merge stderr into stdout so the console shows a single chronological
        # stream, the same way a terminal does when you do not redirect.
        self.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)


class MainWindow(QMainWindow):
    """The whole application lives in this one window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("GitHub Branch Clone Tool")
        self.resize(980, 720)

        # Full console text, kept separately from what the filter displays,
        # so clearing the filter does not destroy the log. Each entry is
        # (kind, line). kind is "command", "lesson", "output", or "info",
        # and only "command" lines are drawn as the highlighted Git command.
        self._full_log: list[tuple[str, str]] = []
        self._busy = False
        self._process: GitWorker | None = None
        # What to do after the current command exits successfully.
        # Fetch is one command; clone-and-rename is a short sequence.
        self._queue: list[tuple[list[str], str, Path | None]] = []
        self._on_success = None  # optional callable run after the queue drains

        self._build_ui()
        self._log_lesson(
            "Ready.",
            "Fill in the clone URL and a destination folder, then Fetch branches. "
            "Every Git step below is the real command line, plus a one-line explanation.",
        )

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        form_box = QGroupBox("Repository")
        form = QFormLayout(form_box)

        # --- Clone URL -------------------------------------------------
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://github.com/owner/repository.git   or   git@github.com:owner/repository.git")
        self.url_edit.setClearButtonEnabled(True)
        form.addRow("Clone URL", self.url_edit)

        # --- Destination folder ---------------------------------------
        dest_row = QHBoxLayout()
        self.dest_edit = QLineEdit()
        self.dest_edit.setPlaceholderText(r"C:\path\to\parent\folder")
        dest_row.addWidget(self.dest_edit, stretch=1)

        # File dialog first, folder dialog second, as requested.
        # Cloning creates a directory, so a file path itself is not a valid
        # destination. The file button uses the parent folder of the file
        # you pick. The folder button uses the folder you pick directly.
        browse_file = QPushButton("Open file…")
        browse_file.setToolTip("Pick any file. Its parent folder becomes the clone destination.")
        browse_file.clicked.connect(self._browse_file)
        dest_row.addWidget(browse_file)

        browse_folder = QPushButton("Open folder…")
        browse_folder.setToolTip("Pick the parent folder. The repository will be cloned inside it.")
        browse_folder.clicked.connect(self._browse_folder)
        dest_row.addWidget(browse_folder)

        dest_wrap = QWidget()
        dest_wrap.setLayout(dest_row)
        form.addRow("Destination", dest_wrap)

        # --- Remote branch --------------------------------------------
        branch_row = QHBoxLayout()
        self.branch_combo = QComboBox()
        self.branch_combo.setEditable(True)
        self.branch_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.branch_combo.lineEdit().setPlaceholderText("Select a fetched branch or type one")
        branch_row.addWidget(self.branch_combo, stretch=1)

        self.fetch_button = QPushButton("Fetch branches")
        self.fetch_button.setToolTip("Run git ls-remote --heads and fill the list above.")
        self.fetch_button.clicked.connect(self._fetch_branches)
        branch_row.addWidget(self.fetch_button)

        branch_wrap = QWidget()
        branch_wrap.setLayout(branch_row)
        form.addRow("Remote branch", branch_wrap)

        # --- New local name -------------------------------------------
        self.new_name_edit = QLineEdit()
        self.new_name_edit.setPlaceholderText("local branch name, for example feature/my-copy")
        form.addRow("New local name", self.new_name_edit)

        # --- Options --------------------------------------------------
        options_row = QHBoxLayout()
        self.single_branch_check = QCheckBox("Single-branch clone (faster, only this branch)")
        self.single_branch_check.setChecked(True)
        self.single_branch_check.setToolTip(
            "Passes --single-branch so Git does not download every other branch."
        )
        options_row.addWidget(self.single_branch_check)
        options_row.addStretch(1)
        options_wrap = QWidget()
        options_wrap.setLayout(options_row)
        form.addRow("Options", options_wrap)

        # --- Action bar, kept above the console -----------------------
        # The button says "rename", not "switch". git clone --branch already
        # checks the remote branch out. git branch -m only renames that
        # current branch; HEAD never moves to a different branch.
        actions = QHBoxLayout()
        self.clone_button = QPushButton("Clone and rename")
        self.clone_button.setToolTip(
            "Clone the selected remote branch (which checks it out) and rename that local branch."
        )
        self.clone_button.clicked.connect(self._clone_and_rename)
        actions.addWidget(self.clone_button)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        actions.addWidget(self.cancel_button)
        actions.addStretch(1)
        actions_wrap = QWidget()
        actions_wrap.setLayout(actions)

        # --- Console --------------------------------------------------
        console_box = QGroupBox("Console  —  command log (what Git is doing, and why)")
        console_layout = QVBoxLayout(console_box)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Show only lines containing this text")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self.filter_edit, stretch=1)

        clear_filter = QPushButton("Clear filter")
        clear_filter.clicked.connect(lambda: self.filter_edit.clear())
        filter_row.addWidget(clear_filter)

        clear_console = QPushButton("Clear console")
        clear_console.clicked.connect(self._clear_console)
        filter_row.addWidget(clear_console)

        export_button = QPushButton("Export…")
        export_button.setToolTip("Save the full console log (filter ignored) to a text file.")
        export_button.clicked.connect(self._export_console)
        filter_row.addWidget(export_button)
        console_layout.addLayout(filter_row)

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        # Dark console so the highlighted command blocks stand out the way
        # they would in a terminal, instead of blending into the form.
        self.console.setStyleSheet(
            "QPlainTextEdit {"
            " background-color: #1e1e1e;"
            " color: #d4d4d4;"
            " selection-background-color: #264f78;"
            "}"
        )
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(10)
        self.console.setFont(mono)
        # Commands use a larger bold face. Explanations stay at the base size
        # so the eye lands on the command first.
        self._command_font = QFont("Consolas")
        self._command_font.setStyleHint(QFont.StyleHint.Monospace)
        self._command_font.setPointSize(13)
        self._command_font.setBold(True)
        console_layout.addWidget(self.console)

        # Form on top, clone action directly above the console, log below.
        # The action bar is outside the splitter so it cannot slide under
        # the console when the panes are resized.
        root.addWidget(form_box)
        root.addWidget(actions_wrap)
        root.addWidget(console_box, stretch=1)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Idle")

    # ------------------------------------------------------------- dialogs

    def _browse_folder(self) -> None:
        """Open a folder picker. The chosen path is the parent of the clone."""
        start = self.dest_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose destination folder", start)
        if chosen:
            self.dest_edit.setText(chosen)
            self._log_info(f"Destination folder set to: {chosen}")

    def _browse_file(self) -> None:
        """
        Open a file picker.

        Cloning creates a directory, so a file path itself is not a valid
        destination. We take the parent folder of the file you pick and use
        that. This matches the requested "open file dialog" without sending
        Git a path it cannot clone into.
        """
        start = self.dest_edit.text().strip() or str(Path.home())
        chosen, _filter = QFileDialog.getOpenFileName(self, "Choose a file inside the destination folder", start)
        if not chosen:
            return
        parent = str(Path(chosen).parent)
        self.dest_edit.setText(parent)
        self._log_info(
            f"Selected file: {chosen}\n"
            f"Using its parent folder as the clone destination: {parent}"
        )

    # --------------------------------------------------------------- log

    def _format_for(self, kind: str) -> QTextCharFormat:
        """
        Visual weight of one log line.

        Commands are the thing you are meant to learn, so they are larger,
        bold, and painted on a green bar. Lessons are the explanation under
        them (cyan, not bold). Git's own output stays dim so it does not
        compete with the command.
        """
        fmt = QTextCharFormat()
        if kind == "command":
            fmt.setFont(self._command_font)
            fmt.setForeground(QColor("#0b1f14"))
            fmt.setBackground(QColor("#3dd68c"))
        elif kind == "lesson":
            fmt.setForeground(QColor("#9cdcfe"))
        elif kind == "output":
            fmt.setForeground(QColor("#9a9a9a"))
        else:
            fmt.setForeground(QColor("#d4d4d4"))
        return fmt

    def _write_line(self, kind: str, line: str) -> None:
        """Append one already-filtered line using the format for ``kind``."""
        cursor = self.console.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if self.console.document().characterCount() > 1:
            cursor.insertText("\n")
        cursor.insertText(line, self._format_for(kind))
        self.console.setTextCursor(cursor)
        self.console.ensureCursorVisible()

    def _append_raw(self, kind: str, line: str) -> None:
        """Store one log line and show it if it passes the current filter."""
        self._full_log.append((kind, line))
        needle = self.filter_edit.text().strip().lower()
        if needle and needle not in line.lower():
            return
        self._write_line(kind, line)

    def _log_info(self, text: str) -> None:
        for line in text.splitlines() or [""]:
            self._append_raw("info", f"[{timestamp()}] {line}")

    def _log_lesson(self, title: str, explanation: str) -> None:
        """
        Write a teaching block: a heading plus the reason the next command
        exists. Drawn in the lesson color, smaller than the command itself.
        """
        self._append_raw("lesson", f"[{timestamp()}] ── {title}")
        for line in explanation.splitlines():
            self._append_raw("lesson", f"[{timestamp()}]    {line}")

    def _log_command(self, args: list[str], cwd: Path | None) -> None:
        """
        Show the exact argv that is about to be executed.

        The command is written alone, on its own highlighted line, with a
        blank line before it. The working directory is a separate dim note
        so the command text stays easy to copy and easy to remember.
        """
        quoted = " ".join(_quote(part) for part in args)
        self._append_raw("info", "")
        self._append_raw("command", f"  $ {quoted}")
        if cwd is not None:
            self._append_raw("output", f"    working directory: {cwd}")

    def _clear_console(self) -> None:
        self._full_log.clear()
        self.console.clear()
        self._log_info("Console cleared.")

    def _apply_filter(self) -> None:
        """Rebuild the visible console from the full log using the filter box."""
        needle = self.filter_edit.text().strip().lower()
        self.console.clear()
        for kind, line in self._full_log:
            if not needle or needle in line.lower():
                self._write_line(kind, line)
        self.console.moveCursor(QTextCursor.MoveOperation.End)

    def _export_console(self) -> None:
        """Save the unfiltered log. The filter only affects what you see."""
        default_name = f"git-console-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Export console log",
            str(Path.home() / default_name),
            "Text files (*.txt);;All files (*.*)",
        )
        if not path:
            return
        try:
            # Export the plain text only. Highlight colors are for the window;
            # a .txt file keeps the command lines as "$ ..." so they stay readable.
            text = "\n".join(line for _kind, line in self._full_log)
            Path(path).write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self._log_info(f"Console exported to: {path}")
        self.statusBar().showMessage(f"Exported {path}")

    # ------------------------------------------------------------- actions

    def _validate_common(self) -> tuple[str, Path] | None:
        """Check URL and destination. Return them, or None after showing an error."""
        url = self.url_edit.text().strip()
        dest_text = self.dest_edit.text().strip()

        if not looks_like_clone_url(url):
            QMessageBox.warning(
                self,
                "Clone URL",
                "Enter a Git clone URL, not a browser page.\n\n"
                "Good:\n"
                "  https://github.com/owner/repository.git\n"
                "  git@github.com:owner/repository.git\n\n"
                "Not good:\n"
                "  https://github.com/owner/repository/tree/main",
            )
            return None

        if not dest_text:
            QMessageBox.warning(self, "Destination", "Choose the folder where the repository should be cloned.")
            return None

        dest = Path(dest_text)
        if not dest.exists() or not dest.is_dir():
            QMessageBox.warning(self, "Destination", f"This folder does not exist:\n{dest}")
            return None

        return url, dest

    def _fetch_branches(self) -> None:
        """
        Ask the remote which branches exist, without cloning anything.

        ``git ls-remote --heads <url>`` prints one line per branch:

            <commit-sha>\\trefs/heads/<branch-name>

        It talks to the server only. Nothing is written to the destination
        folder. That makes it the right command for "show me the branches
        before I clone".
        """
        checked = self._validate_common()
        if checked is None or self._busy:
            return
        url, _dest = checked

        self._log_lesson(
            "Fetching the remote branch list",
            "Command: git ls-remote --heads <clone-url>\n"
            "This contacts the Git server and lists branch tips "
            "(refs/heads/*). It does not download the repository and does "
            "not create any files. Each line is a commit id followed by the "
            "full ref name. We keep only the part after refs/heads/.",
        )
        self._on_success = self._parse_remote_branches
        self._start_queue(
            [(["git", "ls-remote", "--heads", url], "list remote branches", None)]
        )

    def _parse_remote_branches(self, output: str) -> None:
        """Turn ``git ls-remote`` output into combo-box entries."""
        names: list[str] = []
        for line in output.splitlines():
            # Format: "<sha>\\trefs/heads/<name>"
            if "\t" not in line:
                continue
            _sha, ref = line.split("\t", 1)
            prefix = "refs/heads/"
            if ref.startswith(prefix):
                names.append(ref[len(prefix):])

        self.branch_combo.clear()
        if not names:
            self._log_info("The remote returned no branches.")
            QMessageBox.information(self, "Branches", "No branches were returned by the remote.")
            return

        self.branch_combo.addItems(names)
        # Prefer main/master as the default selection when either exists.
        for preferred in ("main", "master"):
            index = self.branch_combo.findText(preferred)
            if index >= 0:
                self.branch_combo.setCurrentIndex(index)
                break

        self._log_lesson(
            f"Found {len(names)} branch(es)",
            "The names above came from refs/heads/. "
            "Pick one in the Remote branch box, or type a name that was not listed "
            "(Git will fail later if the remote really does not have it).",
        )
        self.statusBar().showMessage(f"Fetched {len(names)} branches")

    def _clone_and_rename(self) -> None:
        """
        Clone one remote branch, then give it the local name the user typed.

        Sequence (each step is a separate ``git`` process):

        1. ``git clone --branch <remote> -- <url> <folder>``
           Downloads the repository and checks out <remote> immediately.
           With "single-branch" ticked we also pass ``--single-branch``,
           which tells Git to fetch only that one branch.

        2. ``git branch -m <new-name>``
           Renames the branch that is currently checked out. After a fresh
           clone that branch is the remote one, so this is the rename.

        3. ``git status -sb`` and ``git branch --show-current``
           Read-only confirmation. ``git clone --branch`` already checked the
           branch out, and ``branch -m`` only renames it. HEAD does not move
           to a different branch, so there is no ``git switch`` / ``git checkout``.
        """
        checked = self._validate_common()
        if checked is None or self._busy:
            return
        url, dest = checked

        remote_branch = self.branch_combo.currentText().strip()
        new_name = self.new_name_edit.text().strip()

        if not is_valid_branch_name(remote_branch):
            QMessageBox.warning(
                self,
                "Remote branch",
                "Select a branch or type a valid branch name (letters, numbers, '.', '_', '-', '/').",
            )
            return
        if not is_valid_branch_name(new_name):
            QMessageBox.warning(
                self,
                "New local name",
                "Type a valid new branch name (letters, numbers, '.', '_', '-', '/').",
            )
            return
        if new_name == remote_branch:
            QMessageBox.warning(
                self,
                "New local name",
                "The new local name is the same as the remote branch.\n"
                "Type a different name — that is the point of the rename step.",
            )
            return

        folder_name = repo_folder_name(url)
        target = dest / folder_name
        if target.exists():
            QMessageBox.warning(
                self,
                "Destination not empty",
                f"Git would clone into this path, but it already exists:\n{target}\n\n"
                "Move it aside or pick another destination folder.",
            )
            return

        clone_args = ["git", "clone", "--branch", remote_branch]
        if self.single_branch_check.isChecked():
            clone_args.append("--single-branch")
        # "--" ends options so a branch name that starts with "-" cannot be
        # misread as a flag. URL and target path follow.
        clone_args.extend(["--", url, str(target)])

        self._log_lesson(
            "Step 1 of 3 — clone that branch",
            "Command: git clone --branch <remote-branch> [--single-branch] -- <url> <folder>\n"
            "--branch tells Git which remote branch to check out at the end of the clone.\n"
            "--single-branch (when ticked) fetches only that branch, not the whole remote.\n"
            "The last two arguments are the clone URL and the new folder Git will create.\n"
            "When this finishes, the working tree is already on the remote branch name.",
        )
        self._log_lesson(
            "Step 2 of 3 — rename the local branch",
            "Command: git branch -m <new-name>\n"
            "Run inside the new clone. -m means 'move' (rename). With one argument it "
            "renames the branch you are currently on. History is unchanged; only the "
            "local branch label changes. This is not a switch: HEAD stays put, only the name changes.",
        )
        self._log_lesson(
            "Step 3 of 3 — show where HEAD is",
            "Command: git status -sb   and   git branch --show-current\n"
            "These do not change anything. -sb is short branch status. "
            "branch --show-current prints the branch name you are on, which should "
            "now be the new local name.",
        )

        # Captured output of the whole sequence is not needed; success is
        # "the last command exited 0". A short confirmation is logged then.
        self._on_success = lambda _output: self._log_lesson(
            "Done",
            f"The repository is in:\n{target}\n"
            f"The checked-out branch was renamed from '{remote_branch}' to '{new_name}'. "
            "No extra switch was needed: clone already checked it out, and rename kept HEAD there.",
        )
        self._start_queue(
            [
                (clone_args, "clone", None),
                (["git", "branch", "-m", new_name], "rename local branch", target),
                (["git", "status", "-sb"], "status", target),
                (["git", "branch", "--show-current"], "show current branch", target),
            ]
        )

    # ------------------------------------------------------- process queue

    def _start_queue(self, steps: list[tuple[list[str], str, Path | None]]) -> None:
        self._queue = list(steps)
        self._set_busy(True)
        self._run_next()

    def _run_next(self) -> None:
        if not self._queue:
            self._set_busy(False)
            self.statusBar().showMessage("Idle")
            return

        args, label, cwd = self._queue.pop(0)
        self._log_command(args, cwd)
        self.statusBar().showMessage(f"Running: {label}")

        process = GitWorker(self)
        self._process = process
        if cwd is not None:
            process.setWorkingDirectory(str(cwd))

        # Accumulate this command's output. Fetch needs it in order to parse
        # branch names; the other commands only need it echoed to the console.
        chunks: list[str] = []

        def on_ready() -> None:
            data = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
            if not data:
                return
            chunks.append(data)
            for line in data.splitlines():
                # Git's own stdout/stderr stays dim. The command that produced
                # it is the highlighted line above this block.
                self._append_raw("output", f"[{timestamp()}]   {line}")

        def on_finished(exit_code: int, _status) -> None:
            on_ready()  # drain anything still buffered
            output = "".join(chunks)
            if exit_code != 0:
                self._queue.clear()
                self._set_busy(False)
                self._log_info(f"Command failed with exit code {exit_code}. Stopped.")
                self.statusBar().showMessage(f"Failed ({exit_code})")
                QMessageBox.critical(
                    self,
                    "Git command failed",
                    f"Exit code {exit_code}.\nSee the console for the Git message.",
                )
                return

            self._log_info(f"Exit code 0 — {label} finished.")
            if self._queue:
                # Tiny pause so the log stays readable when several commands
                # finish almost instantly.
                QTimer.singleShot(50, self._run_next)
                return

            self._set_busy(False)
            self.statusBar().showMessage("Done")
            callback = self._on_success
            self._on_success = None
            if callback is not None:
                callback(output)

        process.readyReadStandardOutput.connect(on_ready)
        process.finished.connect(on_finished)
        process.errorOccurred.connect(lambda err: self._on_process_error(err, label))

        program, *rest = args
        process.start(program, rest)
        if not process.waitForStarted(5000):
            self._queue.clear()
            self._set_busy(False)
            self._log_info(
                "Could not start the process. Is Git installed and on PATH? "
                "Try `git --version` in a terminal."
            )
            QMessageBox.critical(self, "Git not started", "Could not start git. Check that Git is installed and on PATH.")

    def _on_process_error(self, error, label: str) -> None:
        # FailedToStart is also reported by waitForStarted above; ignore the
        # duplicate. Crashed / Timedout still deserve a console line.
        if error == QProcess.ProcessError.FailedToStart:
            return
        self._log_info(f"Process error during {label}: {error}")

    def _cancel(self) -> None:
        """Kill the running Git process and drop the rest of the queue."""
        self._queue.clear()
        if self._process is not None and self._process.state() != QProcess.ProcessState.NotRunning:
            self._log_info("Cancel requested — killing the current Git process.")
            self._process.kill()
        self._set_busy(False)
        self.statusBar().showMessage("Cancelled")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.fetch_button.setEnabled(not busy)
        self.clone_button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.url_edit.setReadOnly(busy)
        self.dest_edit.setReadOnly(busy)

    def closeEvent(self, event) -> None:  # noqa: N802  (Qt naming)
        """Do not leave a git process running after the window closes."""
        if self._process is not None and self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()
            self._process.waitForFinished(2000)
        super().closeEvent(event)


def _quote(part: str) -> str:
    """Quote an argument for display only. The real process gets a raw argv."""
    if part == "" or any(ch in part for ch in ' \t"'):
        escaped = part.replace('"', '\\"')
        return f'"{escaped}"'
    return part


def main() -> int:
    # High-DPI is the default in Qt 6; this keeps the app looking consistent
    # if the environment still sets an older rounding policy.
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("GitHub Branch Clone Tool")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    # Make the working directory the script folder so a double-click on
    # Windows still finds nothing it needs (the app has no relative data
    # files), and so a missing PATH entry for git is easier to reason about.
    os.chdir(Path(__file__).resolve().parent)
    raise SystemExit(main())
