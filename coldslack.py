#!/usr/bin/env python3
"""coldslack: cold-cli-style conditional DM sequences for Slack.

Sequences of Slack DMs with per-step delays. A reply from the lead cancels
all remaining steps for that lead. State in SQLite, single tick engine,
gated like cold-cli: tick sends unless --dry-run, but live sends require
the COLDSLACK_ALLOW_SEND unlock (carried by ~/.coldslack/tick.sh).

Auth: Slack session credentials from a 1Password item (default "Slack API",
override via COLDSLACK_OP_ITEM / COLDSLACK_OP_VAULT) with fields
xoxc_token + xoxd_cookie, both required by Slack's web API.
"""

import argparse
import os
import csv
import json
import random
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR = Path.home() / ".coldslack"
DB_PATH = STATE_DIR / "data.db"
OP_ITEM = os.environ.get("COLDSLACK_OP_ITEM", "Slack API")
OP_VAULT = os.environ.get("COLDSLACK_OP_VAULT")
SEND_WINDOW = (9, 17)  # local hours, inclusive start / exclusive end
SEND_DAYS = {0, 1, 2, 3, 4}  # Mon-Fri
GAP_SECONDS = (120, 300)  # Slack automation needs minutes between actions, not seconds
ALLOW_SEND_ENV = "COLDSLACK_ALLOW_SEND"
ALLOW_SEND_TOKEN = "I_UNDERSTAND_AND_USER_APPROVED_EXACT_SENDS"

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    steps_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    slack_user_id TEXT NOT NULL,
    name TEXT,
    channel_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    UNIQUE(campaign_id, slack_user_id)
);
CREATE TABLE IF NOT EXISTS sends (
    id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    step INTEGER NOT NULL,
    body TEXT NOT NULL,
    send_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    slack_ts TEXT,
    sent_at TEXT
);
"""


def db():
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def op_field(field):
    # Env override (COLDSLACK_XOXC_TOKEN / COLDSLACK_XOXD_COOKIE) lets a runner
    # supply cached credentials so a 30-minute tick never spends 1Password requests.
    env_val = os.environ.get("COLDSLACK_" + field.upper())
    if env_val:
        return env_val.strip()
    return subprocess.run(
        ["op", "item", "get", OP_ITEM]
        + (["--vault", OP_VAULT] if OP_VAULT else [])
        + ["--fields", field, "--reveal"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class Slack:
    def __init__(self):
        self.token = op_field("xoxc_token")
        self.cookie = op_field("xoxd_cookie")

    def call(self, method, **params):
        req = urllib.request.Request(
            f"https://slack.com/api/{method}",
            data=urllib.parse.urlencode(params).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Cookie": f"d={self.cookie}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read())
        if not out.get("ok"):
            raise RuntimeError(f"slack {method}: {out.get('error')}")
        return out

    def open_dm(self, user_id):
        return self.call("conversations.open", users=user_id)["channel"]["id"]

    def post(self, channel, text):
        return self.call("chat.postMessage", channel=channel, text=text)["ts"]

    def replies_after(self, channel, oldest_ts, me):
        msgs = self.call(
            "conversations.history", channel=channel, oldest=oldest_ts, limit=50
        ).get("messages", [])
        return [
            m for m in msgs if m.get("user") and m["user"] != me and not m.get("bot_id")
        ]

    def me(self):
        return self.call("auth.test")["user_id"]

    def lead_posts_in(self, channel, user_id, oldest_ts):
        """Everything the lead posted in this channel after oldest_ts, thread replies included
        (conversations.history returns top-level messages only). Verified 2026-10-20: the
        in:<#C...> form with a user token returns thread replies without an @mention."""
        after = datetime.fromtimestamp(float(oldest_ts) - 2 * 86400, tz=timezone.utc)
        try:
            out = self.call(
                "search.messages",
                query=f"from:<@{user_id}> in:<#{channel}> after:{after:%Y-%m-%d}",
                count=50,
            )
        except RuntimeError:
            return []
        return [
            m
            for m in out.get("messages", {}).get("matches") or []
            if float(m.get("ts", 0)) > float(oldest_ts)
        ]

    def thread_replies_from(self, channel, our_ts, user_id, oldest_ts):
        """The lead's replies inside the threads our posts live in (or started). Backstop for
        search index lag: conversations.replies reads the thread directly."""
        roots = set()
        for ts in our_ts:
            try:
                m = self.call("conversations.replies", channel=channel, ts=ts, limit=1)[
                    "messages"
                ][0]
                roots.add(m.get("thread_ts") or ts)
            except (RuntimeError, IndexError, KeyError):
                continue
        out = []
        for root in roots:
            try:
                msgs = self.call(
                    "conversations.replies",
                    channel=channel,
                    ts=root,
                    oldest=oldest_ts,
                    limit=200,
                ).get("messages", [])
            except RuntimeError:
                continue
            out += [
                m
                for m in msgs
                if m.get("user") == user_id and float(m.get("ts", 0)) > float(oldest_ts)
            ]
        return out

    def channel_mentions_from(self, user_id, oldest_ts, me):
        """Messages the lead posted in shared channels mentioning us after oldest_ts.

        Uses search.messages (user-session tokens support it), so it covers every
        channel the authenticated user can read without walking channel lists.
        """
        after = datetime.fromtimestamp(float(oldest_ts) - 2 * 86400, tz=timezone.utc)
        try:
            out = self.call(
                "search.messages",
                query=f"from:<@{user_id}> after:{after:%Y-%m-%d}",
                count=50,
            )
        except RuntimeError:
            return []  # search unavailable on this token; DM detection still applies
        matches = out.get("messages", {}).get("matches") or []
        return [
            m
            for m in matches
            if float(m.get("ts", 0)) > float(oldest_ts)
            and f"<@{me}>" in m.get("text", "")
        ]


