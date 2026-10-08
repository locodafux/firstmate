#!/usr/bin/env python3
"""fm-chat-bridge.py - bridge the Supabase chat_messages table to this home's captain inbox (stdlib only).

  init   check config/chat-bridge.env has the required keys and print what is missing
  run    loop forever: captain messages in, firstmate replies out
  once   one iteration (tests, manual checks)

Captain messages (sender=captain, status=sent) become `fm-inbox.sh note --request-id chat-<id>`
notes, then move to status received (or offline when firstmate cannot receive); the status column
is the cursor and the request id makes a retry safe. Firstmate's inbox replies are posted back as
sender=firstmate rows. The reply cursor lives in state/chat-bridge/reply_cursor and a first run
adopts the current position without sending old replies.

Settings come from the home's gitignored config/chat-bridge.env (KEY=value lines) or the environment:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   required; the service-role key bypasses row security
  POLL_SECS                                 seconds between iterations, default 10
FM_HOME selects the home (default: the repo root). docs/chat-bridge.md is the setup guide.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent
BODY_LIMIT = 8000  # chat_messages.body check constraint
NO_CURSOR = "000000000000"  # receipts cursors are zero-padded, so this sorts first
REQUIRED = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")


def log(msg):
    print(f"fm-chat-bridge: {msg}", file=sys.stderr, flush=True)


def home_dir(environ=os.environ):
    return Path(environ.get("FM_HOME") or CODE_ROOT)


def env_path(environ=os.environ):
    return home_dir(environ) / "config" / "chat-bridge.env"


def read_env(env_file, environ=os.environ):
    vals = {}
    if Path(env_file).is_file():
        for line in Path(env_file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip("\"'")
    for k in (*REQUIRED, "POLL_SECS"):
        if environ.get(k):
            vals[k] = environ[k]
    return vals


def load_config(environ=os.environ):
    env_file = env_path(environ)
    vals = read_env(env_file, environ)
    missing = [k for k in REQUIRED if not vals.get(k)]
    if missing:
        raise SystemExit(f"missing in {env_file}: {', '.join(missing)}")
    home = home_dir(environ)
    return {
        "url": vals["SUPABASE_URL"].rstrip("/"),
        "key": vals["SUPABASE_SERVICE_ROLE_KEY"],
        "home": str(home),
        "inbox": str(CODE_ROOT / "bin" / "fm-inbox.sh"),
        "poll": int(vals.get("POLL_SECS", "10")),
        "state": home / "state" / "chat-bridge",
    }


# ---- state (one small text file per cursor) --------------------------------

def read_state(cfg, name):
    try:
        return (cfg["state"] / name).read_text().strip() or None
    except FileNotFoundError:
        return None


def write_state(cfg, name, value):
    cfg["state"].mkdir(parents=True, exist_ok=True)
    tmp = cfg["state"] / f".{name}.tmp"
    tmp.write_text(value + "\n")
    tmp.replace(cfg["state"] / name)


# ---- supabase (PostgREST) --------------------------------------------------

def rest(cfg, method, path, body=None):
    """One PostgREST call with the service-role key (bypasses RLS); returns parsed JSON or None."""
    req = urllib.request.Request(
        f"{cfg['url']}/rest/v1/{path}", method=method,
        data=None if body is None else json.dumps(body).encode())
    req.add_header("apikey", cfg["key"])
    req.add_header("Authorization", f"Bearer {cfg['key']}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Prefer", "return=minimal")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path.split('?')[0]} -> {e.code}: {e.read().decode(errors='replace')}")
    return json.loads(raw) if raw else None


# ---- firstmate inbox -------------------------------------------------------

def fm(cfg, *args):
    env = dict(os.environ, FM_HOME=cfg["home"])
    p = subprocess.run([cfg["inbox"], *args], env=env, capture_output=True,
                       text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"fm-inbox {args[0]} exit {p.returncode}: {p.stderr.strip()}")
    return json.loads(p.stdout)


# ---- one iteration ---------------------------------------------------------

def tasks_in(cfg):
    """Status is the cursor: a captain message leaves `sent` only after it is filed as a note."""
    sent = rest(cfg, "GET", "chat_messages?sender=eq.captain&status=eq.sent&order=created_at.asc")
    for msg in sent or []:
        note = fm(cfg, "note", "--request-id", f"chat-{msg['id']}", "--json", "--", msg["body"])
        status = "offline" if fm(cfg, "ready").get("can_receive") is False else "received"
        rest(cfg, "PATCH", f"chat_messages?id=eq.{msg['id']}&status=eq.sent",
             {"status": status, "inbox_note_id": note["id"]})


def pings_out(cfg):
    cursor = read_state(cfg, "reply_cursor")
    if cursor is None:  # first run: adopt, send nothing
        r = fm(cfg, "receipts", "--all-replies")
        write_state(cfg, "reply_cursor", r.get("reply_cursor") or NO_CURSOR)
        return
    r = fm(cfg, "receipts", "--after", cursor)
    for rep in r.get("replies", []):
        body = rep["body"][:BODY_LIMIT]
        if body.strip():  # an empty body would fail the table check and wedge the cursor
            note_id = urllib.parse.quote(str(rep["id"]), safe="")
            hit = rest(cfg, "GET", f"chat_messages?inbox_note_id=eq.{note_id}&select=id&limit=1")
            rest(cfg, "POST", "chat_messages", {
                "sender": "firstmate", "body": body,
                "reply_to": hit[0]["id"] if hit else None})
        write_state(cfg, "reply_cursor", rep["cursor"])


def once(cfg):
    for step in (tasks_in, pings_out):
        try:
            step(cfg)
        except Exception as e:  # never crash the loop; status/cursor make the retry safe
            log(f"{step.__name__}: {e}")


def init(environ=os.environ):
    env_file = env_path(environ)
    missing = [k for k in REQUIRED if not read_env(env_file, environ).get(k)]
    print(f"missing in {env_file}: {', '.join(missing)}" if missing
          else f"{env_file}: all required keys present")
    return not missing


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "init":
        raise SystemExit(0 if init() else 1)
    elif cmd in ("run", "once"):
        cfg = load_config()
        while True:
            once(cfg)
            if cmd == "once":
                break
            time.sleep(cfg["poll"])
    else:
        raise SystemExit("usage: fm-chat-bridge.py init|run|once")


if __name__ == "__main__":
    try:
        main(sys.argv)
    except KeyboardInterrupt:
        pass
