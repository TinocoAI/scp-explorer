#!/usr/bin/env python3
"""SCP Explorer — a navigable file browser for the remote host behind the
current SSH pane (MobaXterm-style).

Runs inside a Herdr plugin pane. Detection:
  - The opening action passes the SSH pane id via $HERDR_SCP_PANE (a plain
    pane id, no JSON — immune to shell-quoting corruption) and an optional
    starting path via $HERDR_SCP_PATH.
  - If those are absent, the explorer scans the current tab for a pane whose
    foreground process is `ssh`, and uses that.

Controls:
  Up / Down / j / k .... move selection
  Enter / Right ........ open file (view) or cd into directory
  Left / h ............. go up one directory
  / .................... jump to a remote path (supports ~)
  g .................... go to remote home (~)
  r .................... refresh listing
  c .................... copy selected file/dir to a local path (scp)
  p .................... push selected file/dir from local to remote
  P .................... set/change the SSH password (sshpass)
  q / Esc .............. quit
"""
import curses
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from shutil import which

# Prefer the running Herdr binary (portable across Unix sockets and Windows
# named pipes); fall back to "herdr" on PATH for non-Herdr launches.
HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"


def _state_dir():
    """Durable, per-user plugin state directory.

    Herdr sets HERDR_PLUGIN_STATE_DIR for installed/linked plugins; use it so
    we never write into the (managed, possibly read-only) plugin source root or
    into /tmp. Fall back to a config location when run outside Herdr.
    """
    d = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if d:
        return d
    return os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        "herdr", "plugins", "state", "scp-explorer")


STATE_DIR = _state_dir()
LOG_PATH = os.path.join(STATE_DIR, "scp-explorer.log")

# --- Mouse scroll button resolution (portable: macOS / Linux / Windows) ---
# The wheel is reported as BUTTON4 (up) / BUTTON5 (down). Curses builds differ:
#   * standard ncurses / PDCurses(Windows): BUTTON1=1 BUTTON2=2 BUTTON3=4
#     BUTTON4=8 BUTTON5=16.
#   * macOS stock curses (Python from Xcode): BUTTON1=2 BUTTON2=128 BUTTON3=8192
#     BUTTON4=524288 (1<<19), each successive button is <<6, and BUTTON5_PRESSED
#     is simply absent.
# We collect every plausible bit for BUTTON4 and BUTTON5 and test them in OR, so
# the code works unchanged on any of those platforms. If a terminal reports the
# wheel-down with a bit we don't recognise (some macOS terminal emulators do),
# the first such event is *learned* and persisted to disk so it works forever.
_BUTTON4_CONST = getattr(curses, "BUTTON4_PRESSED", 0)
_BUTTON5_CONST = getattr(curses, "BUTTON5_PRESSED", 0)
# All plausible BUTTON4 (up) bit values across builds.
BUTTON4_CANDIDATES = [b for b in (
    _BUTTON4_CONST, 1 << 19, 8, 1 << 3, 1 << 1, 2
) if b]
# All plausible BUTTON5 (down) bit values across builds.
BUTTON5_CANDIDATES = [b for b in (
    _BUTTON5_CONST, 1 << 25, 1 << 4, 16, _BUTTON4_CONST << 6, _BUTTON4_CONST << 1
) if b]
# All known button-PRESS bits for buttons 1..3 across builds (we never treat
# buttons 4/5 as clicks — they are wheel/scroll). ncurses/PDCurses: 1/2/4;
# macOS stock curses: 2/128/8192. A click sets one of these.
_CLICK_BITS = 0x7 | (1 << 7) | (1 << 13)  # 1|2|4 | 128 | 8192
# Largest known up-bit; a wheel-down must be strictly higher than this so we
# never mistake a middle/right click for a scroll.
_UP_MAX = max(BUTTON4_CANDIDATES) if BUTTON4_CANDIDATES else 0
# Learned down-bit (populated at runtime / from disk).
_LEARNED_DOWN = [0]

# Persist the learned bit so it survives restarts (per-user state dir).
_LEARN_PATH = os.path.join(STATE_DIR, "mouse_down_bit")


def _load_learned_down():
    global _LEARNED_DOWN
    try:
        with open(_LEARN_PATH) as f:
            v = int(f.read().strip(), 0)
        if v > _UP_MAX:
            _LEARNED_DOWN[0] = v
    except Exception:
        pass


def _save_learned_down(v):
    try:
        os.makedirs(os.path.dirname(_LEARN_PATH), exist_ok=True)
        with open(_LEARN_PATH, "w") as f:
            f.write("0x%x\n" % v)
    except Exception:
        pass


_load_learned_down()


def mouse_scroll_dir(mstate):
    """Return -1 for wheel-up, +1 for wheel-down, or 0 if not a scroll event.

    Works on macOS, Linux (ncurses) and Windows (PDCurses). Any unknown wheel
    bit seen for the first time (and higher than the known up-bit) is learned
    as the down-bit and persisted.
    """
    for b in BUTTON4_CANDIDATES:
        if mstate & b:
            return -1
    for b in BUTTON5_CANDIDATES:
        if mstate & b:
            return 1
    if _LEARNED_DOWN[0] and (mstate & _LEARNED_DOWN[0]):
        return 1
    # Unknown event: isolate its single highest set bit. If that bit is strictly
    # higher than any known up-bit and not a low click bit, treat it as a
    # wheel-down and remember it.
    if mstate and not (mstate & _CLICK_BITS):
        hi = 1 << (mstate.bit_length() - 1)
        if hi > _UP_MAX:
            _LEARNED_DOWN[0] = hi
            _save_learned_down(hi)
            return 1
    return 0