def now_utc():
    return datetime.now(timezone.utc)


def in_send_window(dt_local):
    return (
        dt_local.weekday() in SEND_DAYS
        and SEND_WINDOW[0] <= dt_local.hour < SEND_WINDOW[1]
    )


def next_window_start(dt_local):
    d = dt_local
    while True:
        if d.weekday() in SEND_DAYS and d.hour < SEND_WINDOW[1]:
            start = d.replace(
                hour=SEND_WINDOW[0], minute=random.randint(0, 45), second=0
            )
            if start > dt_local:
                return start
            if in_send_window(dt_local):
                return dt_local
        d = (d + timedelta(days=1)).replace(hour=0, minute=0, second=0)


def render(body, lead):
    return body.replace("{{name}}", lead["name"] or "there")


def cmd_campaign_create(args):
    steps = json.loads(Path(args.sequence).read_text())
    assert isinstance(steps, list) and all("body" in s for s in steps), (
        "sequence: list of {delay_days, body}"
    )
    con = db()
    cur = con.execute(
        "INSERT INTO campaigns (name, steps_json, created_at) VALUES (?,?,?)",
        (args.name, json.dumps(steps), now_utc().isoformat()),
    )
    cid = cur.lastrowid
    n = 0
    with open(args.leads) as f:
        for row in csv.DictReader(f):
            lead = con.execute(
                "INSERT INTO leads (campaign_id, slack_user_id, name, channel_id) VALUES (?,?,?,?)",
                (
                    cid,
                    row["slack_user_id"].strip(),
                    (row.get("name") or "").strip(),
                    args.channel,
                ),
            )
            if args.at:
                base = datetime.fromisoformat(args.at)
            else:
                base = next_window_start(datetime.now().replace(tzinfo=None))
            if args.seed_ts:
                # Anchor: Calvin's own last post in the channel/DM, recorded as a sent
                # step 0 so reply detection runs from it BEFORE step 1 goes out.
                con.execute(
                    "INSERT INTO sends (campaign_id, lead_id, step, body, send_at, status, slack_ts, sent_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        cid,
                        lead.lastrowid,
                        0,
                        "[seed] existing post",
                        args.seed_ts
                        and datetime.fromtimestamp(float(args.seed_ts)).isoformat(),
                        "sent",
                        args.seed_ts,
                        datetime.fromtimestamp(
                            float(args.seed_ts), timezone.utc
                        ).isoformat(),
                    ),
                )
            for i, step in enumerate(steps):
                send_at = base + timedelta(days=int(step.get("delay_days", 0)))
                if i > 0:
                    send_at = send_at.replace(minute=random.randint(0, 59))
                con.execute(
                    "INSERT INTO sends (campaign_id, lead_id, step, body, send_at) VALUES (?,?,?,?,?)",
                    (cid, lead.lastrowid, i + 1, step["body"], send_at.isoformat()),
                )
            n += 1
    con.commit()
    print(
        f"campaign {args.name} (id={cid}) created: {n} lead(s), {len(steps)} step(s), status=draft"
    )


def cmd_campaign_set_status(args, status):
    con = db()
    if (
        con.execute(
            "UPDATE campaigns SET status=? WHERE name=?", (status, args.name)
        ).rowcount
        == 0
    ):
        sys.exit(f"no campaign named {args.name}")
    con.commit()
    print(f"campaign {args.name} is now {status}")


