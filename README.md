# Strata Dropbox

[![CI](https://github.com/joaodrp/strata-dropbox/actions/workflows/ci.yml/badge.svg)](https://github.com/joaodrp/strata-dropbox/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

See what Dropbox has synced, right in the [Strata](https://stratafiles.io/) file manager. Every
file and folder in your Dropbox gets a badge for its sync state, and the right-click menu has
Dropbox's own actions: copy a link, version history, and more.

<img src="docs/images/badges.png" alt="A Dropbox folder in Strata's icon view: badges show synced, syncing, can't sync, and excluded files" width="520">

## Highlights

- ✅ A badge on every file and folder in your Dropbox: synced, syncing, can't sync, or excluded
  from Dropbox sync. Problems outrank normal and syncing badges, from this or other providers.
- 🔄 Badges update by themselves when a sync finishes. No refreshing.
- 🔗 Dropbox's own right-click menu: Copy link, Version history, View on Dropbox.com and View
  comments, as the Dropbox client offers them for what you selected.
- 🪶 Talks to the Dropbox client you already run, the same way Nautilus's Dropbox extension
  does. One Python file, standard library only, no account sign-in.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/joaodrp/strata-dropbox/main/install.sh | bash
```

Then in Strata open **Settings -> Integrations** and select **Refresh**. Dropbox shows up as
manually installed, with a switch to turn it off.

Prefer to read before you run? The [installer](install.sh) is short: it copies `provider.py`
and four badge images into `~/.config/strata/providers/dropbox/` and writes the
`provider.json` Strata reads. From a clone, `./install.sh` installs that checkout.

## Badges

| Badge | Means |
| --- | --- |
| <img src="icons/synced.png" width="18" alt="Green check"> | Synced: this computer and Dropbox match |
| <img src="icons/syncing.png" width="18" alt="Blue arrows"> | Syncing: uploading or downloading now |
| <img src="icons/error.png" width="18" alt="Red exclamation mark"> | Can't sync: Dropbox refuses this file, often for its name or size. Check the Dropbox client |
| <img src="icons/excluded.png" width="18" alt="Grey minus"> | Excluded: ignored, or left out of this computer by selective sync |

Files outside your Dropbox folder get no badge, and nothing is asked about them.

## The Dropbox menu

Right-click anything in your Dropbox, or the background of a Dropbox folder, and the Dropbox
section lists what the Dropbox client offers for that selection:

<img src="docs/images/menu.png" alt="Strata's context menu on a Dropbox file: Copy link, Version history, View on Dropbox.com and View comments under a Dropbox heading" width="520">

| Action | Does |
| --- | --- |
| Copy link | Copies a Dropbox link to your clipboard, one per line for several items (up to 20) |
| Version history | Opens the file's earlier versions on Dropbox.com |
| View on Dropbox.com | Opens the file or folder on Dropbox.com |
| View comments | Opens the file's comments on Dropbox.com |

The list comes from the client, so it changes with the selection: a folder gets fewer actions
than a file. Actions the client marks unavailable are left out. Copy link confirms with a
notice:

<img src="docs/images/notice.png" alt="Strata's notice: Dropbox, Link copied" width="354">

**Copy link creates a link when the file does not have one yet**, the same as the Dropbox
client's own Copy link. Anyone with the link can see the file, within your account's sharing
settings.

**Do not sync is left out on purpose.** Dropbox calls it ignoring a file: the file stays on this
computer but is [removed from Dropbox.com and your other
devices](https://help.dropbox.com/sync/ignored-files). Strata cannot ask you to confirm a
provider action, so a stray click would be too costly. Use the Dropbox client when you mean it.

## Requirements

- [Strata](https://stratafiles.io/) 1.1.1 or newer
- The [Dropbox desktop client](https://www.dropbox.com/install-linux), running
- Python 3.10 or newer, at `/usr/bin/python3` or on `PATH`
- `wl-copy` (`wl-clipboard`) for the clipboard. Without it, Strata shows the links to copy by
  hand, for selections small enough to fit

## Troubleshooting

| You see | Try |
| --- | --- |
| No badges at all | Dropbox is not running, or the files are outside your Dropbox folder. Check with `dropbox-cli status` |
| Dropbox missing from Integrations | Select **Refresh**, or restart Strata |
| No Dropbox actions in the menu | The selection includes something outside your Dropbox, or Dropbox is not running |
| "This menu expired" | The menu was open for over two minutes. Open it again |

## Privacy

- **Nothing leaves your computer that Dropbox would not send anyway.** Badges come from the
  local Dropbox client; this provider makes no network connections of its own.
- **It never reads your files.** It asks Dropbox about paths, and only paths inside your Dropbox.
- **Nothing happens until you choose an action.** Drawing the menu only asks Dropbox which
  actions it offers; a link is only created when you choose Copy link.

[How it works](docs/how-it-works.md) has the detail.

## Update

Run the installer again; it updates in place. Select **Refresh** in Strata afterwards.

## Remove

```sh
curl -fsSL https://raw.githubusercontent.com/joaodrp/strata-dropbox/main/install.sh | bash -s -- --uninstall
```

Or select the bin icon next to Dropbox in **Settings -> Integrations**.

## Contributing

Bug reports and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) to get set up,
and [docs/how-it-works.md](docs/how-it-works.md) for how the pieces fit.

## License

MIT, see [LICENSE](LICENSE). The badge glyphs are from [Lucide](https://lucide.dev/), under the
ISC license; see [icons/LICENSE-lucide](icons/LICENSE-lucide). Not affiliated with Dropbox or
Strata.
