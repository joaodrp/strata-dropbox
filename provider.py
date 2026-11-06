#!/usr/bin/python3
"""Strata file provider: Dropbox sync badges and Dropbox's own right-click menu.

Speaks Strata's external provider protocol (newline-delimited JSON on
stdin/stdout). Files inside your Dropbox folder get a badge for their sync
state, and the context menu offers the actions the Dropbox client itself lists
for the selection, as Nautilus's Dropbox extension does. It talks to the
running client over its local command socket, the one `dropbox-cli` and
`nautilus-dropbox` use.

https://github.com/joaodrp/strata-dropbox
"""
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import unquote

__version__ = "0.1.0"  # x-release-please-version

MAX_FRAME = 1024 * 1024
MAX_LINE = 64 * 1024  # one reply line from the Dropbox client
MAX_TICKETS = 64
MAX_NODES = 64  # Strata's cap on a provider's menu tree, branches included
MAX_DEPTH = 4
MAX_LINK_PATHS = 20  # Copy link creates links one by one; keep it well inside the deadline
MAX_WATCHED = 1000
TICKET_SECONDS = 120
WATCH_SECONDS = 3
SOCKET_TIMEOUT = 2
# Strata gives a request 8 seconds; stop asking Dropbox in time to reply.
DEADLINE_SECONDS = 5

# Dropbox's overlay states -> (badge, description, priority). States not here,
# such as "unwatched", get no badge.
STATES = {
    "up to date": ("synced", "Synced to Dropbox", 0),
    "syncing": ("syncing", "Syncing to Dropbox", 1),
    "unsyncable": ("error", "Dropbox cannot sync this", 2),
    "ignored": ("excluded", "Excluded from Dropbox sync", 0),
    "selective sync": ("excluded", "Excluded from Dropbox sync", 0),  # clients before v94
}

# Known client verbs -> (label, notice). A label of None keeps the client's
# own; Strata heads the actions with the provider name, so "Dropbox" is dropped
# where the client repeats it. An entry of None hides the verb:
# - context_menu_settings is the client's settings for this menu, not an action.
# - ignore_path ("Do not sync") removes the item from Dropbox.com and every
#   other device while keeping it here, and Strata cannot ask to confirm.
VERBS = {
    "copy_link": ("Copy link", "Link copied."),
    "browse": (None, "Opening on Dropbox.com."),
    "revisions": (None, "Opening version history on Dropbox.com."),
    "comment": (None, "Opening comments on Dropbox.com."),
    "ignore_path": None,
    "context_menu_settings": None,
}
ACTION_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
INVALID = {"error": "invalid-request", "message": "Invalid provider request."}


def dropbox_home():
    return os.path.expanduser("~/.dropbox")


def tool(name):
    return shutil.which(name, path="/usr/bin:/bin")


def dropbox_roots(home=None):
    """The personal and business Dropbox folders this client syncs."""
    try:
        with open(os.path.join(home or dropbox_home(), "info.json")) as f:
            info = json.load(f)
    except (OSError, ValueError):
        return []
    roots = []
    for account in ("personal", "business"):
        path = (info.get(account) or {}).get("path") if isinstance(info, dict) else None
        if isinstance(path, str) and os.path.isabs(path):
            roots.append(os.path.normpath(path))
    return roots


def inside(path, roots):
    path = os.path.normpath(path)  # so "<root>/../x" is not inside
    return any(path == root or path.startswith(root + "/") for root in roots)


def identity(path):
    """What the path names now, without following a final symlink."""
    s = os.lstat(path)
    return (s.st_dev, s.st_ino, s.st_mode)


def clean(text, limit=500):
    """Client text made safe for a Strata message: printable, bounded."""
    text = "".join(c if c.isprintable() else " " for c in str(text)).strip()
    return text if len(text) <= limit else text[:limit - 3] + "..."


class DaemonError(Exception):
    """The Dropbox client could not be reached or stopped answering.
    `submitted` is true once a command was sent, so it may have run."""

    def __init__(self, message, submitted=False):
        super().__init__(message)
        self.submitted = submitted


class DaemonRefused(DaemonError):
    """The client answered that it would not run the command."""