def is_mouse_click(mstate):
    """True only for an actual left/middle/right button click (not scroll)."""
    if mouse_scroll_dir(mstate) != 0:
        return False  # wheel events are never clicks
    # A click sets one of the low 3 button bits (1/2/4). A release clears them.
    return (mstate & _CLICK_BITS) != 0


def mouse_debug(mstate):
    """If HERDR_SCP_MOUSE_DEBUG is set, append the raw bstate to a log file.

    Useful for diagnosing a terminal that reports the wheel with an unexpected
    bit: run once with the env var, scroll the wheel, then inspect the log.
    """
    if not os.environ.get("HERDR_SCP_MOUSE_DEBUG"):
        return
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "scp-explorer-mouse.log"), "a") as f:
            f.write("mstate=%d (0x%X) dir=%d is_click=%s\n"
                     % (mstate, mstate, mouse_scroll_dir(mstate), is_mouse_click(mstate)))
    except Exception:
        pass


def log(*a):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(" ".join(str(x) for x in a) + "\n")
    except Exception:
        pass


def load_data():
    raw = os.environ.get("HERDR_PLUGIN_DATA")
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            log("HERDR_PLUGIN_DATA unparseable:", repr(raw[:120]))
    return {}


def run_herdr(args):
    r = subprocess.run([HERDR] + args, capture_output=True, text=True, timeout=15)
    return r.returncode, r.stdout, r.stderr


def parse_ssh(argv):
    if not argv or argv[0] != "ssh":
        return None
    args = argv[1:]
    port = None
    jump = None
    target = None
    flag_with_value = {
        "-p", "-P", "-J", "--jump-host", "-o", "-i", "-F", "-c", "-b", "-m",
        "-e", "-W", "-E", "-L", "-R", "-D", "-l", "-B", "-S", "-O",
    }
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-p", "-P", "-J", "--jump-host"):
            val = args[i + 1] if i + 1 < len(args) else None
            if a in ("-p", "-P"):
                port = val
            else:
                jump = val
            i += 2
            continue
        if a.startswith("-"):
            i += 2 if a in flag_with_value else 1
            continue
        target = a
        break
    if not target:
        return None
    if ":" in target and not target.startswith("["):
        target = target.split(":", 1)[0]
    return {"target": target, "port": port, "jump": jump}


def detect_from_pane(pane_id):
    rc, out, _ = run_herdr(["pane", "process-info", "--pane", pane_id])
    if rc != 0:
        log("process-info failed for", pane_id, out)
        return None
    try:
        procs = json.loads(out)["result"]["process_info"]["foreground_processes"]
    except (ValueError, KeyError):
        return None
    for p in procs:
        t = parse_ssh(p.get("argv", []))
        if t:
            return t
    return None


def detect_from_tab():
    rc, out, _ = run_herdr(["pane", "current"])
    if rc != 0:
        return None, None
    try:
        cur = json.loads(out)["result"]["pane"]
        tab_id = cur.get("tab_id")
        self_id = cur.get("pane_id")
    except (ValueError, KeyError):
        return None, None
    rc, out, _ = run_herdr(["pane", "list"])
    if rc != 0:
        return None, None
    try:
        panes = json.loads(out)["result"]["panes"]
    except (ValueError, KeyError):
        return None, None
    for p in panes:
        if p.get("tab_id") != tab_id or p.get("pane_id") == self_id:
            continue
        t = detect_from_pane(p["pane_id"])
        if t:
            return t, p.get("pane_id")
    return None, None


