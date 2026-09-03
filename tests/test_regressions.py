import importlib.util
import subprocess
import tempfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("explorer", ROOT / "src" / "explorer.py")
explorer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(explorer)


def test_macos_left_click_is_not_scroll():
    # macOS stock curses: BUTTON1=2, BUTTON4=524288.
    assert explorer.mouse_scroll_dir(2) == 0
    assert explorer.is_mouse_click(2)


def test_stream_collect_push_success_without_temp_file():
    proc = mock.Mock()
    proc.wait.return_value = 0
    ok, message = explorer._stream_collect(
        proc, None, None, "/remote/out", "out", "push"
    )
    assert ok is True
    assert "out" in message


def test_remote_run_does_not_duplicate_sshpass():
    remote = explorer.Remote("user@example.com", password="secret")
    completed = subprocess.CompletedProcess([], 0, "", "")
    with mock.patch.object(explorer.subprocess, "run", return_value=completed) as run:
        remote.run("true")
    argv = run.call_args.args[0]
    assert argv[:2] == ["sshpass", "-e"]
    assert argv.count("sshpass") == 1


def test_directory_scp_transfer_wraps_scp_with_sshpass():
    remote = explorer.Remote("user@example.com", password="secret")
    remote.run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "1\n", ""))
    proc = mock.Mock()
    proc.stdout.read.side_effect = [b"done", b""]
    proc.wait.return_value = 0
    with mock.patch.object(explorer.subprocess, "Popen", return_value=proc) as popen:
        explorer._scp_transfer(remote, "get", "/remote/dir", "/tmp/dir", mock.Mock(), mock.Mock())
    assert popen.call_args.args[0][:2] == ["sshpass", "-e"]


def test_directory_get_uses_remote_directory_detection():
    remote = mock.Mock()
    remote.is_dir.return_value = True
    remote.target = "user@example.com"
    remote.ssh_prefix.return_value = (["ssh"], None)
    remote.quote_path.side_effect = lambda p: p
    fake_proc = mock.Mock()
    fake_proc.poll.return_value = 0
    fake_proc.wait.return_value = 0
    # A remote-only path must not be checked with os.path.isdir locally.
    with mock.patch.object(explorer, "_scp_transfer", return_value=(True, "ok")) as transfer:
        with mock.patch.object(explorer, "which", return_value=None):
            with mock.patch.object(explorer.subprocess, "Popen", return_value=fake_proc):
                explorer.do_get(remote, "/remote-only/folder", "/tmp/dest", mock.Mock(), mock.Mock())
    transfer.assert_called_once()


def test_scrolled_mouse_click_accounts_for_viewport_offset():
    # The helper is used by both list views to translate screen -> entry index.
    assert explorer.list_index_from_screen(2, 2, 7) == 7


def test_remote_listing_keeps_cd_failure_as_failure():
    remote = explorer.Remote("user@example.com")
    completed = subprocess.CompletedProcess([], 1, "", "No such file")
    with mock.patch.object(remote, "run", return_value=completed) as run:
        entries, error = remote.list_dir("/missing")
    assert entries == []
    assert "No such directory" in error
    assert "&& (ls -la" in run.call_args.args[0]


def test_file_preview_is_bounded():
    remote = explorer.Remote("user@example.com")
    completed = subprocess.CompletedProcess([], 0, "preview", "")
    with mock.patch.object(remote, "run", return_value=completed) as run:
        assert remote.read_file("/var/log/app.log") == "preview"
    assert run.call_args.args[0] == "head -c 262144 /var/log/app.log"


def test_local_picker_tab_in_file_mode_returns_selected_file_not_parent():
    class Screen:
        def __init__(self):
            self.keys = iter([explorer.curses.KEY_DOWN, ord("\t")])

        def getmaxyx(self):
            return 24, 80

        def timeout(self, *_):
            pass

        def erase(self):
            pass

        def addstr(self, *_):
            pass

        def refresh(self):
            pass

        def getch(self):
            return next(self.keys)

        def move(self, *_):
            pass

    with tempfile.TemporaryDirectory() as directory:
        selected = Path(directory) / "upload.txt"
        selected.write_text("payload")
        with mock.patch.object(explorer.curses, "curs_set"):
            result = explorer.local_picker(Screen(), "file", directory)
    assert result == str(selected)


def test_local_picker_file_mode_tab_returns_marked_files():
    class Screen:
        def __init__(self):
            self.keys = iter([explorer.curses.KEY_DOWN, ord(" "), ord("\t")])

        def getmaxyx(self):
            return 24, 80

        def timeout(self, *_):
            pass

        def erase(self):
            pass

        def addstr(self, *_):
            pass

        def refresh(self):
            pass

        def getch(self):
            return next(self.keys)

        def move(self, *_):
            pass

    with tempfile.TemporaryDirectory() as directory:
        selected = Path(directory) / "upload.txt"
        selected.write_text("payload")
        with mock.patch.object(explorer.curses, "curs_set"):
            result = explorer.local_picker(Screen(), "file", directory)
    assert result == [str(selected)]


def test_push_pv_progress_uses_percentage_against_file_size():
    remote = mock.Mock()
    remote.target = "user@example.com"
    remote.ssh_prefix.return_value = (["ssh"], None)
    remote.quote_path.side_effect = lambda p: p

    pv = mock.Mock()
    pv.stdin = mock.Mock()
    pv.stderr.readline.side_effect = [b"50\n", b""]
    pv.wait.return_value = 0
    ssh = mock.Mock()
    ssh.wait.return_value = 0

    progress = []
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "small.txt"
        source.write_bytes(b"1234567890")
        with mock.patch.object(explorer, "which", return_value="/usr/bin/pv"):
            with mock.patch.object(explorer.subprocess, "Popen", side_effect=[pv, ssh]):
                ok, _ = explorer.do_push(
                    remote, str(source), "/remote/small.txt", mock.Mock(),
                    lambda _, p: progress.append((p.done, p.pct)))

    assert ok is True
    assert (5.0, 50) in progress
    assert progress[-1] == (10, 100)
