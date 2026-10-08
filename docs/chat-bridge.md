# The chat bridge

Connect a private chat web app, backed by a Supabase `chat_messages` table, to this home's captain inbox.
It is optional and off until you configure it.

- **Messages in:** the bridge polls for `sender=captain`, `status=sent` rows and files each as a note with `bin/fm-inbox.sh note --request-id chat-<id>`.
  It then sets `status` to `received`, or `offline` when firstmate cannot receive, and stores `inbox_note_id`.
  The status column is the cursor, and the request id makes a retry after a crash safe.
- **Replies out:** when firstmate replies to a note, the bridge inserts a `sender=firstmate` row.
  Its `reply_to` is the captain message whose `inbox_note_id` matches, or null for a note that did not come from chat.
  Reply text is cut to 8000 characters, and an empty reply is skipped.

The bridge connects out to Supabase, so nothing on this machine is listening.
`bin/fm-inbox.sh` stays the single owner of the queue; the bridge only calls its `note`, `ready` and `receipts` subcommands.
The table and the web app are not part of this repo.

## Set it up

1. Create `config/chat-bridge.env` in the home with `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`; see [configuration](configuration.md#chat-bridge-configchat-bridgeenv).
   The service-role key bypasses row security, so keep the file private; `config/` is gitignored.
2. `python3 bin/fm-chat-bridge.py init` checks the required keys and names any that are missing.
3. `python3 bin/fm-chat-bridge.py run` loops forever; `once` runs a single iteration.

The reply cursor lives in `state/chat-bridge/reply_cursor`.
On the first run it adopts firstmate's current position and sends no old replies.

## Start it at login (macOS launchd)

Not installed automatically.
From the firstmate repo root, with `FM_HOME` set to the home that holds the config:

```sh
mkdir -p "$FM_HOME/state/chat-bridge"
sed -e "s|__REPO__|$PWD|g" -e "s|__HOME__|$FM_HOME|g" docs/examples/chat-bridge.plist \
  > ~/Library/LaunchAgents/com.firstmate.chat-bridge.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.firstmate.chat-bridge.plist
```

Remove it with `launchctl bootout gui/$(id -u)/com.firstmate.chat-bridge` and delete the plist.
Logs go to `state/chat-bridge/bridge.log`.

## Verify

`tests/fm-chat-bridge.test.sh` runs the bridge against a fake PostgREST server and a fake inbox, with no network.