def cmd_preview(args):
    con = db()
    rows = con.execute(
        """SELECT c.name cname, c.status cstatus, l.slack_user_id, l.name lname, l.status lstatus,
                  s.step, s.send_at, s.status sstatus
           FROM sends s JOIN leads l ON l.id=s.lead_id JOIN campaigns c ON c.id=s.campaign_id
           WHERE c.name=? ORDER BY l.id, s.step""",
        (args.name,),
    ).fetchall()
    if not rows:
        sys.exit(f"no campaign named {args.name}")
    print(f"campaign {rows[0]['cname']} (status: {rows[0]['cstatus']})")
    for r in rows:
        print(
            f"  {r['send_at'][:16]}  step {r['step']}  {r['slack_user_id']:<14} {r['lname'] or '':<16} "
            f"lead={r['lstatus']} send={r['sstatus']}"
        )


def cmd_status(_args):
    con = db()
    for c in con.execute("SELECT * FROM campaigns ORDER BY id"):
        counts = dict(
            con.execute(
                "SELECT status, COUNT(*) FROM sends WHERE campaign_id=? GROUP BY status",
                (c["id"],),
            ).fetchall()
        )
        replied = con.execute(
            "SELECT COUNT(*) FROM leads WHERE campaign_id=? AND status='replied'",
            (c["id"],),
        ).fetchone()[0]
        print(
            f"{c['name']:<20} {c['status']:<8} sends={counts} replied_leads={replied}"
        )


def cmd_tick(args):
    live = not args.dry_run
    if live and os.environ.get(ALLOW_SEND_ENV) != ALLOW_SEND_TOKEN:
        print(
            "BLOCKED: coldslack sends are gated. Run via ~/.coldslack/tick.sh, or",
            file=sys.stderr,
        )
        print(f"export {ALLOW_SEND_ENV}={ALLOW_SEND_TOKEN}", file=sys.stderr)
        print(
            "only after the campaign's exact copy and lead list were approved.",
            file=sys.stderr,
        )
        sys.exit(64)
    con = db()
    slack = Slack()
    me = slack.me()

    # 1. Reply detection: any lead message after our first send cancels the rest.
    for lead in con.execute(
        """SELECT l.*, MIN(s.slack_ts) first_ts FROM leads l
           JOIN sends s ON s.lead_id=l.id AND s.status='sent'
           JOIN campaigns c ON c.id=l.campaign_id
           WHERE l.status='pending' AND c.status='active' AND l.channel_id IS NOT NULL
           GROUP BY l.id"""
    ).fetchall():
        replies = slack.replies_after(lead["channel_id"], lead["first_ts"], me)
        where = "dm"
        if not replies:
            replies = slack.lead_posts_in(
                lead["channel_id"], lead["slack_user_id"], lead["first_ts"]
            )
            where = "thread"
        if not replies:
            ours = [
                r["slack_ts"]
                for r in con.execute(
                    "SELECT slack_ts FROM sends WHERE lead_id=? AND status='sent' AND slack_ts IS NOT NULL",
                    (lead["id"],),
                )
            ]
            replies = slack.thread_replies_from(
                lead["channel_id"], ours, lead["slack_user_id"], lead["first_ts"]
            )
            where = "thread"
        if not replies:
            replies = slack.channel_mentions_from(
                lead["slack_user_id"], lead["first_ts"], me
            )
            where = "channel"
        if replies:
            print(
                f"reply ({where}) from {lead['slack_user_id']} ({lead['name']}): "
                f"{replies[-1].get('text', '')[:80]!r} -> cancelling remaining steps"
            )
            con.execute("UPDATE leads SET status='replied' WHERE id=?", (lead["id"],))
            con.execute(
                "UPDATE sends SET status='cancelled' WHERE lead_id=? AND status='pending'",
                (lead["id"],),
            )
    if live:
        con.commit()
    else:
        con.rollback()  # --dry-run reports cancellations without applying them

    # 2. Send due steps.
    local_now = datetime.now()
    if not args.ignore_window and not in_send_window(local_now):
        print(f"outside send window ({local_now:%a %H:%M}); nothing sent")
        return
    due = con.execute(
        """SELECT s.*, l.slack_user_id, l.name lname, l.channel_id FROM sends s
           JOIN leads l ON l.id=s.lead_id JOIN campaigns c ON c.id=s.campaign_id
           WHERE s.status='pending' AND l.status='pending' AND c.status='active'
             AND s.send_at <= ? ORDER BY s.send_at""",
        (local_now.isoformat(),),
    ).fetchall()
    if not due:
        print("tick complete: nothing to do")
        return
    for i, s in enumerate(due):
        text = render(s["body"], {"name": s["lname"]})
        if not live:
            print(
                f"[DRY RUN] would send step {s['step']} to {s['slack_user_id']} ({s['lname']}): {text[:80]!r}"
            )
            continue
        if i > 0:
            time.sleep(random.uniform(*GAP_SECONDS))
        verdict = engagement_gate(con, s)
        if verdict == 10:
            con.execute("UPDATE leads SET status='replied' WHERE id=?", (s["lead_id"],))
            con.execute(
                "UPDATE sends SET status='cancelled' WHERE lead_id=? AND status='pending'",
                (s["lead_id"],),
            )
            con.commit()
            print(
                f"gate: {s['lname']} engaged on another channel -> cancelling remaining steps"
            )
            continue
        if verdict != 0:
            print(
                f"gate: exit {verdict} for {s['lname']} (not CLEAR) -> not sending step {s['step']} this tick"
            )
            continue
        channel = s["channel_id"] or slack.open_dm(s["slack_user_id"])
        ts = slack.post(channel, text)
        con.execute("UPDATE leads SET channel_id=? WHERE id=?", (channel, s["lead_id"]))
        con.execute(
            "UPDATE sends SET status='sent', slack_ts=?, sent_at=? WHERE id=?",
            (ts, now_utc().isoformat(), s["id"]),
        )
        con.commit()
        print(f"sent step {s['step']} to {s['slack_user_id']} ({s['lname']}) ts={ts}")