class Remote:
    def __init__(self, target, port=None, jump=None, password=None):
        self.target = target
        self.port = port
        self.jump = jump
        self.password = password

    def ssh_prefix(self):
        """SSH (or sshpass+ssh) prefix WITHOUT the target host. Reused by
        streaming single-file transfers so the same auth/options apply."""
        args = ["ssh"]
        if self.port:
            args += ["-p", str(self.port)]
        if self.jump:
            args += ["-J", self.jump]
        args += ["-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
                 "-o", "ServerAliveCountMax=3"]
        if self.password:
            args += ["-o", "PreferredAuthentications=keyboard-interactive,password,publickey"]
        else:
            args += ["-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey"]
        if self.password:
            return ["sshpass", "-e"] + args, dict(os.environ, SSHPASS=self.password)
        return args, None

    def ssh_base(self):
        # ControlMaster auto (set in ~/.ssh/config) lets us reuse the active
        # session's authenticated connection when one exists.
        prefix, _ = self.ssh_prefix()
        return prefix + [self.target]

    def quote_path(self, path):
        return shlex.quote(path)

    def run(self, cmd, want_stderr=False):
        full = self.ssh_base() + [cmd]
        if self.password:
            full = ["sshpass", "-e"] + full
            env = dict(os.environ, SSHPASS=self.password)
            return subprocess.run(full, capture_output=True, text=True, timeout=25, env=env)
        return subprocess.run(full, capture_output=True, text=True, timeout=20)

    def test_auth(self):
        """Return (ok, message). ok=False with 'PASSWORD' if a password is needed."""
        r = self.run("true")
        if r.returncode == 0:
            return True, ""
        err = (r.stderr or "").lower()
        if "permission denied" in err or "password" in err or "authenticate" in err:
            return False, "PASSWORD"
        return False, r.stderr.strip() or "connection failed"

    def _scp_base_args(self):
        args = ["scp", "-r", "-o", "ConnectTimeout=15",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
        if self.port:
            args += ["-P", str(self.port)]
        if self.jump:
            args += ["-J", self.jump]
        if self.password:
            args += ["-o", "PreferredAuthentications=keyboard-interactive,password,publickey"]
        else:
            args += ["-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey"]
        return args

    def _scp_env(self):
        if self.password:
            return dict(os.environ, SSHPASS=self.password)
        return None

    def local_copy(self, remote_path, local_path):
        args = self._scp_base_args()[:]  # copy (no -r here; single file)
        args.remove("-r")
        if self.password:
            args = ["sshpass", "-e"] + args
        args += ["%s:%s" % (self.target, self.quote_path(remote_path)), local_path]
        env = self._scp_env()
        if env:
            return subprocess.run(args, capture_output=True, text=True, timeout=60, env=env)
        return subprocess.run(args, capture_output=True, text=True, timeout=60)

    def push(self, local_path, remote_path):
        args = self._scp_base_args()[:]
        args.remove("-r")
        if self.password:
            args = ["sshpass", "-e"] + args
        args += [local_path, "%s:%s" % (self.target, self.quote_path(remote_path))]
        env = self._scp_env()
        if env:
            return subprocess.run(args, capture_output=True, text=True, timeout=60, env=env)
        return subprocess.run(args, capture_output=True, text=True, timeout=60)

    def list_dir(self, path):
        # Use `ls -la` — a single fast pass. The previous approach ran `du -ks`
        # on every subdirectory, which is recursive and times out on big homes
        # (e.g. root's home with multi-GB dirs). `ls -la` is instant.
        # NOTE: `~` must NOT be quoted — the shell only expands a bare tilde,
        # not a quoted one. So we keep `~` unquoted and quote the rest.
        if path == "~":
            cd_arg = "~"
        elif path.startswith("~/"):
            cd_arg = "~" + self.quote_path(path[2:])
        else:
            cd_arg = self.quote_path(path)
        # No `2>/dev/null` on the cd so a bad path surfaces as an error.
        cmd = f"cd {cd_arg} && ls -la --time-style=+%s"
        r = self.run(cmd)
        if r.returncode != 0:
            err = (r.stderr or "").strip() or "remote listing failed"
            if "permission denied" in err.lower() or "password" in err.lower():
                return [], "PASSWORD"
            if "no such file" in err.lower() or "not a directory" in err.lower() or "can't cd" in err.lower():
                return [], "No such directory: %s" % path
            return [], err
        entries = []
        for line in r.stdout.splitlines():
            # perms links owner group size date name  (name may contain spaces,
            # so split into at most 7 tokens: 6 metadata fields + full name).
            parts = line.split(None, 6)
            if len(parts) < 7:
                continue
            if parts[0] == "total":  # first line of `ls -la`
                continue
            name = parts[-1]
            if name in (".", ".."):  # skip self / parent; we add ".." ourselves
                continue
            perms = parts[0]
            size = int(parts[4] or 0)
            if perms.startswith("d"):
                typ = "d"
            elif perms.startswith("l"):
                typ = "l"
                if " -> " in name:
                    name = name.split(" -> ")[0]
            else:
                typ = "f"
            entries.append({"name": name, "type": typ, "size": size})
        # ".." to go up one level (not produced by ls in a normal dir).
        entries.insert(0, {"name": "..", "type": "up", "size": 0})
        order = {"up": 0, "d": 1, "l": 2, "f": 3}
        entries.sort(key=lambda e: (order.get(e["type"], 9), e["name"].lower()))
        return entries, None

    def read_file(self, path):
        return self.run("cat %s" % self.quote_path(path)).stdout


def fmt_size(n):
    for unit in ["B", "K", "M", "G", "T"]:
        if abs(n) < 1024:
            return "%3.0f%s" % (n, unit) if unit == "B" else "%3.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fP" % n


def human(path):
    return path.replace(os.path.expanduser("~"), "~")


class Progress:
    """Tracks a transfer for a live progress bar. `total` may be None when
    unknown (directory copies) — we then show an indeterminate spinner bar."""
    def __init__(self, total):
        self.total = total          # bytes, or None
        self.done = 0               # bytes
        self.start = time.time()
        self.last = self.start
        self.last_done = 0
        self.rate = 0.0             # bytes/sec (smoothed)

    def update(self, delta):
        now = time.time()
        self.done += delta
        dt = now - self.last
        if dt >= 0.4:               # recompute rate ~2.5x/sec
            inst = (self.done - self.last_done) / dt if dt > 0 else 0
            # exponential smoothing so the displayed speed isn't jumpy
            self.rate = self.rate * 0.6 + inst * 0.4
            self.last = now
            self.last_done = self.done

    @property
    def frac(self):
        if not self.total:
            return 0.0
        return min(1.0, self.done / self.total) if self.total else 0.0

    @property
    def pct(self):
        return int(self.frac * 100)

    @property
    def eta(self):
        if self.total and self.rate > 0:
            rem = (self.total - self.done) / self.rate
            return rem
        return None


def fmt_eta(sec):
    if sec is None:
        return ""
    sec = int(sec)
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm%02ds" % (sec // 60, sec % 60)
    return "%dh%02dm" % (sec // 3600, (sec % 3600) // 60)


def draw_progress(stdscr, label, prog, width=None, bar_only=False):
    """Render a progress bar on the bottom line of the pane.

    Shows: [####------] 42%  1.2M/s  eta 3s  (label)
    When total is unknown: an animated indeterminate bar + live speed.
    Auto-abbreviates the label/numbers to fit the pane width."""
    h, w = stdscr.getmaxyx()
    y = h - 1
    if width is None:
        width = w
    # Reserve fixed columns for the bar + percentage.
    bar_cols = max(10, min(40, (width - 20) // 2))
    pct = prog.pct
    if prog.total:
        filled = int(round(prog.frac * bar_cols))
        bar = "#" * filled + "-" * (bar_cols - filled)
    else:
        # indeterminate: a moving block
        seg = max(3, bar_cols // 4)
        pos = int((time.time() * 1.5)) % (bar_cols - seg + 1)
        bar = "-" * pos + "#" * seg + "-" * (bar_cols - pos - seg)
        pct = None
    rate = fmt_size(prog.rate) + "/s"
    eta = ("eta " + fmt_eta(prog.eta)) if prog.eta is not None else ""
    head = "[%s] %s" % (bar, ("%3d%%" % pct) if pct is not None else "   ?")
    tail = "  %s  %s" % (rate, eta)
    text = head + tail + "  " + label
    if len(text) > width - 1:
        avail = width - 1 - len(head) - len(tail) - 2
        text = head + tail + "  " + label[:max(0, avail)]
    try:
        stdscr.addstr(y, 0, " " * (width - 1))
        stdscr.addstr(y, 0, text[:width - 1], curses.A_REVERSE)
    except curses.error:
        pass
    stdscr.refresh()


def do_get(remote, remote_path, local_path, stdscr, on_progress):
    """Download remote_path -> local_path (file or directory).

    Single files stream through `ssh … cat` (optionally via pv) for a real
    percentage bar + speed. Directories fall back to `scp -r` with an
    indeterminate bar + speed. `on_progress(label, prog)` draws the bar on
    the pane bottom line. Returns (ok, message)."""
    name = os.path.basename(remote_path.rstrip("/")) or remote_path
    on_progress("get %s" % name, Progress(None))
    if os.path.isdir(remote_path):
        # directory copy: scp -r, indeterminate
        return _scp_transfer(remote, "get", remote_path, local_path,
                              stdscr, on_progress)
    prefix, env = remote.ssh_prefix()
    ssh_cmd = prefix + [remote.target, "cat " + remote.quote_path(remote_path)]
    tmp = local_path + ".part"
    if which("pv"):
        pipe = subprocess.Popen(["pv", "-bnp", "-i", "0.3", "-c"],
                                stdin=subprocess.PIPE, stdout=open(tmp, "wb"),
                                stderr=subprocess.PIPE)
        proc = subprocess.Popen(ssh_cmd, stdout=pipe.stdin,
                                stderr=subprocess.DEVNULL, env=env)
        prog = Progress(None)
        while True:
            line = pipe.stderr.readline()
            if not line:
                break
            try:
                prog.done = int(line.strip())
            except ValueError:
                pass
            on_progress("get %s" % name, prog)
        return _stream_collect(proc, pipe, tmp, local_path, name, "get")
    # no pv on this host: probe remote size so the bar can show a real %,
    # then fall back to ssh-cat, measuring bytes written to the temp file.
    total = None
    try:
        r = remote.run("stat -c %s " + remote.quote_path(remote_path))
        if r.returncode == 0:
            total = int(r.stdout.strip())
    except Exception:
        total = None
    proc = subprocess.Popen(ssh_cmd, stdout=open(tmp, "wb"),
                            stderr=subprocess.DEVNULL, env=env)
    prog = Progress(total)
    while proc.poll() is None:
        try:
            prog.done = os.path.getsize(tmp)
        except OSError:
            prog.done = 0
        prog.update(0)  # recompute rate from elapsed time
        on_progress("get %s" % name, prog)
    return _stream_collect(proc, None, tmp, local_path, name, "get")


def _stream_collect(proc, pipe, tmp, local_path, name, direction):
    rc = proc.wait()
    if pipe is not None:
        pipe.wait()
    if rc == 0 and tmp:
        os.replace(tmp, local_path)
        return True, "got %s -> %s" % (name, human(local_path))
    if tmp:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return False, "ERR: transfer failed (rc=%d)" % rc


def _scp_transfer(remote, direction, src, dst, stdscr, on_progress):
    """scp -r fallback for directories (no per-file percentage possible).
    Shows an indeterminate bar + a live speed gleaned from a size probe
    (`du`) so the % advances roughly with actual bytes copied."""
    name = os.path.basename(src.rstrip("/")) or src
    args = remote._scp_base_args()
    if direction == "get":
        args += ["%s:%s" % (remote.target, remote.quote_path(src)), dst]
        # probe total size so the bar can show a real percentage.
        total = None
        try:
            r = remote.run("du -ks %s" % remote.quote_path(src))
            if r.returncode == 0:
                total = int(r.stdout.split()[0]) * 1024
        except Exception:
            total = None
    else:
        args += [src, "%s:%s" % (remote.target, remote.quote_path(dst))]
        try:
            total = sum(os.path.getsize(os.path.join(dp, f))
                        for dp, _, fs in os.walk(src) for f in fs)
        except Exception:
            total = None
    prog = Progress(total)
    on_progress("%s %s" % (direction, name), prog)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=remote._scp_env())
    # scp is silent, so advance the bar by an estimate: if we know the total,
    # interpolate by elapsed time scaled by a steady ramp; otherwise it stays
    # indeterminate. We still recompute the live rate from elapsed time.
    started = time.time()
    while True:
        ch = proc.stdout.read(4096)
        if not ch:
            break
        if total:
            elapsed = time.time() - started
            # steady linear estimate — good enough for a directory progress bar
            prog.done = min(total, total * _ramp(elapsed))
            on_progress("%s %s" % (direction, name), prog)
    rc = proc.wait()
    if rc == 0:
        verb = "got" if direction == "get" else "pushed"
        return True, "%s %s -> %s" % (verb, name, human(dst))
    return False, "ERR: %s failed (rc=%d)" % (direction, rc)


def _ramp(elapsed):
    # soft ramp so the bar doesn't sit at 0 or 100 too long; clamps to <1
    # until the process actually exits (exit sets it to 100 via rc check).
    return min(0.98, elapsed / 8.0)


def _eta_guess(total):
    # rough divisor so the ramp spans a believable window for big copies
    return max(2.0, total / (200 * 1024))


def do_push(remote, local_path, remote_path, stdscr, on_progress):
    """Upload local_path -> remote_path (file or directory). Mirrors do_get."""
    name = os.path.basename(local_path.rstrip("/")) or local_path
    on_progress("push %s" % name, Progress(None))
    if os.path.isdir(local_path):
        return _scp_transfer(remote, "push", local_path, remote_path,
                             stdscr, on_progress)
    prefix, env = remote.ssh_prefix()
    ssh_cmd = prefix + [remote.target, "cat > " + remote.quote_path(remote_path)]
    if which("pv"):
        pipe = subprocess.Popen(["pv", "-bnp", "-i", "0.3", "-c"],
                                stdin=open(local_path, "rb"),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc = subprocess.Popen(ssh_cmd, stdin=pipe.stdout,
                                stderr=subprocess.DEVNULL, env=env)
        prog = Progress(None)
        while True:
            line = pipe.stderr.readline()
            if not line:
                break
            try:
                prog.done = int(line.strip())
            except ValueError:
                pass
            on_progress("push %s" % name, prog)
        return _stream_collect(proc, pipe, None, remote_path, name, "push")
    size = os.path.getsize(local_path)
    prog = Progress(size)
    src = open(local_path, "rb")
    proc = subprocess.Popen(ssh_cmd, stdin=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=env)
    sent = 0
    chunk = 65536
    while True:
        b = src.read(chunk)
        if not b:
            break
        try:
            proc.stdin.write(b)
        except (BrokenPipeError, ValueError):
            break
        sent += len(b)
        prog.done = sent
        on_progress("push %s" % name, prog)
        if proc.poll() is not None:
            break
    try:
        proc.stdin.close()
    except (BrokenPipeError, ValueError):
        pass
    src.close()
    rc = proc.wait()
    if rc == 0:
        return True, "pushed %s -> %s" % (name, human(remote_path))
    return False, "ERR: push failed (rc=%d)" % rc


def prompt_str(stdscr, label, default="", secret=False):
    """Line editor with an optional prefilled default. Avoids curses.textpad
    (not available in every curses build). Supports insert, backspace, left/
    right, Enter (confirm) and Esc (cancel).
    When `secret=True` the typed characters are masked with '*' (used for
    passwords) so they never appear in clear text on screen."""
    h, w = stdscr.getmaxyx()
    y = h - 1
    x0 = len(label)
    buf = list(default)
    pos = len(buf)
    # Blocking input for the prompt (restore the 400ms poll afterwards).
    stdscr.timeout(-1)
    curses.curs_set(1)
    while True:
        try:
            stdscr.addstr(y, 0, " " * (w - 1))
            stdscr.addstr(y, 0, label)
            if secret:
                # mask every character (including default) with '*'
                visible = "*" * len(buf)
            else:
                visible = "".join(buf)
            stdscr.addstr(y, x0, visible[:w - x0 - 1])
            stdscr.addstr(y, x0 + len(visible), " " * max(0, w - x0 - len(visible) - 1))
            stdscr.move(y, min(w - 1, x0 + pos))
        except curses.error:
            pass
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            break
        if ch == 27:  # Esc cancels
            buf = []
            break
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            if pos > 0:
                buf.pop(pos - 1)
                pos -= 1
        elif ch == curses.KEY_LEFT:
            if pos > 0:
                pos -= 1
        elif ch == curses.KEY_RIGHT:
            if pos < len(buf):
                pos += 1
        elif 32 <= ch < 127:
            buf.insert(pos, chr(ch))
            pos += 1
        # other keys (non-ascii, etc.) ignored
    curses.curs_set(0)
    stdscr.timeout(400)
    return "".join(buf).strip()


def remote_cwd_of_pane(pane_id):
    """Read the remote CWD from the source pane's terminal title, e.g.
    'root@host: /var/log' -> '/var/log'. Returns None if not detectable.
    If HERDR_SCP_CWD_FILE is set, read the CWD from that file instead
    (used for testing / external control)."""
    mock = os.environ.get("HERDR_SCP_CWD_FILE")
    if mock and os.path.exists(mock):
        try:
            with open(mock) as f:
                v = f.read().strip()
            return v or None
        except OSError:
            return None
    rc, out, _ = run_herdr(["pane", "get", pane_id])
    if rc != 0:
        return None
    try:
        pane = json.loads(out)["result"]["pane"]
    except (ValueError, KeyError):
        return None
    title = pane.get("terminal_title_stripped") or pane.get("terminal_title") or ""
    if ":" not in title:
        return None
    after = title.split(":", 1)[1].strip()
    if not after:
        return None
    return after  # may be "~" or an absolute path


def local_list(path):
    entries = []
    try:
        for e in os.scandir(path):
            if e.is_dir(follow_symlinks=False):
                typ = "d"
            elif e.is_symlink():
                typ = "l"
            else:
                typ = "f"
            try:
                size = e.stat(follow_symlinks=False).st_size if typ == "f" else 0
            except OSError:
                size = 0
            entries.append({"name": e.name, "type": typ, "size": size})
    except OSError as ex:
        return [], str(ex)
    entries.insert(0, {"name": "..", "type": "up", "size": 0})
    order = {"up": 0, "d": 1, "l": 2, "f": 3}
    entries.sort(key=lambda x: (order.get(x["type"], 9), x["name"].lower()))
    return entries, None


def local_picker(stdscr, mode, start=None):
    """Browse the LOCAL filesystem and pick a path.
    mode='dir'  -> pick a directory (used as download target); confirm with Tab.
    mode='file' -> pick a file (used as push source); confirm with Enter.
    Returns the chosen path, or None if cancelled (Esc/q)."""
    lcur = os.path.abspath(start or os.path.expanduser("~"))
    lentries, lerr = local_list(lcur)
    lsel = 0
    ltop = 0  # first visible entry (viewport scroll offset)
    stdscr.timeout(-1)  # blocking while picking

    def clamp_lview():
        nonlocal ltop, lsel
        h, _ = stdscr.getmaxyx()
        visible = max(0, (h - 1) - 2)  # rows from top(2) to h-1 (status line)
        if visible <= 0:
            return
        if lsel < ltop:
            ltop = lsel
        elif lsel >= ltop + visible:
            ltop = lsel - visible + 1
        if ltop < 0:
            ltop = 0
        if ltop > max(0, len(lentries) - visible):
            ltop = max(0, len(lentries) - visible)
    while True:
        h, w = stdscr.getmaxyx()
        clamp_lview()
        stdscr.erase()
        hint = ("Tab/click-header=choose dir · click=sel · 2x=open · g=home · /=jump · Esc=cancel"
                if mode == "dir" else
                "Enter/2x-click=choose file · click=open dir · g=home · /=jump · Esc=cancel")
        title = " LOCAL %s  [%s]" % ("dir" if mode == "dir" else "file", lcur)
        try:
            stdscr.addstr(0, 0, (title + "  " + hint)[:w - 1], curses.A_REVERSE)
        except curses.error:
            pass
        top = 2
        for i in range(top, h - 1):
            idx = i - top + ltop
            if idx >= len(lentries):
                break
            e = lentries[idx]
            icon = {"up": "..", "d": "▸", "l": "→", "f": " "}.get(e["type"], " ")
            size = "" if e["type"] in ("d", "up") else fmt_size(e["size"])
            line = " %s %s  %s" % (icon, e["name"], size)
            attr = curses.A_REVERSE if idx == lsel else curses.A_NORMAL
            try:
                stdscr.addstr(i, 0, line[:w - 1], attr)
            except curses.error:
                pass
        if lerr:
            try:
                stdscr.addstr(h - 1, 0, ("ERR: " + lerr)[:w - 1], curses.A_BOLD)
            except curses.error:
                pass
        stdscr.refresh()
        ch = stdscr.getch()
        if ch == -1:  # defensive: never spin on a poll timeout
            continue
        if ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, mstate = curses.getmouse()
            except Exception:
                continue
            mouse_debug(mstate)
            top = 2
            clicked = my - top
            in_list = (top <= my <= h - 2) and (0 <= clicked < len(lentries))
            # 1) Scroll (wheel) always moves the selection and never activates.
            sdir = mouse_scroll_dir(mstate)
            if sdir != 0:
                if sdir < 0 and lsel > 0:
                    lsel -= 1
                elif sdir > 0 and lsel < len(lentries) - 1:
                    lsel += 1
                continue
            # 2) Only a real left-click can select / activate.
            if not is_mouse_click(mstate):
                continue
            if in_list:
                if clicked == lsel:
                    # second click on the same row activates it
                    e = lentries[lsel]
                    if e["type"] == "up":
                        lcur = os.path.dirname(lcur.rstrip("/")) or "/"
                        lentries, lerr = local_list(lcur)
                        lsel = 0
                    elif e["type"] in ("d", "l"):
                        lcur = os.path.join(lcur, e["name"])
                        lentries, lerr = local_list(lcur)
                        lsel = 0
                    elif e["type"] == "f":
                        if mode == "file":
                            stdscr.timeout(400)
                            return os.path.join(lcur, e["name"])
                        # dir mode: files are not valid dir targets; ignore
                else:
                    lsel = clicked
            elif my == 0 and mode == "dir":
                # clicking the header (current dir) confirms it as the target
                stdscr.timeout(400)
                return lcur
            continue
        if ch in (27, ord('q')):
            stdscr.timeout(400)
            return None
        elif ch == curses.KEY_DOWN:
            lsel = min(len(lentries) - 1, lsel + 1)
        elif ch == curses.KEY_UP:
            lsel = max(0, lsel - 1)
        elif ch in (curses.KEY_NPAGE,):
            lsel = min(len(lentries) - 1, lsel + 10)
        elif ch in (curses.KEY_PPAGE,):
            lsel = max(0, lsel - 10)
        elif ch in (curses.KEY_ENTER, 10, 13):
            if not lentries:
                continue
            e = lentries[lsel]
            if e["type"] == "up":
                lcur = os.path.dirname(lcur.rstrip("/")) or "/"
                lentries, lerr = local_list(lcur)
                lsel = 0
            elif e["type"] in ("d", "l"):
                lcur = os.path.join(lcur, e["name"])
                lentries, lerr = local_list(lcur)
                lsel = 0
            elif e["type"] == "f":
                if mode == "file":
                    stdscr.timeout(400)
                    return os.path.join(lcur, e["name"])
                # dir mode: files are not valid dir targets; ignore
        elif ch == ord('\t') or (mode == "dir" and ch in (ord(' '),)):
            # confirm current directory as the target
            stdscr.timeout(400)
            return lcur
        elif ch == ord('g'):
            lcur = os.path.expanduser("~")
            lentries, lerr = local_list(lcur)
            lsel = 0
        elif ch == ord('/'):
            p = prompt_str(stdscr, "local path> ", default="/")
            if p and os.path.isdir(p):
                lcur = os.path.abspath(p)
                lentries, lerr = local_list(lcur)
                lsel = 0
    stdscr.timeout(400)
    return None


def main(stdscr):
    curses.curs_set(0)
    stdscr.keypad(True)
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    except Exception:
        pass  # mouse may be unsupported on some terminals; keyboard still works
    stdscr.timeout(400)  # poll for follow ~2.5x/sec without blocking input
    log("=== explorer start ===")

    # 1) explicit pane from env (plain id, safe)
    ssh_pane = os.environ.get("HERDR_SCP_PANE")
    start_path = os.environ.get("HERDR_SCP_PATH") or "~"
    target_obj = None
    if ssh_pane:
        target_obj = detect_from_pane(ssh_pane)
        log("detect from env pane", ssh_pane, "->", target_obj)
    # 2) scan tab
    if not target_obj:
        target_obj, _ = detect_from_tab()
        log("detect from tab ->", target_obj)
    # 2b) direct target override (debug / manual): HERDR_SCP_TARGET=user@host
    if not target_obj:
        dbg = os.environ.get("HERDR_SCP_TARGET")
        if dbg:
            target_obj = {"target": dbg, "port": None, "jump": None}
            log("detect from HERDR_SCP_TARGET ->", target_obj)
    # 3) legacy JSON
    if not target_obj:
        d = load_data()
        if d.get("target"):
            target_obj = {"target": d["target"], "port": d.get("port"), "jump": d.get("jump")}
            start_path = d.get("path") or start_path
            log("detect from JSON ->", target_obj)

    if not target_obj:
        msg = ("No active SSH session found in this tab.\n\n"
               "Focus a pane where `ssh` is the foreground process (not just a\n"
               "stale prompt showing a remote host), then press prefix+f again.\n\n"
               "Press q to close.")
        stdscr.addstr(0, 0, msg)
        while True:
            if stdscr.getch() in (ord('q'), 27):
                return

    remote = Remote(target_obj["target"], target_obj.get("port"), target_obj.get("jump"))
    cur = start_path
    log("remote target", remote.target, "start", cur)
    # Follow mode: track the source pane's CWD when it changes.
    follow = bool(ssh_pane)
    last_follow_cwd = None

    entries = []
    err = None
    sel = 0
    top_idx = 0  # first visible entry (viewport scroll offset)
    msg_line = "Connecting to %s ..." % remote.target

    need_pw = False

    def reload():
        nonlocal entries, err, sel, need_pw, msg_line, top_idx
        entries, err = remote.list_dir(cur)
        sel = 0
        top_idx = 0
        if err == "PASSWORD":
            need_pw = True
            msg_line = "auth needs password — press P (or it will prompt)"
        elif err:
            msg_line = "ERR: %s" % err
        else:
            msg_line = ""
        log("reload", cur, "entries", len(entries), "err", err)

    def activate_selected():
        """Open/view/enter the currently selected entry (same as Enter/Right)."""
        nonlocal sel, cur, err, msg_line, last_follow_cwd
        if not entries or sel >= len(entries):
            return
        e = entries[sel]
        if e["type"] == "up":
            cur = os.path.dirname(cur.rstrip("/")) or "/"
            if cur == "":
                cur = "/"
            reload()
        elif e["type"] in ("d", "l"):
            new = os.path.join(cur, e["name"])
            cur = os.path.normpath(new)
            reload()
        else:
            content = remote.read_file(os.path.join(cur, e["name"]))
            viewer(content, os.path.join(cur, e["name"]))

    reload()
    # Seed the follow tracker with the pane's CURRENT remote CWD (not our
    # start_path) so that follow only reacts to *future* changes made in the
    # SSH pane. This prevents the first navigation we do from being reverted
    # back to the pane's CWD by the follow logic.
    last_follow_cwd = remote_cwd_of_pane(ssh_pane) if ssh_pane else None

    def clamp_view():
        """Keep the selection visible: adjust top_idx (viewport offset)."""
        nonlocal top_idx, sel
        h, _ = stdscr.getmaxyx()
        visible = max(0, h - 2 - 2)  # rows from top(2) to h-2 (status line)
        if visible <= 0:
            return
        if sel < top_idx:
            top_idx = sel
        elif sel >= top_idx + visible:
            top_idx = sel - visible + 1
        if top_idx < 0:
            top_idx = 0
        if top_idx > max(0, len(entries) - visible):
            top_idx = max(0, len(entries) - visible)

    def draw():
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        header = " SCP: %s  [%s]%s%s" % (
            remote.target, human(cur),
            "  *password*" if remote.password else "",
            "  [follow]" if follow else "")
        try:
            stdscr.addstr(0, 0, header[:w - 1], curses.A_REVERSE)
        except curses.error:
            pass
        top = 2
        for i in range(top, h - 2):
            idx = i - top + top_idx
            if idx >= len(entries):
                break
            e = entries[idx]
            icon = {"up": "..", "d": "▸", "l": "→", "f": " "}.get(e["type"], " ")
            size = "" if e["type"] in ("d", "up") else fmt_size(e["size"])
            line = " %s %s  %s" % (icon, e["name"], size)
            attr = curses.A_REVERSE if idx == sel else curses.A_NORMAL
            try:
                stdscr.addstr(i, 0, line[:w - 1], attr)
            except curses.error:
                pass
        if msg_line:
            try:
                stdscr.addstr(h - 1, 0, msg_line[:w - 1], curses.A_BOLD)
            except curses.error:
                pass
        else:
            try:
                stdscr.addstr(h - 1, 0, ("↑↓ nav · Enter cd/open · ← up · / path · g home · "
                                         "f follow · r refresh · c get · p push · P pw · mouse: click=sel, 2x=open · q quit")[:w - 1])
            except curses.error:
                pass
        stdscr.refresh()

    def viewer(text, title):
        vh, vw = stdscr.getmaxyx()
        lines = text.splitlines() or [""]
        off = 0
        while True:
            stdscr.erase()
            try:
                stdscr.addstr(0, 0, (" view: " + title)[:vw - 1], curses.A_REVERSE)
            except curses.error:
                pass
            for i in range(1, vh - 1):
                idx = i - 1 + off
                if idx >= len(lines):
                    break
                try:
                    stdscr.addstr(i, 0, lines[idx][:vw - 1])
                except curses.error:
                    pass
            try:
                stdscr.addstr(vh - 1, 0, "↑↓ scroll · q close"[:vw - 1])
            except curses.error:
                pass
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (ord('q'), 27):
                break
            elif ch in (curses.KEY_DOWN, ord('j')):
                off += 1
            elif ch in (curses.KEY_UP, ord('k')):
                off = max(0, off - 1)
            elif ch == curses.KEY_NPAGE:
                off += vh - 2
            elif ch == curses.KEY_PPAGE:
                off = max(0, off - (vh - 2))

    # If the first load needs a password, prompt immediately.
    while need_pw:
        need_pw = False
        pw = prompt_str(stdscr, "SSH password for %s: " % remote.target, secret=True)
        if not pw:
            err = "PASSWORD_REQUIRED"
            break
        remote.password = pw
        reload()
        if err == "PASSWORD":
            need_pw = True
            msg_line = "auth failed, try again (P to retry)"

    while True:
        # Follow mode: if the source pane's remote CWD changed, follow it.
        if follow and ssh_pane:
            rcwd = remote_cwd_of_pane(ssh_pane)
            if rcwd and rcwd != cur and rcwd != last_follow_cwd:
                cur = rcwd
                last_follow_cwd = rcwd
                reload()
        clamp_view()
        draw()
        ch = stdscr.getch()
        if ch == -1:  # poll timeout, no key pressed
            continue
        if ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, mstate = curses.getmouse()
            except Exception:
                continue
            mouse_debug(mstate)
            h = stdscr.getmaxyx()[0]
            top = 2
            clicked = my - top
            in_list = (top <= my <= h - 2) and (0 <= clicked < len(entries))
            # 1) Scroll (wheel) always moves the selection and never activates.
            sdir = mouse_scroll_dir(mstate)
            if sdir != 0:
                if sdir < 0 and sel > 0:
                    sel -= 1
                elif sdir > 0 and sel < len(entries) - 1:
                    sel += 1
                continue
            # 2) Only a real left-click can select / activate.
            if not is_mouse_click(mstate):
                continue
            if in_list:
                if clicked == sel:
                    # a second click on the same row opens it
                    activate_selected()
                else:
                    sel = clicked
            # clicks outside the listing area (e.g. header) are ignored
            continue
        if ch in (ord('q'), 27):
            break
        elif ch in (curses.KEY_DOWN, ord('j')):
            if sel < len(entries) - 1:
                sel += 1
        elif ch in (curses.KEY_UP, ord('k')):
            if sel > 0:
                sel -= 1
        elif ch in (curses.KEY_NPAGE,):
            sel = min(len(entries) - 1, sel + 10)
        elif ch in (curses.KEY_PPAGE,):
            sel = max(0, sel - 10)
        elif ch in (curses.KEY_RIGHT, curses.KEY_ENTER, 10, 13):
            activate_selected()
        elif ch in (curses.KEY_LEFT, ord('h')):
            cur = os.path.dirname(cur.rstrip("/")) or "/"
            if cur == "":
                cur = "/"
            reload()
        elif ch == ord('g'):
            cur = "~"
            reload()
        elif ch == ord('r'):
            reload()
        elif ch == ord('f'):
            follow = not follow
            last_follow_cwd = None  # re-arm follow detection after a toggle
            msg_line = "follow %s" % ("ON" if follow else "OFF")
        elif ch == ord('/'):
            p = prompt_str(stdscr, "path> ", default="/")
            if p:
                cur = os.path.expanduser(p) if p.startswith("~") else p
                reload()
        elif ch == ord('P'):
            pw = prompt_str(stdscr, "SSH password (blank=clear): ", secret=True)
            remote.password = pw or None
            msg_line = "password %s" % ("set" if pw else "cleared")
            reload()
            if err == "PASSWORD":
                msg_line = "still needs password"
        elif ch == ord('c'):
            if not entries or sel >= len(entries) or entries[sel]["type"] == "up":
                continue
            e = entries[sel]
            dest = local_picker(stdscr, "dir", start=os.path.expanduser("~"))
            if dest:
                local_path = os.path.join(dest, e["name"])
                def _cb(label, prog):
                    draw_progress(stdscr, label, prog)
                try:
                    ok, res = do_get(remote,
                                     os.path.join(cur, e["name"]), local_path,
                                     stdscr, _cb)
                except Exception as ex:
                    log("GET error", repr(ex))
                    ok, res = False, "ERR: %s" % ex
                reload()
                if ok:
                    msg_line = "%s Downloaded Successfuly" % e["name"]
                elif "PASSWORD" in res:
                    msg_line = "auth error — press P to set password"
                else:
                    msg_line = res
        elif ch == ord('p'):
            src = local_picker(stdscr, "file", start=os.path.expanduser("~"))
            if src and os.path.exists(src):
                remote_path = os.path.join(cur, os.path.basename(src))
                def _cb(label, prog):
                    draw_progress(stdscr, label, prog)
                try:
                    ok, res = do_push(remote, src, remote_path, stdscr, _cb)
                except Exception as ex:
                    log("PUSH error", repr(ex))
                    ok, res = False, "ERR: %s" % ex
                reload()
                if ok:
                    msg_line = "%s Uploaded Successfuly" % os.path.basename(src)
                elif "PASSWORD" in res:
                    msg_line = "auth error — press P to set password"
                else:
                    msg_line = res


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    except Exception as ex:
        log("FATAL", repr(ex))
