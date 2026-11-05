# How it works

Detail behind the [README](../README.md): where the badges come from, how they stay current, and
where the menu comes from.

## The provider protocol

Strata runs each registered provider as a child process and talks to it in newline-delimited
JSON over stdin and stdout
([Strata's provider docs](https://github.com/lgse/strata/blob/v1.1.1/docs/file-providers.md)).

| Request | Dropbox answers |
| --- | --- |
| `query` | A badge for each path inside your Dropbox |
| `menu` | The Dropbox client's own actions for the selection |
| `activate` | Runs the chosen action and replies with a short notice |

Strata heads a provider's actions with its name, so the provider drops "Dropbox" from labels:
the client's "Copy Dropbox link" shows as **Copy link**. `provider.json` registers the process and the four badge images beside it.
Strata runs its `command` as literal argv with no shell and no `PATH` lookup, which is why the
installer writes an absolute interpreter path.

## Talking to Dropbox

The Dropbox client listens on a Unix socket, `~/.dropbox/command_socket`, the one `dropbox-cli`
and Nautilus's Dropbox extension use. A command is its name, `key<TAB>value` lines, and `done`;
the reply is `ok` or `notok`, fields, and `done`:

```
icon_overlay_file_status        ok
path<TAB>/home/me/Dropbox/a.md  status<TAB>up to date
done                            done
```

A whole `query` batch shares one connection. A missing socket, a refusal or a timeout means
no badges and no menu, never a guessed state.

Your Dropbox folders come from `~/.dropbox/info.json`, personal and business. Paths outside them
are answered without asking Dropbox at all.

## Badges

| Dropbox says | Badge | Priority |
| --- | --- | --- |
| `up to date` | synced | normal |
| `syncing` | syncing | warning |
| `unsyncable` | error | error |
| `ignored`, or `selective sync` on clients before v94 | excluded | normal |
| `unwatched`, or anything else | none | |

Strata shows the highest-priority badge when several providers decorate the same file, so a
sync error outranks other providers' normal and warning badges. Between two error badges,
Strata picks by provider id.

## Keeping badges current

Dropbox sends no change events over its command socket. Strata re-asks for visible files on
its own every few seconds, but that is slow to show a sync finishing. So the provider remembers
the paths it last reported as syncing, and every three seconds re-checks up to 200 of them,
least recently checked first, so a long list is covered in turns. It sends Strata an
`invalidate` event for paths that settled, left your Dropbox folder, or could not be checked
because Dropbox stopped answering; Strata then asks for those badges again. It watches at most
1,000 paths, dropping the longest-unchecked first.

Every request that talks to Dropbox gets one five-second deadline, started before connecting.
The provider reads and writes the socket itself, so each receive and send waits only for the
time left, and the clipboard gets at most two more seconds. That keeps a reply inside Strata's
eight-second limit. A badge request that runs out returns the badges it has;
the rest come with Strata's next refresh.

## The menu

Like Nautilus's Dropbox extension, the provider does not decide the menu. For a selection inside
your Dropbox it sends `icon_overlay_context_options`, and the client answers with its actions as
`label~inner~verb`, each field percent-encoded, for example `Version%20history~View the history
of changes to this file~revisions`. The provider reads them the way `nautilus-dropbox` does:

- A label starting with `!` is an action the client marks unavailable; it is left out.
- An inner field that decodes to something holding `~` is a submenu of `|`-separated options,
  which becomes a Strata branch, up to Strata's four levels.
- Every other option becomes an action with the verb as its id (`_` becomes `-`, as Strata ids
  allow only letters, digits and dashes). The tree stops at Strata's 64 nodes.

The provider keeps one table of the verbs it knows, for shorter labels and notices; others
appear with the client's own label. It hides two: the client's settings for this menu, and
`ignore_path` (Do not sync), which removes the item from Dropbox.com and other devices and
cannot be confirmed from Strata.

The whole menu shares one random, single-use token, valid for two minutes, bound to the
selection, the background flag, your Dropbox folders and each item's identity (device, inode
and type, without following a final symlink). Choosing an action re-checks all of them, then
sends `icon_overlay_context_action` with that verb, and the client does the rest, such as
opening Dropbox.com. Dropbox's commands take paths, not open files, so a file swapped in after
that check would still be the one acted on; the check narrows that window to the command
itself.

Copy link is the exception. The provider sends `get_shared_link` itself for each selected item,
up to 20, and copies the returned `https://` links, one per line, with `wl-copy`, because the
client may not reach a Wayland clipboard. `get_shared_link` **creates a link when there is
none**, the same as the client's own Copy link. Drawing a menu never calls it.

`wl-copy` forks to keep serving the clipboard. The provider does not wait on its output, and
starts it in its own session, so Strata stopping the provider does not end the clipboard.
Without `wl-copy`, the reply shows the links in Strata's dialog instead, as long as they fit in
12 KiB; past that it suggests a smaller selection or installing `wl-clipboard`.

## What the reply says

The reply tells Strata what actually happened:

| Case | Outcome |
| --- | --- |
| Done | `accepted`, with a short notice |
| Dropbox refused, or was not running, so nothing ran | `rejected` |
| Some links made, then one refused | `partial`, with the links made so far copied |
| Sent, but Dropbox stopped answering or sent something unexpected | `unknown`: it may have run. Check the Dropbox client |

Text from the client is cleaned of control characters and shortened before it reaches Strata.
Links are only accepted as printable `https://` URLs of at most 2,048 bytes, and they are not
repeated in a reply once they are on the clipboard. Replies go out as UTF-8 JSON and stay under
Strata's 1 MiB frame size: an oversized badge reply drops badges from the end for the next
refresh, and an oversized refresh event is split.

## Limits

- Up to 200 paths per badge request, Strata's own cap.
- Copy link handles up to 20 items at a time; larger selections do not offer it.
- Badges need the Dropbox desktop client running; there is no web API fallback.
