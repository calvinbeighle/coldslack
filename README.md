# coldslack

cold-cli-style conditional DM sequences for Slack. A sequence of DMs with
per-step delays; a reply from the lead cancels every remaining step for that
lead.

Companion to [cold-cli](https://github.com/andersmyrmel/cold-cli) (email via
gws). Same shape: SQLite state, single `tick` engine, dry-run by default.

## Auth

Slack session credentials come from a 1Password item (default `Slack API`,
override with `COLDSLACK_OP_ITEM` / `COLDSLACK_OP_VAULT`) with fields
`xoxc_token` and `xoxd_cookie`. Both are required by Slack's web API. Sends
go out as the authenticated user in that workspace.

A small gated wire-in to cold-cli: sends are blocked unless
`COLDSLACK_ALLOW_SEND` carries the unlock token printed by a blocked tick.
Keep the unlock in a private `~/.coldslack/tick.sh` runner; activation of a
campaign is the human approval step.

## Usage

```bash
# sequence.json: [{"delay_days": 0, "body": "Hi {{name}}, ..."},
#                 {"delay_days": 2, "body": "Were you able to see this?"}]
# leads.csv:     slack_user_id,name

./coldslack.py campaign create --name demo --sequence sequence.json --leads leads.csv
./coldslack.py campaign preview demo
./coldslack.py campaign activate demo
./coldslack.py tick            # dry run
./coldslack.py tick --live     # actually send
./coldslack.py status
```

Run `tick` from cron or launchd. Guardrails: sends only Mon-Fri 09:00-17:00
local, 2-5 minute randomized gaps between sends in a tick, dry-run unless
`--live`.

## State

`~/.coldslack/data.db` (SQLite): campaigns, leads, sends. A lead is marked
`replied` when any message from them lands in the DM channel after our first
send; their pending sends flip to `cancelled`.