class DaemonTimeout(DaemonError):
    """The request's time ran out; what was gathered before still stands."""


class Daemon:
    """One connection to the Dropbox client's command socket. Commands are
    `name`, then `key<TAB>value` lines, then `done`; replies are `ok` or
    `notok`, then fields, then `done`."""

    def __init__(self, path=None, timeout=SOCKET_TIMEOUT, deadline=None):
        self.timeout = timeout
        # time.monotonic() by which every read and write must finish.
        self.deadline = deadline
        self.buffer = b""
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._limit()
            self.sock.connect(path or os.path.join(dropbox_home(), "command_socket"))
        except (OSError, DaemonTimeout) as exc:
            self.sock.close()
            raise DaemonError("Dropbox is not running") from exc

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _limit(self, submitted=False):
        """Wait no longer than the time left before the deadline."""
        remaining = self.timeout
        if self.deadline is not None:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise DaemonTimeout("Dropbox took too long", submitted=submitted)
            remaining = min(remaining, left)
        self.sock.settimeout(remaining)

    def _line(self):
        # Read straight from the socket, so the deadline bounds every receive.
        while b"\n" not in self.buffer:
            if len(self.buffer) > MAX_LINE:
                raise DaemonError("Dropbox sent an overlong reply", submitted=True)
            self._limit(submitted=True)
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout as exc:
                raise DaemonTimeout("Dropbox took too long", submitted=True) from exc
            if not chunk:
                raise DaemonError("Dropbox closed the connection", submitted=True)
            self.buffer += chunk
        line, _, self.buffer = self.buffer.partition(b"\n")
        if len(line) > MAX_LINE:
            raise DaemonError("Dropbox sent an overlong reply", submitted=True)
        return line.decode("utf-8", "replace")

    def command(self, name, **args):
        """Values may be a string or a list, sent tab-separated."""
        args = {k: [v] if isinstance(v, str) else list(v) for k, v in args.items()}
        if any("\n" in v or "\t" in v for values in args.values() for v in values):
            raise DaemonError("path contains a tab or newline")
        frame = name + "\n" + "".join("\t".join([k, *v]) + "\n" for k, v in args.items()) + "done\n"
        self._limit()
        try:
            self.sock.sendall(frame.encode("utf-8"))
        except socket.timeout as exc:
            raise DaemonTimeout("Dropbox took too long", submitted=True) from exc
        except OSError as exc:
            raise DaemonError("Dropbox stopped answering", submitted=True) from exc
        try:
            header = self._line()
            if header not in ("ok", "notok"):
                raise DaemonError("Dropbox sent an unexpected reply", submitted=True)
            lines = []
            for _ in range(20):
                line = self._line()
                if line == "done":
                    break
                lines.append(line)
            else:
                raise DaemonError("Dropbox sent an overlong reply", submitted=True)
        except OSError as exc:
            raise DaemonError("Dropbox stopped answering", submitted=True) from exc
        if header == "notok":
            raise DaemonRefused(clean("; ".join(lines)) or "Dropbox refused the command")
        fields = {}
        for line in lines:
            key, _, value = line.partition("\t")
            fields[key] = value.split("\t") if value else []
        return fields

    def first(self, name, key, **args):
        return (self.command(name, **args).get(key) or [""])[0]

    def status(self, path):
        return self.first("icon_overlay_file_status", "status", path=path)

    def context_options(self, paths):
        """The client's right-click menu for these paths, as raw options."""
        return self.command("icon_overlay_context_options", paths=paths).get("options", [])

    def context_action(self, paths, verb):
        self.command("icon_overlay_context_action", paths=paths, verb=verb)

    def shared_link(self, path):
        link = self.first("get_shared_link", "link", path=path)
        if not link.startswith("https://") or not link.isprintable() or len(link.encode("utf-8")) > 2048:
            raise DaemonError("Dropbox returned no link", submitted=True)
        return link


