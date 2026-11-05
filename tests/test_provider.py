"""Tests for provider.py, standard library only. Run from the repository root:

    python3 -I -m unittest discover -s tests
"""
import importlib.util
import io
import json
import os
import pathlib
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import quote as urlquote

SPEC = importlib.util.spec_from_file_location(
    "provider", pathlib.Path(__file__).resolve().parent.parent / "provider.py")
provider = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provider)


class FakeDropbox:
    """A stand-in for the Dropbox client's command socket. `statuses` maps a
    path to its overlay state; `links` maps a path to its shared link."""

    def __init__(self, root):
        self.path = os.path.join(root, "command_socket")
        self.statuses, self.links, self.commands = {}, {}, []
        self.options = None  # None: offer FILE_OPTIONS for anything watched
        self.raw_reply = None  # text to send instead of a real reply
        self.hangup = set()  # commands to read, then close without answering
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.talk, args=(conn,), daemon=True).start()

    def talk(self, conn):
        with conn, conn.makefile("rw", encoding="utf-8", newline="\n") as f:
            while True:
                name = f.readline().rstrip("\n")
                if not name:
                    return
                args = {}
                for line in iter(f.readline, ""):
                    line = line.rstrip("\n")
                    if line == "done":
                        break
                    key, _, value = line.partition("\t")
                    args[key] = value
                self.commands.append((name, args))
                if name in self.hangup:
                    return
                if self.raw_reply is not None:
                    f.write(self.raw_reply)
                    f.flush()
                    return
                f.write(self.reply(name, args))
                f.flush()

    def reply(self, name, args):
        path = args.get("path", "")
        if name == "icon_overlay_context_options":
            options = FILE_OPTIONS if self.options is None else self.options
            return "ok\n" + ("options\t" + "\t".join(options) + "\n" if options else "") + "done\n"
        if name == "icon_overlay_context_action":
            return "ok\ndone\n"
        if name == "icon_overlay_file_status":
            return f"ok\nstatus\t{self.statuses.get(path, 'unwatched')}\ndone\n"
        if name == "get_shared_link":
            if path in self.links:
                return f"ok\nlink\t{self.links[path]}\ndone\n"
            return "notok\nThe path does not exist\ndone\n"
        return "notok\nUnknown command\ndone\n"

    def close(self):
        self.server.close()


# What the real client offers for a file, as label~description~verb.
FILE_OPTIONS = [
    "Right-click actions...~Right-click actions...~context_menu_settings",
    "Copy Dropbox link~Copy a Dropbox link for this file to your clipboard.~copy_link",
    "Version history~View the history of changes to this file~revisions",
    "View on Dropbox.com~View this file on the Dropbox website~browse",
    "View comments~View or add comments on this file~comment",
    "Do not sync~Do not sync~ignore_path",
]


def quote(text):
    """Percent-encode the way the Dropbox client does, ~ included (Python's
    quote leaves ~ alone, and it is the field separator here)."""
    return urlquote(text, safe="").replace("~", "%7E")


def request(method, paths, **extra):
    return {"version": 1, "id": 1, "method": method, "paths": paths, "background": False, **extra}


class ProviderTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.root = os.path.join(self.tmp, "Dropbox")
        os.mkdir(self.root)
        self.file = os.path.join(self.root, "notes.md")
        pathlib.Path(self.file).write_text("x")
        self.dropbox = FakeDropbox(self.tmp)
        self.addCleanup(self.dropbox.close)
        self.copied = []
        self.copy_ok = True

        def copy(text):
            self.copied.append(text)
            return self.copy_ok

        self.p = provider.Provider(roots=lambda: [self.root],
                                   connect=lambda: provider.Daemon(self.dropbox.path), copy=copy)

    def handle(self, method, paths, **extra):
        return self.p.handle(request(method, paths, **extra))

    # Badges

    def test_each_state_gets_its_badge(self):
        cases = {"up to date": "synced", "syncing": "syncing", "unsyncable": "error",
                 "ignored": "excluded", "selective sync": "excluded"}
        for state, badge in cases.items():
            self.dropbox.statuses[self.file] = state
            decorations = self.handle("query", [self.file])["decorations"]
            self.assertEqual([d["badge"] for d in decorations], [badge], state)

    def test_errors_outrank_other_badges(self):
        self.dropbox.statuses[self.file] = "unsyncable"
        self.assertEqual(self.handle("query", [self.file])["decorations"][0]["priority"], 2)

    def test_unwatched_and_outside_paths_get_no_badge(self):
        outside = os.path.join(self.tmp, "elsewhere.txt")
        self.assertEqual(self.handle("query", [self.file, outside])["decorations"], [])
        self.assertEqual([args["path"] for _, args in self.dropbox.commands], [self.file],
                         "paths outside Dropbox are never sent to the client")

    def test_only_outside_paths_never_connect(self):
        self.p.connect = lambda: self.fail("must not connect for paths outside Dropbox")
        self.assertEqual(self.handle("query", [os.path.join(self.tmp, "x")])["decorations"], [])

    def test_a_sibling_folder_is_not_inside(self):
        self.assertFalse(provider.inside(self.root + "2/file", [self.root]))
        self.assertTrue(provider.inside(self.root, [self.root]))

    def test_one_connection_serves_a_whole_batch(self):
        connections = []

        def connect():
            connections.append(1)
            return provider.Daemon(self.dropbox.path)

        self.p.connect = connect
        self.handle("query", [self.file, self.root])
        self.assertEqual(len(connections), 1)

    def test_no_badges_when_dropbox_is_not_running(self):
        self.p.connect = lambda: provider.Daemon(os.path.join(self.tmp, "missing"))
        self.assertEqual(self.handle("query", [self.file])["decorations"], [])
        self.assertEqual(self.handle("menu", [self.file])["actions"], [])

    # Copy link

    def test_menu_mirrors_the_clients_own_options(self):
        actions = self.handle("menu", [self.file])["actions"]
        self.assertEqual([(a["id"], a["label"]) for a in actions], [
            ("copy-link", "Copy link"), ("revisions", "Version history"), ("browse", "View on Dropbox.com"),
            ("comment", "View comments")])
        self.assertEqual(self.dropbox.commands[-1], ("icon_overlay_context_options", {"paths": self.file}))

    def test_menu_covers_a_whole_selection_inside_dropbox(self):
        self.handle("menu", [self.file, self.root])
        self.assertEqual(self.dropbox.commands[-1][1], {"paths": f"{self.file}\t{self.root}"})
        self.assertEqual(self.handle("menu", [self.file, os.path.join(self.tmp, "x")])["actions"], [],
                         "nothing when any item is outside Dropbox")

    def test_background_menu_offers_the_current_folder(self):
        actions = self.p.handle({**request("menu", [self.root]), "background": True})["actions"]
        self.assertTrue(actions)

    def test_unusable_options_are_skipped(self):
        self.dropbox.options = ["Bad id~d~has space", "~no label~verb", "Tab\x07bell~d~bell",
                                "x" * 129 + "~long~long_label", "Keep~d~keep_me", "Again~d~keep_me", "junk"]
        actions = self.handle("menu", [self.file])["actions"]
        self.assertEqual([(a["id"], a["label"]) for a in actions], [("keep-me", "Keep")])

    def test_no_menu_without_options(self):
        self.dropbox.options = []
        self.assertEqual(self.handle("menu", [self.file])["actions"], [])

    def test_other_actions_go_to_the_client(self):
        for action, notice in (("browse", "Opening on Dropbox.com."), ("revisions", "Opening version history on Dropbox.com.")):
            a = next(x for x in self.handle("menu", [self.file])["actions"] if x["id"] == action)
            reply = self.handle("activate", [self.file], action=action, context=a["context"])
            self.assertEqual((reply["message"], reply["feedback"]), (notice, "notice"))
            self.assertEqual(self.dropbox.commands[-1],
                             ("icon_overlay_context_action", {"paths": self.file, "verb": action.replace("-", "_")}))
        self.assertEqual(self.copied, [])

    def test_unknown_verbs_get_a_generic_notice(self):
        self.dropbox.options = ["Send~Send~send_copy"]
        a = self.handle("menu", [self.file])["actions"][0]
        self.assertEqual(self.handle("activate", [self.file], action="send-copy", context=a["context"])["message"],
                         "Sent to Dropbox.")

    def test_copy_link_for_several_items_copies_every_link(self):
        self.dropbox.links = {self.file: "https://www.dropbox.com/a", self.root: "https://www.dropbox.com/b"}
        a = next(x for x in self.handle("menu", [self.file, self.root])["actions"] if x["id"] == "copy-link")
        reply = self.handle("activate", [self.file, self.root], action="copy-link", context=a["context"])
        self.assertEqual(reply["outcome"], {"status": "accepted", "accepted": 2, "total": 2})
        self.assertEqual(self.copied, ["https://www.dropbox.com/a\nhttps://www.dropbox.com/b"])

    def test_copy_link_copies_and_shows_a_notice(self):
        self.dropbox.links[self.file] = "https://www.dropbox.com/scl/fi/abc/notes.md"
        action = self.handle("menu", [self.file])["actions"][0]
        reply = self.handle("activate", [self.file], action="copy-link", context=action["context"])
        self.assertEqual(reply["outcome"], {"status": "accepted", "accepted": 1, "total": 1})
        self.assertEqual((reply["message"], reply["feedback"]), ("Link copied.", "notice"))
        self.assertEqual(self.copied, ["https://www.dropbox.com/scl/fi/abc/notes.md"])

    def test_without_a_clipboard_the_link_is_shown_instead(self):
        self.copy_ok = False
        self.dropbox.links[self.file] = "https://www.dropbox.com/scl/fi/abc/notes.md"
        action = self.handle("menu", [self.file])["actions"][0]
        reply = self.handle("activate", [self.file], action="copy-link", context=action["context"])
        self.assertIn("https://www.dropbox.com/scl/fi/abc/notes.md", reply["message"])
        self.assertNotIn("feedback", reply, "the link shows in a dialog the user can copy from")

    def test_a_ticket_works_once(self):
        self.dropbox.links[self.file] = "https://www.dropbox.com/scl/fi/abc/notes.md"
        action = self.handle("menu", [self.file])["actions"][0]
        self.handle("activate", [self.file], action="copy-link", context=action["context"])
        again = self.handle("activate", [self.file], action="copy-link", context=action["context"])
        self.assertEqual(again["outcome"]["status"], "rejected")

    def test_stale_or_mismatched_activations_are_refused(self):
        now = [0.0]
        self.p.clock = lambda: now[0]

        def act(paths, action="copy-link"):
            a = self.handle("menu", [self.file])["actions"][0]
            return self.handle("activate", paths, action=action, context=a["context"])["outcome"]["status"]

        self.assertEqual(act([self.file], action="other"), "rejected")
        self.assertEqual(act([self.root]), "rejected", "different path")
        a = self.handle("menu", [self.file])["actions"][0]
        now[0] += provider.TICKET_SECONDS + 1
        self.assertEqual(self.handle("activate", [self.file], action="copy-link", context=a["context"])
                         ["outcome"]["status"], "rejected", "expired")
        reply = self.handle("activate", [self.file], action="copy-link", context=["list"])
        self.assertEqual(reply["outcome"]["status"], "rejected")

    def test_a_refused_link_is_reported(self):
        action = self.handle("menu", [self.file])["actions"][0]
        reply = self.handle("activate", [self.file], action="copy-link", context=action["context"])
        self.assertEqual(reply["outcome"]["status"], "rejected")
        self.assertIn("The path does not exist", reply["message"])
        self.assertEqual(self.copied, [])

    def test_ticket_cache_is_bounded(self):
        for _ in range(provider.MAX_TICKETS + 10):
            self.handle("menu", [self.file])
        self.assertLessEqual(len(self.p.tickets), provider.MAX_TICKETS)

    # The menu format, as nautilus-dropbox reads it

    def test_menu_options_are_percent_decoded(self):
        self.dropbox.options = ["View%20on%20Dropbox.com~d~browse", "Copy%20Dropbox%20link~d~copy%5Flink"]
        self.assertEqual([(a["id"], a["label"]) for a in self.handle("menu", [self.file])["actions"]],
                         [("browse", "View on Dropbox.com"), ("copy-link", "Copy link")])

    def test_disabled_and_hidden_options_are_left_out(self):
        self.dropbox.options = ["!Unavailable~d~browse", "Do not sync~d~ignore_path", "Settings~d~context_menu_settings",
                                "Keep~d~revisions"]
        self.assertEqual([a["id"] for a in self.handle("menu", [self.file])["actions"]], ["revisions"])

    def test_submenus_become_strata_branches(self):
        # The inner menu is itself percent-encoded, so its ~ and | do not split the outer option.
        inner = quote("Copy link~d~copy_link|View~d~browse")
        self.dropbox.options = [f"Share~{inner}~share"]
        (branch,) = self.handle("menu", [self.file])["actions"]
        self.assertEqual(branch["label"], "Share")
        self.assertNotIn("context", branch, "branches carry no context")
        self.assertEqual([(c["id"], c["label"]) for c in branch["children"]],
                         [("copy-link", "Copy link"), ("browse", "View")])
        self.assertTrue(all("context" in c for c in branch["children"]))

    def test_a_submenu_with_nothing_usable_is_dropped(self):
        self.dropbox.options = [f"Hidden~{quote('Do not sync~d~ignore_path')}~x", "Keep~d~browse"]
        self.assertEqual([a["id"] for a in self.handle("menu", [self.file])["actions"]], ["browse"])

    def test_menu_stays_inside_strata_s_node_cap(self):
        self.dropbox.options = [f"Item {i}~d~verb_{i}" for i in range(100)]
        self.assertEqual(len(self.handle("menu", [self.file])["actions"]), provider.MAX_NODES)
        nested = quote("|".join(f"Child {i}~d~child_{i}" for i in range(100)))
        self.dropbox.options = [f"More~{nested}~x"]
        (branch,) = self.handle("menu", [self.file])["actions"]
        self.assertEqual(1 + len(branch["children"]), provider.MAX_NODES, "the branch counts too")

    def test_copy_link_is_offered_only_for_a_small_selection(self):
        files = []
        for i in range(provider.MAX_LINK_PATHS + 1):
            path = os.path.join(self.root, f"f{i}")
            pathlib.Path(path).write_text("x")
            files.append(path)
        ids = [a["id"] for a in self.handle("menu", files)["actions"]]
        self.assertNotIn("copy-link", ids)
        self.assertIn("browse", ids)

    # Activation re-checks the selection

    def test_a_replaced_file_is_refused(self):
        a = self.menu_action("browse")
        os.unlink(self.file)
        pathlib.Path(self.file).write_text("new file, same name")
        self.assertIn("changed", self.activate_action(a)["message"])
        self.assertNotIn("icon_overlay_context_action", [c for c, _ in self.dropbox.commands])

    def test_a_moved_dropbox_folder_is_refused(self):
        a = self.menu_action("browse")
        self.p.roots = lambda: [os.path.join(self.tmp, "Elsewhere")]
        self.assertEqual(self.activate_action(a)["outcome"]["status"], "rejected")

    def test_a_background_mismatch_is_refused(self):
        a = self.menu_action("browse")
        reply = self.p.handle({**request("activate", [self.file], action="browse", context=a["context"]),
                               "background": True})
        self.assertEqual(reply["outcome"]["status"], "rejected")

    def test_malformed_action_fields_are_refused(self):
        a = self.menu_action("browse")
        reply = self.handle("activate", [self.file], action={}, context=a["context"])
        self.assertEqual(reply["outcome"]["status"], "rejected")

    # Outcomes say what really happened

    def test_a_refused_action_is_rejected(self):
        a = self.menu_action("browse")
        self.dropbox.raw_reply = "notok\nNo permission\ndone\n"
        reply = self.activate_action(a)
        self.assertEqual(reply["outcome"]["status"], "rejected")
        self.assertIn("No permission", reply["message"])

    def test_an_action_whose_reply_is_lost_is_unknown(self):
        a = self.menu_action("browse")
        self.dropbox.hangup = {"icon_overlay_context_action"}
        self.assertEqual(self.activate_action(a)["outcome"]["status"], "unknown")

    def test_a_link_that_fails_part_way_is_partial_and_keeps_the_first(self):
        self.dropbox.links = {self.file: "https://www.dropbox.com/a"}  # the root has none: refused
        a = next(x for x in self.handle("menu", [self.file, self.root])["actions"] if x["id"] == "copy-link")
        reply = self.handle("activate", [self.file, self.root], action="copy-link", context=a["context"])
        self.assertEqual(reply["outcome"], {"status": "partial", "accepted": 1, "total": 2})
        self.assertEqual(self.copied, ["https://www.dropbox.com/a"])

    def test_a_link_whose_reply_is_lost_is_unknown(self):
        self.dropbox.hangup = {"get_shared_link"}
        a = self.menu_action("copy-link")
        reply = self.activate_action(a)
        self.assertEqual(reply["outcome"], {"status": "unknown", "accepted": 0, "total": 1})

    def test_client_error_text_is_cleaned_and_bounded(self):
        a = self.menu_action("browse")
        self.dropbox.raw_reply = "notok\n" + "\x1b[31m" + "x" * 2000 + "\ndone\n"
        message = self.activate_action(a)["message"]
        self.assertNotIn("\x1b", message)
        self.assertLess(len(message), 700)

    def test_an_overlong_reply_line_is_refused(self):
        self.dropbox.raw_reply = "ok\nstatus\t" + "x" * (provider.MAX_LINE + 10) + "\ndone\n"
        with provider.Daemon(self.dropbox.path) as d:
            with self.assertRaisesRegex(provider.DaemonError, "overlong"):
                d.status(self.file)

    # Deadlines and the watcher

    def test_query_stops_at_its_deadline(self):
        ticks = iter(range(0, 1000, 2))
        self.p.clock = lambda: next(ticks)  # each status call costs two seconds
        files = [os.path.join(self.root, f"f{i}") for i in range(10)]
        for path in files:
            self.dropbox.statuses[path] = "up to date"
        decorations = self.handle("query", files)["decorations"]
        self.assertLess(len(decorations), len(files))

    def test_the_watcher_rotates_and_stays_bounded(self):
        for i in range(provider.MAX_WATCHED + 5):
            self.p.track(os.path.join(self.root, f"f{i}"), True)
        self.assertEqual(len(self.p.syncing), provider.MAX_WATCHED)
        first = {p for p, _ in sorted(self.p.syncing.items(), key=lambda kv: kv[1])[:200]}
        for path in self.p.syncing:
            self.dropbox.statuses[path] = "syncing"
        self.p.watch_once(lambda e: None)
        second = {p for p, _ in sorted(self.p.syncing.items(), key=lambda kv: kv[1])[:200]}
        self.assertFalse(first & second, "the next poll checks paths the last one did not")

    def test_paths_that_leave_dropbox_are_dropped_and_refreshed(self):
        events = []
        self.p.track(self.file, True)
        self.p.roots = lambda: [os.path.join(self.tmp, "Elsewhere")]
        self.p.watch_once(events.append)
        self.assertEqual(events, [{"version": 1, "event": "invalidate", "paths": [self.file]}])
        self.assertEqual(self.p.syncing, {})

    def test_a_deleted_file_is_refused(self):
        a = self.menu_action("browse")
        os.unlink(self.file)
        self.assertEqual(self.activate_action(a)["outcome"]["status"], "rejected")

    def test_dropbox_gone_at_activation_is_rejected_not_unknown(self):
        a = self.menu_action("browse")
        self.p.connect = lambda: provider.Daemon(os.path.join(self.tmp, "missing"))
        reply = self.activate_action(a)
        self.assertEqual(reply["outcome"]["status"], "rejected", "nothing was sent, so nothing happened")

    def test_copy_link_stops_at_its_deadline(self):
        files = []
        for i in range(3):
            path = os.path.join(self.root, f"f{i}")
            pathlib.Path(path).write_text("x")
            self.dropbox.links[path] = f"https://www.dropbox.com/{i}"
            files.append(path)
        a = next(x for x in self.handle("menu", files)["actions"] if x["id"] == "copy-link")
        ticks = iter(range(0, 1000, 3))
        self.p.clock = lambda: next(ticks)  # each step costs three seconds
        reply = self.handle("activate", files, action="copy-link", context=a["context"])
        self.assertEqual(reply["outcome"]["status"], "partial")
        self.assertIn("took too long", reply["message"])

    def test_branch_and_leaf_ids_never_collide(self):
        inner = quote("A~d~browse|Deeper~" + quote("B~d~revisions") + "~x")
        # "menu_3" comes first, so the branch that would be menu-3 must pick another id.
        self.dropbox.options = ["Menu3~d~menu_3", f"One~{inner}~x", f"Two~{quote('C~d~comment')}~x",
                                "Menu0~d~menu_0", "Menu5~d~menu_5"]
        ids = []

        def walk(nodes):
            for node in nodes:
                ids.append(node["id"])
                walk(node.get("children", []))

        walk(self.handle("menu", [self.file])["actions"])
        self.assertEqual(len(ids), len(set(ids)), ids)

    def test_a_link_with_control_characters_is_refused(self):
        self.dropbox.links[self.file] = "https://www.dropbox.com/\x1b[31m"
        a = self.menu_action("copy-link")
        # Dropbox answered, so a link may now exist: unknown, and nothing copied.
        self.assertEqual(self.activate_action(a)["outcome"]["status"], "unknown")
        self.assertEqual(self.copied, [])

    def test_links_too_long_to_show_are_not_shown(self):
        self.copy_ok = False
        files = []
        for i in range(provider.MAX_LINK_PATHS):
            path = os.path.join(self.root, f"f{i}")
            pathlib.Path(path).write_text("x")
            self.dropbox.links[path] = "https://www.dropbox.com/" + "x" * 2000
            files.append(path)
        a = next(x for x in self.handle("menu", files)["actions"] if x["id"] == "copy-link")
        reply = self.handle("activate", files, action="copy-link", context=a["context"])
        self.assertLess(len(reply["message"].encode("utf-8")), 16 * 1024)
        self.assertIn("wl-clipboard", reply["message"])

    def test_a_partial_copy_does_not_repeat_the_links(self):
        self.dropbox.links = {self.file: "https://www.dropbox.com/a"}
        a = next(x for x in self.handle("menu", [self.file, self.root])["actions"] if x["id"] == "copy-link")
        reply = self.handle("activate", [self.file, self.root], action="copy-link", context=a["context"])
        self.assertNotIn("https://", reply["message"], "the copied link is on the clipboard already")

    def test_an_unexpected_reply_header_is_unknown_not_refused(self):
        a = self.menu_action("browse")
        self.dropbox.raw_reply = "unexpected\ndone\n"
        self.assertEqual(self.activate_action(a)["outcome"]["status"], "unknown")

    def test_reads_stop_at_the_request_deadline(self):
        silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = os.path.join(self.tmp, "slow")
        silent.bind(path)
        silent.listen()
        self.addCleanup(silent.close)
        with provider.Daemon(path, timeout=30) as d:
            d.deadline = time.monotonic() + 0.2
            start = time.monotonic()
            with self.assertRaises(provider.DaemonError) as caught:
                d.status(self.file)
            self.assertLess(time.monotonic() - start, 2, "the deadline wins over the 30 s socket timeout")
            self.assertTrue(caught.exception.submitted)
            d.deadline = time.monotonic() - 1
            with self.assertRaisesRegex(provider.DaemonError, "took too long"):
                d.status(self.file)

    def test_frames_stay_inside_strata_s_size_limit(self):
        big = "/" + "é" * 8000
        decorations = [{"path": big, "badge": "synced", "description": "d", "priority": 0}] * 200
        (frame,) = provider.frames({"version": 1, "id": 7, "decorations": decorations})
        self.assertLessEqual(len(frame.encode("utf-8")), provider.MAX_FRAME)
        self.assertIn("é", frame, "UTF-8, not \\u escapes")
        self.assertTrue(json.loads(frame)["decorations"])
        events = provider.frames({"version": 1, "event": "invalidate", "paths": [big] * 200})
        self.assertGreater(len(events), 1)
        self.assertEqual(sum(len(json.loads(e)["paths"]) for e in events), 200)
        (error,) = provider.frames({"version": 1, "id": 8, "message": "x" * (2 * provider.MAX_FRAME)})
        self.assertEqual(json.loads(error)["error"], "too-large")
        self.assertEqual(provider.frames({"version": 1, "event": "other", "x": "y" * (2 * provider.MAX_FRAME)}), [])

    def test_a_trickled_reply_still_stops_at_the_deadline(self):
        slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = os.path.join(self.tmp, "trickle")
        slow.bind(path)
        slow.listen()
        self.addCleanup(slow.close)

        def trickle():
            conn, _ = slow.accept()
            with conn:
                conn.recv(4096)
                for byte in b"ok\nstatus\tup to date\ndone\n":
                    try:
                        conn.sendall(bytes([byte]))
                    except OSError:
                        return
                    time.sleep(0.05)

        threading.Thread(target=trickle, daemon=True).start()
        with provider.Daemon(path, timeout=30) as d:
            d.deadline = time.monotonic() + 0.3
            start = time.monotonic()
            with self.assertRaises(provider.DaemonTimeout):
                d.status(self.file)
            self.assertLess(time.monotonic() - start, 1, "many small receives still share one deadline")

    def test_badges_gathered_before_the_deadline_are_kept(self):
        other = os.path.join(self.root, "other.md")
        self.dropbox.statuses = {self.file: "up to date", other: "up to date"}
        calls = []
        real_status = provider.Daemon.status

        def status(daemon, path):
            calls.append(path)
            if len(calls) == 2:
                raise provider.DaemonTimeout("Dropbox took too long", submitted=True)
            return real_status(daemon, path)

        provider.Daemon.status = status
        self.addCleanup(setattr, provider.Daemon, "status", real_status)
        decorations = self.handle("query", [self.file, other])["decorations"]
        self.assertEqual([d["path"] for d in decorations], [self.file])

    def test_a_branch_never_takes_a_later_action_s_id(self):
        # The branch holds one action, so its natural id would be menu-1; the
        # client's own "menu_1" verb comes later and must survive.
        self.dropbox.options = [f"Group~{quote('Inside~d~browse')}~x", "Later~d~menu_1"]
        ids = []

        def walk(nodes):
            for node in nodes:
                ids.append(node["id"])
                walk(node.get("children", []))

        walk(self.handle("menu", [self.file])["actions"])
        self.assertIn("menu-1", ids, "the later action keeps its id")
        self.assertIn("browse", ids)
        self.assertEqual(len(ids), len(set(ids)))

    def menu_action(self, action_id, paths=None):
        actions = self.handle("menu", paths or [self.file])["actions"]
        return next(a for a in actions if a["id"] == action_id)

    def activate_action(self, action, paths=None):
        return self.handle("activate", paths or [self.file], action=action["id"], context=action["context"])

    # Watching syncs finish

    def test_a_finished_sync_asks_strata_to_refresh(self):
        events = []
        self.dropbox.statuses[self.file] = "syncing"
        self.handle("query", [self.file])
        self.p.watch_once(events.append)
        self.assertEqual(events, [], "still syncing: nothing to refresh")
        self.dropbox.statuses[self.file] = "up to date"
        self.p.watch_once(events.append)
        self.assertEqual(events, [{"version": 1, "event": "invalidate", "paths": [self.file]}])
        self.p.watch_once(events.append)
        self.assertEqual(len(events), 1, "a settled path is no longer watched")

    def test_dropbox_stopping_mid_sync_refreshes_everything_watched(self):
        events = []
        self.dropbox.statuses[self.file] = "syncing"
        self.handle("query", [self.file])
        self.p.connect = lambda: provider.Daemon(os.path.join(self.tmp, "missing"))
        self.p.watch_once(events.append)
        self.assertEqual(events[0]["paths"], [self.file])

    def test_nothing_watched_means_no_work(self):
        self.p.connect = lambda: self.fail("must not connect with nothing to watch")
        self.p.watch_once(lambda e: self.fail("must not emit"))

    # The Dropbox socket

    def test_daemon_refusal_and_bad_replies(self):
        with provider.Daemon(self.dropbox.path) as d:
            with self.assertRaisesRegex(provider.DaemonError, "Unknown command"):
                d.command("no_such_command")
            with self.assertRaisesRegex(provider.DaemonError, "tab or newline"):
                d.status("/x\ty")
        self.dropbox.raw_reply = "ok\n"  # then the connection closes
        with provider.Daemon(self.dropbox.path) as d:
            with self.assertRaisesRegex(provider.DaemonError, "closed"):
                d.status(self.file)
        self.dropbox.raw_reply = "ok\n" + "x\ty\n" * 25
        with provider.Daemon(self.dropbox.path) as d:
            with self.assertRaisesRegex(provider.DaemonError, "overlong"):
                d.status(self.file)
        self.dropbox.raw_reply = "ok\nlink\tnot-a-link\ndone\n"
        with provider.Daemon(self.dropbox.path) as d:
            with self.assertRaisesRegex(provider.DaemonError, "no link"):
                d.shared_link(self.file)

    def test_daemon_that_stops_answering(self):
        silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = os.path.join(self.tmp, "silent")
        silent.bind(path)
        silent.listen()
        self.addCleanup(silent.close)
        with provider.Daemon(path, timeout=0.2) as d:
            with self.assertRaisesRegex(provider.DaemonTimeout, "took too long"):
                d.status(self.file)

    def test_roots_come_from_info_json(self):
        home = os.path.join(self.tmp, "home")
        os.mkdir(home)
        info = os.path.join(home, "info.json")
        pathlib.Path(info).write_text(json.dumps(
            {"personal": {"path": "/h/Dropbox/"}, "business": {"path": "/h/Work Dropbox"}}))
        self.assertEqual(provider.dropbox_roots(home), ["/h/Dropbox", "/h/Work Dropbox"])
        pathlib.Path(info).write_text(json.dumps({"personal": {"path": "relative"}, "business": None}))
        self.assertEqual(provider.dropbox_roots(home), [])
        pathlib.Path(info).write_text("not json")
        self.assertEqual(provider.dropbox_roots(home), [])
        os.unlink(info)
        self.assertEqual(provider.dropbox_roots(home), [])

    # Clipboard

    def test_clipboard_uses_wl_copy_without_waiting_on_its_fork(self):
        log = os.path.join(self.tmp, "clip")
        fake = os.path.join(self.tmp, "wl-copy")
        # Like wl-copy: a child keeps running, holding stdout, after the parent exits.
        pgid = os.path.join(self.tmp, "pgid")
        pathlib.Path(fake).write_text(
            f'#!/bin/sh\nprintf "%s" "$2" >"{log}"\nps -o pgid= -p $$ >"{pgid}"\n(sleep 30) &\nexit 0\n')
        os.chmod(fake, 0o700)
        real = provider.tool
        provider.tool = lambda name: fake if name == "wl-copy" else None
        self.addCleanup(setattr, provider, "tool", real)
        done = threading.Event()
        result = {}

        def run():
            result["ok"] = provider.copy_to_clipboard("https://example.com/x")
            done.set()

        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(done.wait(5), "copying must not wait for wl-copy's background child")
        self.assertTrue(result["ok"])
        self.assertEqual(pathlib.Path(log).read_text(), "https://example.com/x")
        self.assertNotEqual(int(pathlib.Path(pgid).read_text()), os.getpgid(0),
                            "wl-copy runs in its own session, out of reach of Strata stopping the provider")
        provider.tool = lambda name: None
        self.assertFalse(provider.copy_to_clipboard("x"))
        provider.tool = lambda name: os.path.join(self.tmp, "missing")
        self.assertFalse(provider.copy_to_clipboard("x"))

    def test_every_badge_has_an_icon(self):
        icons = pathlib.Path(provider.__file__).parent / "icons"
        self.assertEqual({b for b, _, _ in provider.STATES.values()}, {p.stem for p in icons.glob("*.png")})

    def test_a_path_that_climbs_out_is_not_inside(self):
        self.assertFalse(provider.inside(self.root + "/../elsewhere", [self.root]))

    def test_tool_looks_only_in_system_directories(self):
        self.assertIn(provider.tool("sh"), ("/usr/bin/sh", "/bin/sh"))
        self.assertIsNone(provider.tool("definitely-not-a-real-tool"))

    # Protocol

    def test_invalid_requests(self):
        self.assertEqual(self.p.handle({**request("query", [self.file]), "id": -1})["error"], "invalid-request")
        self.assertEqual(self.p.handle({**request("query", [self.file]), "version": True})["error"], "invalid-request")
        self.assertEqual(self.handle("query", ["relative"])["error"], "invalid-request")
        self.assertEqual(self.handle("query", ["/\ud800"])["error"], "invalid-request")
        self.assertEqual(self.handle("rename", [self.file])["error"], "unsupported-method")

    def test_main_speaks_newline_delimited_json_and_streams_events(self):
        self.dropbox.statuses[self.file] = "syncing"
        r, w = os.pipe()
        out = io.StringIO()
        real_in, real_out = provider.sys.stdin, provider.sys.stdout
        provider.sys.stdin = io.TextIOWrapper(os.fdopen(r, "rb"))
        self.addCleanup(provider.sys.stdin.close)
        provider.sys.stdout = out
        self.addCleanup(setattr, provider.sys, "stdin", real_in)
        self.addCleanup(setattr, provider.sys, "stdout", real_out)
        done = threading.Event()
        threading.Thread(target=lambda: (provider.main(self.p, watch_seconds=0.05), done.set()),
                         daemon=True).start()
        def wait_for(text):
            for _ in range(200):
                if text in out.getvalue():
                    return
                time.sleep(0.01)

        os.write(w, (json.dumps(request("query", [self.file])) + "\nnot json\n[1]\n").encode())
        wait_for('"id":1')
        self.dropbox.statuses[self.file] = "up to date"
        wait_for('"event"')
        os.write(w, b"truncated")
        os.close(w)
        self.assertTrue(done.wait(5))
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        replies = [m for m in lines if "id" in m]
        self.assertEqual(replies[0]["decorations"][0]["badge"], "syncing")
        self.assertEqual([m.get("error") for m in replies[1:]], ["invalid-request", "invalid-request"])
        self.assertIn({"version": 1, "event": "invalidate", "paths": [self.file]}, lines)

    def test_runs_as_a_program(self):
        done = subprocess.run([provider.sys.executable, "-I", provider.__file__],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        self.assertEqual((done.returncode, done.stdout), (0, b""))


if __name__ == "__main__":
    unittest.main()
