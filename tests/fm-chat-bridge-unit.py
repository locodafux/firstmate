import importlib.util
import json
import socketserver
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

spec = importlib.util.spec_from_file_location(
    "bridge", Path(__file__).resolve().parent.parent / "bin" / "fm-chat-bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

# Fake FM_INBOX: logs every call, serves canned JSON from files next to it.
# `note` reports "replay" for a request id it has already seen, like the real one.
FAKE_INBOX = r"""#!/bin/bash
d="$(dirname "$0")"
echo "$FM_HOME|$*" >> "$d/calls.log"
case "$1" in
  note)
    rid="$3"
    if grep -qx "$rid" "$d/seen" 2>/dev/null; then o=replay; else echo "$rid" >> "$d/seen"; o=created; fi
    echo "{\"outcome\":\"$o\",\"id\":\"n-$rid\"}" ;;
  ready) cat "$d/ready.json" ;;
  receipts) cat "$d/receipts.json" ;;
  *) exit 1 ;;
esac
"""


class FakeSupabase(BaseHTTPRequestHandler):
    rows = []      # chat_messages rows (dicts)
    calls = []     # (method, path, headers dict, json body)

    def log_message(self, *a):
        pass

    def reply(self, code, obj=None):
        out = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        self.end_headers()
        self.wfile.write(out)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def match(self, q):
        # eq filters only, enough for the bridge's queries
        return [r for r in self.rows
                if all(r.get(k) == v[0][3:] for k, v in q.items()
                       if v[0].startswith("eq.") and k != "limit")]

    def do_GET(self):
        u = urlparse(self.path)
        self.calls.append(("GET", self.path, dict(self.headers), None))
        out = sorted(self.match(parse_qs(u.query)), key=lambda r: r["created_at"])
        self.reply(200, out)

    def do_PATCH(self):
        u, b = urlparse(self.path), self.body()
        self.calls.append(("PATCH", self.path, dict(self.headers), b))
        for r in self.match(parse_qs(u.query)):
            r.update(b)
        self.reply(204)

    def do_POST(self):
        b = self.body()
        self.calls.append(("POST", self.path, dict(self.headers), b))
        self.rows.append({"id": f"r{len(self.rows)}", "created_at": f"9{len(self.rows)}",
                          "status": "sent", "inbox_note_id": None, **b})
        self.reply(201)


class QuietServer(HTTPServer):
    def server_bind(self):  # skip HTTPServer's slow getfqdn() lookup
        socketserver.TCPServer.server_bind(self)
        self.server_port = self.socket.getsockname()[1]


class BridgeTest(unittest.TestCase):
    def setUp(self):
        FakeSupabase.rows, FakeSupabase.calls = [], []
        self.srv = QuietServer(("127.0.0.1", 0), FakeSupabase)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.tmp = Path(tempfile.mkdtemp())
        inbox = self.tmp / "fm-inbox.sh"
        inbox.write_text(FAKE_INBOX)
        inbox.chmod(inbox.stat().st_mode | stat.S_IXUSR)
        self.set_ready(True)
        self.set_receipts([], "")
        self.cfg = {
            "url": f"http://127.0.0.1:{self.srv.server_port}", "key": "svc-key",
            "home": "/fake/home", "inbox": str(inbox), "poll": 1, "state": self.tmp / "state",
        }

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def set_ready(self, ok):
        (self.tmp / "ready.json").write_text(json.dumps({"can_receive": ok}))

    def set_receipts(self, replies, cursor):
        (self.tmp / "receipts.json").write_text(json.dumps({
            "pending": [], "handled": [], "replies": replies, "reply_cursor": cursor}))

    def calls(self):
        p = self.tmp / "calls.log"
        return p.read_text().splitlines() if p.exists() else []

    def notes(self):
        return [c for c in self.calls() if "|note " in c]

    def add_msg(self, mid, body, sender="captain", status="sent", note_id=None):
        FakeSupabase.rows.append({"id": mid, "created_at": f"1{len(FakeSupabase.rows)}",
                                  "sender": sender, "body": body, "status": status,
                                  "inbox_note_id": note_id})

    def row(self, mid):
        return next(r for r in FakeSupabase.rows if r["id"] == mid)

    def test_sent_task_is_noted_then_marked_received(self):
        self.add_msg("m1", "fix the login bug")
        bridge.once(self.cfg)
        self.assertEqual(self.notes(),
                         ["/fake/home|note --request-id chat-m1 --json -- fix the login bug"])
        self.assertEqual(self.row("m1")["status"], "received")
        self.assertEqual(self.row("m1")["inbox_note_id"], "n-chat-m1")
        get = FakeSupabase.calls[0]
        self.assertIn("sender=eq.captain&status=eq.sent&order=created_at.asc", get[1])
        self.assertEqual(get[2]["Authorization"], "Bearer svc-key")
        self.assertEqual(get[2]["Apikey"], "svc-key")  # urllib title-cases header names

    def test_tasks_filed_in_order_and_others_ignored(self):
        self.add_msg("m1", "first")
        self.add_msg("f1", "a reply", sender="firstmate")
        self.add_msg("m0", "already done", status="received")
        self.add_msg("m2", "second")
        bridge.once(self.cfg)
        self.assertEqual([c.split()[2] for c in self.notes()], ["chat-m1", "chat-m2"])

    def test_offline_marks_offline_and_keeps_note_id(self):
        self.set_ready(False)
        self.add_msg("m1", "task one")
        bridge.once(self.cfg)
        self.assertEqual(self.row("m1")["status"], "offline")
        self.assertEqual(self.row("m1")["inbox_note_id"], "n-chat-m1")

    def test_replay_after_failed_status_update_notes_once(self):
        self.add_msg("m1", "task one")
        real = bridge.rest
        bridge.rest = lambda cfg, m, *a: (_ for _ in ()).throw(RuntimeError("boom")) if m == "PATCH" else real(cfg, m, *a)
        try:
            bridge.once(self.cfg)  # note filed, PATCH fails, logs, does not raise
        finally:
            bridge.rest = real
        self.assertEqual(self.row("m1")["status"], "sent")
        bridge.once(self.cfg)  # same request id: inbox reports replay, status finally moves
        bridge.once(self.cfg)  # no longer `sent`: not touched again
        self.assertEqual({c.split()[2] for c in self.notes()}, {"chat-m1"})
        self.assertEqual(len(self.notes()), 2)
        self.assertEqual(len((self.tmp / "seen").read_text().split()), 1)
        self.assertEqual(self.row("m1")["status"], "received")

    def test_reply_links_to_captain_message(self):
        self.add_msg("m1", "fix the login bug", status="received", note_id="n1")
        bridge.once(self.cfg)  # adopt cursor
        self.set_receipts([{"id": "n1", "body": "PR ready: https://github.com/a/b/pull/7",
                            "cursor": "000000000005"}], "000000000005")
        bridge.once(self.cfg)
        posts = [c for c in FakeSupabase.calls if c[0] == "POST"]
        self.assertEqual([p[3] for p in posts], [{
            "sender": "firstmate", "body": "PR ready: https://github.com/a/b/pull/7",
            "reply_to": "m1"}])
        self.assertTrue(any("receipts --after 000000000000" in c for c in self.calls()))
        self.assertEqual(bridge.read_state(self.cfg, "reply_cursor"), "000000000005")

    def test_unlinked_reply_has_null_reply_to(self):
        bridge.once(self.cfg)
        self.set_receipts([{"id": "other", "body": "hello", "cursor": "000000000002"}],
                          "000000000002")
        bridge.once(self.cfg)
        posts = [c[3] for c in FakeSupabase.calls if c[0] == "POST"]
        self.assertEqual(posts, [{"sender": "firstmate", "body": "hello", "reply_to": None}])

    def test_reply_is_trimmed_and_empty_reply_is_skipped(self):
        bridge.once(self.cfg)
        self.set_receipts([{"id": "a", "body": "x" * 9000, "cursor": "000000000002"},
                           {"id": "b", "body": "  ", "cursor": "000000000003"}], "000000000003")
        bridge.once(self.cfg)
        posts = [c[3] for c in FakeSupabase.calls if c[0] == "POST"]
        self.assertEqual([len(p["body"]) for p in posts], [8000])
        self.assertEqual(bridge.read_state(self.cfg, "reply_cursor"), "000000000003")

    def test_first_run_replays_nothing(self):
        self.set_receipts([{"id": "n0", "body": "old reply", "cursor": "000000000003"}],
                          "000000000003")
        bridge.once(self.cfg)
        self.assertEqual([c for c in FakeSupabase.calls if c[0] == "POST"], [])
        self.assertEqual(bridge.read_state(self.cfg, "reply_cursor"), "000000000003")

    def test_error_does_not_crash_or_change_status(self):
        self.add_msg("m1", "task one")
        self.cfg["inbox"] = "/nonexistent/fm-inbox.sh"
        bridge.once(self.cfg)  # logs, does not raise
        self.assertEqual(self.row("m1")["status"], "sent")

    def test_init_reports_missing_keys_and_never_writes(self):
        (self.tmp / "config").mkdir()
        env = self.tmp / "config" / "chat-bridge.env"
        environ = {"FM_HOME": str(self.tmp)}
        env.write_text("SUPABASE_ACCESS_TOKEN=keep\nSUPABASE_URL=https://x.supabase.co\n")
        self.assertFalse(bridge.init(environ))
        self.assertEqual(env.read_text(),
                         "SUPABASE_ACCESS_TOKEN=keep\nSUPABASE_URL=https://x.supabase.co\n")
        env.write_text("SUPABASE_URL=u\nSUPABASE_SERVICE_ROLE_KEY=k\n")
        self.assertTrue(bridge.init(environ))
        with self.assertRaises(SystemExit):
            bridge.load_config({"FM_HOME": str(self.tmp / "nope")})
        cfg = bridge.load_config(environ)
        self.assertEqual((cfg["url"], cfg["key"], cfg["poll"]), ("u", "k", 10))
        self.assertEqual(cfg["state"], self.tmp / "state" / "chat-bridge")
        self.assertEqual(cfg["inbox"], str(Path(bridge.__file__).resolve().parent / "fm-inbox.sh"))


if __name__ == "__main__":
    unittest.main()