def parse_menu(options, depth=1):
    """Dropbox's menu as (label, verb or None, children) nodes, the way
    nautilus-dropbox reads it. Each option is `label~inner~verb`, all three
    percent-encoded. A label starting with `!` is a disabled item, left out.
    An inner part holding `~` is a submenu of `|`-separated options, and its
    verb is ignored."""
    nodes = []
    for option in options:
        parts = option.split("~", 2)
        if len(parts) != 3 or "~" in parts[2]:
            continue
        label, inner, verb = (unquote(p) for p in parts)
        label = label.strip()
        if label.startswith("!") or not label:
            continue
        if "~" in inner:
            if depth < MAX_DEPTH:
                children = parse_menu(inner.split("|"), depth + 1)
                if children:
                    nodes.append((label, None, children))
        else:
            nodes.append((label, verb, []))
    return nodes


def decoration(path, state):
    found = STATES.get(state)
    if found is None:
        return None
    badge, description, priority = found
    return {"path": path, "badge": badge, "description": description, "priority": priority}


def copy_to_clipboard(text):
    """True when the text reached the clipboard. wl-copy forks to keep serving
    the selection: its output must not be a pipe this process waits on, and it
    gets its own session so Strata stopping the provider does not take the
    clipboard with it."""
    binary = tool("wl-copy")
    if binary is None:
        return False
    try:
        return subprocess.run([binary, "--", text], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=2, cwd="/", start_new_session=True).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class Provider:
    def __init__(self, roots=dropbox_roots, connect=Daemon, copy=copy_to_clipboard, clock=time.monotonic):
        self.roots = roots
        self.connect = connect
        self.copy = copy
        self.clock = clock
        self.tickets = {}
        self.lock = threading.Lock()
        self.syncing = {}  # path -> when last checked, for paths still syncing

    def open(self):
        """A connection whose connect, reads and writes all finish within
        DEADLINE_SECONDS of this call."""
        deadline = time.monotonic() + DEADLINE_SECONDS
        daemon = self.connect()
        daemon.deadline = deadline
        return daemon

    def query(self, paths):
        roots = self.roots()
        wanted = [p for p in paths if inside(p, roots)]
        if not wanted:
            return []
        decorations = []
        deadline = self.clock() + DEADLINE_SECONDS
        try:
            with self.open() as daemon:
                for path in wanted:
                    if self.clock() > deadline:
                        break  # the rest wait for Strata's next refresh
                    state = daemon.status(path)
                    self.track(path, state == "syncing")
                    found = decoration(path, state)
                    if found:
                        decorations.append(found)
        except DaemonTimeout:
            return decorations  # the rest wait for Strata's next refresh
        except DaemonError:
            return []
        return decorations

    def track(self, path, syncing):
        with self.lock:
            if not syncing:
                self.syncing.pop(path, None)
                return
            self.syncing[path] = self.clock()
            while len(self.syncing) > MAX_WATCHED:
                del self.syncing[min(self.syncing, key=self.syncing.get)]

    def actions_from(self, nodes, paths, verbs, budget, used, reserved):
        """Strata actions for the parsed menu, recording each leaf's verb and
        notice in `verbs`. `budget` is a one-item list holding the nodes left
        under Strata's cap, and `used` the ids taken, both shared across the
        whole tree."""
        actions = []
        for label, verb, children in nodes:
            if budget[0] <= 0 or len(label.encode("utf-8")) > 128 or not label.isprintable():
                continue
            if children:
                budget[0] -= 1
                sub = self.actions_from(children, paths, verbs, budget, used, reserved)
                if not sub:
                    budget[0] += 1
                    continue
                n = len(used)
                while f"menu-{n}" in used or f"menu-{n}" in reserved:
                    n += 1
                used.add(f"menu-{n}")
                actions.append({"id": f"menu-{n}", "label": label, "children": sub})
                continue
            known = VERBS.get(verb, (None, "Sent to Dropbox."))
            action = verb.replace("_", "-")  # Strata ids allow letters, digits and dashes
            if (known is None or not ACTION_ID.fullmatch(action) or action in used
                    or (verb == "copy_link" and len(paths) > MAX_LINK_PATHS)):
                continue
            budget[0] -= 1
            used.add(action)
            verbs[action] = (verb, known[1])
            actions.append({"id": action, "label": known[0] or label})
        return actions

    def menu(self, paths, background):
        roots = self.roots()
        if not all(inside(p, roots) for p in paths):
            return []
        try:
            found = tuple(identity(p) for p in paths)
            with self.open() as daemon:
                nodes = parse_menu(daemon.context_options(paths))
        except (OSError, DaemonError):
            return []
        verbs = {}

        def leaf_ids(nodes):
            for _, verb, children in nodes:
                if children:
                    yield from leaf_ids(children)
                else:
                    yield verb.replace("_", "-")

        # Branch ids are made up, so reserve every action's id before choosing them.
        reserved = set(leaf_ids(nodes))
        actions = self.actions_from(nodes, paths, verbs, [MAX_NODES], set(), reserved)
        if not verbs:
            return []
        now = self.clock()
        with self.lock:
            self.tickets = {k: t for k, t in self.tickets.items() if t["expires"] >= now}
            while len(self.tickets) >= MAX_TICKETS:
                self.tickets.pop(next(iter(self.tickets)))
            token = secrets.token_hex(24)
            self.tickets[token] = {"paths": list(paths), "background": background, "roots": roots,
                                   "identities": found, "verbs": verbs, "expires": now + TICKET_SECONDS}

        def stamp(nodes):
            for node in nodes:
                if "children" in node:
                    stamp(node["children"])
                else:
                    node["context"] = token
            return nodes

        return stamp(actions)

    def still_eligible(self, ticket, paths, background):
        """The selection still names the same files, inside the same Dropbox.
        Dropbox's commands take paths, so a swap after this check still lands
        on the new file; this narrows that window to the command itself."""
        if ticket["paths"] != paths or ticket["background"] != background or self.roots() != ticket["roots"]:
            return False
        try:
            return tuple(identity(p) for p in paths) == ticket["identities"]
        except OSError:
            return False

    def activate(self, request):
        paths = request["paths"]
        total = len(paths)

        def result(status, message, accepted=0, feedback=None):
            outcome = {"status": status, "accepted": total if status == "accepted" else accepted, "total": total}
            reply = {"message": message, "outcome": outcome}
            return {**reply, "feedback": feedback} if feedback else reply

        action, context = request.get("action"), request.get("context")
        if not (isinstance(action, str) and isinstance(context, str) and len(context) <= 2048):
            return result("rejected", "This menu expired. Reopen the right-click menu.")
        with self.lock:
            ticket = self.tickets.pop(context, None)
        chosen = ticket and ticket["verbs"].get(action)
        if not chosen or ticket["expires"] < self.clock():
            return result("rejected", "This menu expired. Reopen the right-click menu.")
        if not self.still_eligible(ticket, paths, request["background"]):
            return result("rejected", "The selection changed. Reopen the right-click menu.")
        verb, notice = chosen
        if verb == "copy_link":
            return self.copy_links(paths, notice, result)
        try:
            with self.open() as daemon:
                daemon.context_action(paths, verb)
        except DaemonRefused as exc:
            return result("rejected", f"Dropbox refused: {exc}.")
        except DaemonError as exc:
            if exc.submitted:
                return result("unknown", f"Sent to Dropbox, but it stopped answering ({exc}). Check the Dropbox client.")
            return result("rejected", f"Dropbox could not do that: {exc}.")
        return result("accepted", notice, feedback="notice")

    def copy_links(self, paths, notice, result):
        """Fetch a link per path, then copy them here: the client may not reach
        a Wayland clipboard. A failure part way keeps the links already made."""
        links, failure = [], None
        deadline = self.clock() + DEADLINE_SECONDS
        try:
            with self.open() as daemon:
                for path in paths:
                    if self.clock() > deadline:
                        failure = DaemonError("Dropbox took too long")
                        break
                    links.append(daemon.shared_link(path))
        except DaemonError as exc:
            failure = exc
        uncertain = failure is not None and failure.submitted and not isinstance(failure, DaemonRefused)
        if not links:
            if uncertain:
                return result("unknown", f"Dropbox stopped answering ({failure}). A link may have been made.")
            return result("rejected", f"No Dropbox link: {failure}.")
        text = "\n".join(links)
        copied = self.copy(text)
        if copied:
            shown = ""
        elif len(text.encode("utf-8")) <= 12 * 1024:
            shown = f"\nCopy them by hand:\n{text}"  # no clipboard: show them in Strata's dialog
        else:
            shown = "\nSelect fewer items, or install wl-clipboard to copy them."
        if failure is None:
            if copied:
                return result("accepted", notice, feedback="notice")
            return result("accepted", f"Could not reach the clipboard.{shown}")
        summary = f"{'Copied' if copied else 'Made'} {len(links)} of {len(paths)} links, then: {clean(failure, 300)}."
        return result("unknown" if uncertain else "partial", summary + shown, accepted=len(links))

    def watch_once(self, emit):
        """Re-check the paths that were syncing, least recently checked first,
        and ask Strata to refresh the ones that settled, left the Dropbox
        folder, or can no longer be checked. Dropbox sends no change events."""
        roots = self.roots()
        with self.lock:
            changed = [p for p in self.syncing if not inside(p, roots)]
            for path in changed:
                del self.syncing[path]
            pending = sorted(self.syncing, key=self.syncing.get)[:200]
        if pending:
            try:
                with self.connect() as daemon:
                    for path in pending:
                        state = daemon.status(path)
                        self.track(path, state == "syncing")
                        if state != "syncing":
                            changed.append(path)
            except DaemonError:
                with self.lock:  # Dropbox went away: let Strata drop the badges
                    for path in pending:
                        if self.syncing.pop(path, None) is not None:
                            changed.append(path)
        for start in range(0, len(changed), 200):
            emit({"version": 1, "event": "invalidate", "paths": changed[start:start + 200]})

    def handle(self, request):
        rid = request.get("id")
        id_ok = type(rid) is int and rid >= 0
        reply = {"version": 1, "id": rid if id_ok else 0}
        paths = request.get("paths")
        try:
            valid = (type(request.get("version")) is int and request["version"] == 1 and id_ok
                     and type(request.get("background")) is bool and isinstance(paths, list)
                     and 1 <= len(paths) <= 200 and (len(paths) == 1 or not request["background"])
                     and all(isinstance(p, str) and p.startswith("/") and "\0" not in p
                             and len(p.encode("utf-8")) <= 16384 for p in paths))
        except UnicodeError:
            valid = False
        if not valid:
            return {**reply, **INVALID}
        method = request.get("method")
        if method == "query":
            return {**reply, "decorations": self.query(paths)}
        if method == "menu":
            return {**reply, "actions": self.menu(paths, request["background"])}
        if method == "activate":
            return {**reply, **self.activate(request)}
        return {**reply, "error": "unsupported-method", "message": "Unsupported provider method."}


