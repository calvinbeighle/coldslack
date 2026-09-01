# coldslack

Conditional Slack DM sequences: a series of DMs with per-step delays where a
reply from the lead cancels every remaining step for that lead. Companion to
[cold-cli](https://github.com/andersmyrmel/cold-cli) (which does the same for
email); same operating shape: SQLite state, a single idempotent `tick`
engine, campaign activation as the human approval step, and an environment
gate on live sends.

Single-file Python 3, stdlib only. Tested on macOS with Python 3.14.

## How it works

1. You define a campaign: a JSON sequence of steps plus a CSV of leads.
2. `campaign create` pre-computes a send schedule per lead (step 1 at the
   next send-window opening, later steps offset by `delay_days`).
3. `tick` does everything, in this order, every time it runs:
   - polls each active lead's DM channel; any message from the lead after
     our first send marks the lead `replied` and flips their pending sends
     to `cancelled`
   - sends due steps (campaign `active`, lead `pending`, `send_at` in the
     past, inside the send window), with a randomized 2-5 minute gap between
     sends in the same tick
4. Run `tick` from cron or launchd. It is safe to run as often as you like;
   a send happens at most once (rows move `pending -> sent`).

## Auth

Credentials are Slack session tokens read from 1Password at runtime via the
`op` CLI. Item defaults to `Slack API`; override with:

- `COLDSLACK_OP_ITEM` -- 1Password item name
- `COLDSLACK_OP_VAULT` -- vault name (required when `op` runs as a service
  account, which always needs an explicit vault)

The item must have two fields, both required by Slack's web API:

- `xoxc_token` -- browser session token (sent as `Authorization: Bearer`)
- `xoxd_cookie` -- the `d` cookie value (sent as `Cookie: d=...`)

Sends appear as the authenticated user in that workspace. Note this is a
user session, not a bot token: it can only DM members of workspaces that
user belongs to.

## The send gate

`tick` refuses to send unless the environment carries the unlock:

```
COLDSLACK_ALLOW_SEND=I_UNDERSTAND_AND_USER_APPROVED_EXACT_SENDS
```

A blocked tick exits 64 and prints the unlock instructions. The intended
setup is a private runner script outside the repo (e.g.
`~/.coldslack/tick.sh`) that loads `op` credentials, sets the vault and the
unlock, and execs `coldslack.py tick`. The human approval step is campaign
activation: only activate a campaign whose exact copy and lead list the
owner has approved, then ticks run unattended.

`tick --dry-run` needs no unlock and prints what would happen.

## Commands

```bash
./coldslack.py campaign create --name NAME --sequence seq.json --leads leads.csv
./coldslack.py campaign preview NAME       # full schedule, one line per send
./coldslack.py campaign activate NAME      # draft -> active (the approval step)
./coldslack.py campaign pause NAME
./coldslack.py tick [--dry-run] [--ignore-window]
./coldslack.py status                      # one line per campaign with send counts
```

### Input formats

`seq.json` -- ordered list of steps. `delay_days` is relative to the lead's
step-1 date. `{{name}}` is replaced with the lead's name (falls back to
"there").

```json
[
  { "delay_days": 0, "body": "Hi {{name}}, ..." },
  { "delay_days": 2, "body": "Were you able to see this?" }
]
```

`leads.csv` -- header row required; `slack_user_id` mandatory, `name`
optional. Slack user IDs look like `U0XXXXXXXXX` (find them via
`users.list` or a profile's "Copy member ID").

```csv
slack_user_id,name
U0XXXXXXXXX,Jane
```

## Behavior details an agent should know

- **State** lives in `~/.coldslack/data.db` (SQLite; tables `campaigns`,
  `leads`, `sends`). Statuses: campaign `draft/active/paused`; lead
  `pending/replied`; send `pending/sent/cancelled`.
- **Send window**: Mon-Fri 09:00-17:00 local, hardcoded (`SEND_WINDOW`,
  `SEND_DAYS` at the top of the file). Outside it, tick sends nothing and
  says so. `--ignore-window` overrides (meant for testing).
- **Reply detection** only sees messages from OTHER users: a lead whose
  `slack_user_id` is the authenticated user (a self-DM test) can never be
  marked replied, because own messages are filtered out.
- **Bots are ignored** in reply detection (`bot_id` messages don't count).
- **Exit codes**: 64 = send gate blocked; nonzero otherwise on missing
  campaign, bad input files, `op` failures, or a Slack API error
  (`RuntimeError: slack <method>: <error>`). A common Slack error is
  `invalid_auth` = the session token expired; refresh the 1Password item.
- **Rate limits**: 2-5 min randomized gaps between sends inside one tick.
  Keep lead lists small; DM automation on a user session should stay at
  human scale (tens per day, not hundreds).
- **Idempotency**: re-running create with an existing campaign name fails on
  the UNIQUE constraint rather than duplicating. Adding leads to an existing
  campaign is not supported; create a new campaign.
- **No unsend**: pausing a campaign stops future sends; it cannot recall a
  sent DM.

## Testing (verified 2026-08-31)

End-to-end check performed on a live workspace:

1. `campaign create` + `preview` + `activate` with a one-lead self-DM
   campaign: schedule computed correctly.
2. `tick` without the unlock: blocked, exit 64.
3. `tick --dry-run`: prints planned sends, sends nothing.
4. Live tick via the unlocked runner (`--ignore-window`, step backdated):
   DM delivered, `slack_ts` recorded, send row `pending -> sent`.
5. Message confirmed present in the DM channel via `conversations.history`
   with the same `ts`.
6. Second tick: reply-detection pass ran clean, step 2 stayed `pending`
   (self-DM cannot trigger reply-cancel; see above).

To re-run the live test, use a campaign whose only lead is your own user ID
and pass `--ignore-window` outside business hours.