GATE_CMD = os.environ.get(
    "COLDSLACK_GATE",
    "ssh -o BatchMode=yes -o ConnectTimeout=15 imac-gate check",
)


def engagement_gate(con, s):
    """Cross-channel check on the iMac (engagement-gate repo) right before a send.
    Exit 0 = CLEAR; 10 = engaged anywhere (cancel); anything else, including an ssh failure, = do not send."""
    sent = con.execute(
        "SELECT slack_ts FROM sends WHERE lead_id=? AND status='sent' AND slack_ts IS NOT NULL",
        (s["lead_id"],),
    ).fetchall()
    tss = sorted(float(r["slack_ts"]) for r in sent)
    if tss:
        since = "%.6f" % tss[-1]
    else:  # first step with no seed: anything since the campaign was created counts
        since = con.execute(
            "SELECT created_at FROM campaigns WHERE id=?", (s["campaign_id"],)
        ).fetchone()[0]
    cmd = GATE_CMD.split() + [
        "--sender",
        f"coldslack:{s['campaign_id']}:{s['id']}",
        "--person",
        s["lname"] or "",
        "--slack",
        s["slack_user_id"],
        "--since",
        since,
    ]
    if s["channel_id"]:
        cmd += sum(
            (["--slack-msg", f"{s['channel_id']}:{r['slack_ts']}"] for r in sent), []
        )
    if (
        cmd[0] == "ssh"
    ):  # ssh re-joins argv with spaces; quote so names with spaces survive
        n = GATE_CMD.split().index("check")
        cmd = cmd[:n] + [" ".join(shlex.quote(a) for a in cmd[n:])]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as e:
        print(f"gate: could not run ({e})")
        return 20
    print("gate: " + (r.stdout.strip() or r.stderr.strip())[:600])
    return r.returncode


def main():
    p = argparse.ArgumentParser(prog="coldslack", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    cc = sub.add_parser("campaign", help="manage campaigns")
    csub = cc.add_subparsers(dest="ccmd", required=True)
    create = csub.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument(
        "--sequence",
        required=True,
        help="JSON: [{delay_days, body}]; {{name}} substituted",
    )
    create.add_argument("--leads", required=True, help="CSV with slack_user_id,name")
    create.add_argument(
        "--channel",
        help="post into this channel id (C...) instead of the lead's DM; reply detection = any non-me message there",
    )
    create.add_argument(
        "--at",
        help="step-1 local time YYYY-MM-DDTHH:MM (default: next send-window opening)",
    )
    create.add_argument(
        "--seed-ts",
        help="Slack ts of our own last post; recorded as a sent step 0 so a reply before step 1 cancels the send",
    )
    create.set_defaults(fn=cmd_campaign_create)
    for st in ("activate", "pause"):
        sp = csub.add_parser(st)
        sp.add_argument("name")
        sp.set_defaults(
            fn=lambda a, st=("active" if st == "activate" else "paused"): (
                cmd_campaign_set_status(a, st)
            )
        )
    prev = csub.add_parser("preview")
    prev.add_argument("name")
    prev.set_defaults(fn=cmd_preview)

    tick = sub.add_parser("tick", help="poll replies, cancel sequences, send due steps")
    tick.add_argument(
        "--dry-run", action="store_true", help="preview actions without sending"
    )
    tick.add_argument(
        "--ignore-window",
        action="store_true",
        help="send even outside the Mon-Fri 09:00-17:00 local window (testing)",
    )
    tick.set_defaults(fn=cmd_tick)

    st = sub.add_parser("status")
    st.set_defaults(fn=cmd_status)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