def frames(message):
    """`message` as JSON frames within Strata's frame size. Too many badges
    are dropped from the end, for Strata to ask again; an invalidation is
    split; anything else becomes an error reply."""
    frame = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
    if len(frame.encode("utf-8")) <= MAX_FRAME:
        return [frame]
    if message.get("decorations"):
        return frames({**message, "decorations": message["decorations"][:len(message["decorations"]) // 2]})
    if message.get("event") == "invalidate" and len(message.get("paths", [])) > 1:
        half = len(message["paths"]) // 2
        return (frames({**message, "paths": message["paths"][:half]})
                + frames({**message, "paths": message["paths"][half:]}))
    if "id" in message:
        return [json.dumps({"version": 1, "id": message["id"], "error": "too-large",
                            "message": "The reply was too large."}, separators=(",", ":"))]
    return []


def main(provider=None, watch_seconds=WATCH_SECONDS):
    provider = provider or Provider()
    stdin, stdout = sys.stdin.buffer, sys.stdout
    out_lock = threading.Lock()

    def emit(message):
        for frame in frames(message):
            with out_lock:
                stdout.write(frame + "\n")
                stdout.flush()

    stop = threading.Event()

    def watch():
        while not stop.wait(watch_seconds):
            provider.watch_once(emit)

    threading.Thread(target=watch, daemon=True).start()
    try:
        while True:
            frame = stdin.readline(MAX_FRAME + 1)
            if not frame.endswith(b"\n"):
                return
            try:
                request = json.loads(frame)
                reply = provider.handle(request) if isinstance(request, dict) else None
            except (ValueError, TypeError):
                reply = None
            emit(reply or {"version": 1, "id": 0, **INVALID})
    finally:
        stop.set()


if __name__ == "__main__":
    main()
