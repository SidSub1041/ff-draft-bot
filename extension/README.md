# ff-draft-bot sidebar (Chrome extension)

Docks the ff-draft-bot panel into the right-hand side of a Sleeper draft page,
so the board, the by-position shortlist and the chat sit beside the draft
instead of in a separate window.

## It is a viewer, and only a viewer

This is worth being explicit about, because a browser extension on a draft page
*could* do far more than this one does:

* It does **not** read, parse or scrape the Sleeper page.
* It does **not** click anything, autopick, or submit a pick.
* It does **not** send your Sleeper data anywhere.

All it injects is a container with an `<iframe>` pointing at the panel server
running on your own machine. The bot learns about the draft the same way it
always has - by polling Sleeper's public API from Python. Pull the plug on the
extension and nothing about the bot's advice changes.

Permissions it asks for, and why:

| Permission | Why |
|---|---|
| `storage` | remembers the port, sidebar width and collapsed state |
| `http://127.0.0.1/*`, `http://localhost/*` | lets the service worker check whether your panel is running |
|  content script on `sleeper.com` (mounts only inside `/draft/` rooms) | to inject the sidebar container, nothing else |

It requests no access to any other site, and no `tabs`, `webRequest` or
`scripting` permission.

## Install

1. Start the panel:

   ```
   cd ~/ff-draft-bot
   ./scripts/ffbot-panel --no-window --connect <draft-url-or-id> --user <you>
   ```

   `--no-window` starts the server without opening the standalone window,
   since the sidebar is going to be your window.

2. Open `chrome://extensions`, turn on **Developer mode** (top right).
3. Click **Load unpacked** and choose this `extension/` folder.
4. Open your Sleeper draft. The sidebar appears on the right.

Default port is **8770**. If you run the panel elsewhere, set it in the
extension's *Details -> Extension options*.

## Using it

* Drag the left edge to resize (320-900px).
* **hide** collapses it to a pill on the right edge; click the pill to reopen.
* **reload** re-checks the server and reloads the panel.
* Width and collapsed state persist per browser profile.

Everything inside the sidebar is the normal panel: the draft board, best by
position, and the chat. Press `/` inside it to jump to the chat box.

## If the sidebar says the panel is not running

It genuinely is not reachable, or it is on a different port. Check:

```
curl http://127.0.0.1:8770/api/ping
```

You want `{"ok": true, "name": "ff-draft-bot"}`.

## If the sidebar stays blank

Chrome restricts pages on the public internet from loading resources on your
private network (Private Network Access), and an https page embedding
`http://127.0.0.1` is exactly that shape. The panel server opts in explicitly:
it answers the preflight with `Access-Control-Allow-Private-Network: true`, but
only for Sleeper's origins - any other site gets a flat 403.

If a future Chrome tightens this further and the frame will not load, nothing
is lost: run

```
./scripts/ffbot-panel --port 8770
```

and use the standalone window beside your browser instead. The panel and the
bot are identical either way; only the container differs.

## Honest testing note

This extension was written against a real Sleeper draft page, and the server
side of the contract (frame-ancestors, the PNA preflight, origin refusal, token
enforcement) is covered by the project's automated tests. The extension itself
has not been exercised in a packaged Chrome profile - unpacked extensions
cannot be loaded in the environment it was built in. Treat the first load as a
smoke test, and if something misbehaves the browser console on the Sleeper tab
will say why.
