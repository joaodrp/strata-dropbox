# Contributing

Bug reports and pull requests are welcome. Read [How it works](docs/how-it-works.md) first: it
covers the Dropbox socket, the badges and the checks every change must keep.

If badges are wrong or missing, include what `dropbox-cli filestatus <file>` prints, your Strata
version (`strata --version`), and your Dropbox client version (`dropbox-cli version`).

## Layout

| File | |
| --- | --- |
| `provider.py` | Everything: the protocol loop, the Dropbox socket client, badges, the sync watcher and the menu |
| `icons/*.png`, `icons/src/*.svg` | The badges Strata loads, and their sources; see [Badges](#badges) |
| `install.sh` | Copies the provider and badges into Strata's providers folder and writes `provider.json` |
| `tests/test_provider.py` | Unit tests, standard library only, against a fake Dropbox socket |
| `tests/test_install.sh` | Installs, updates and removes into a throwaway config folder |
| [`docs/how-it-works.md`](docs/how-it-works.md) | The protocol, the socket, badge priorities and how they stay current |
| `docs/images/` | README screenshots; see [Screenshots](#screenshots) |
| `.github/`, `release-please-config.json`, `.release-please-manifest.json` | CI and the release automation; see [Releases](#releases) |

## Running it

Install your checkout into Strata, then select **Refresh** in **Settings -> Integrations**:

```sh
./install.sh
```

Run it again after every change, then Refresh. To talk to the provider without Strata, pipe a
request into it; it asks your real Dropbox client:

```sh
echo '{"version":1,"id":1,"method":"query","paths":["'"$HOME"'/Dropbox"],"background":false}' \
  | python3 -I provider.py
```

Asking for the menu is safe, but do not activate actions against your real account while
testing: Copy link creates a public link, and other actions act on your account.

## Checks

```sh
python3 -I -B -m unittest discover -s tests -v
tests/test_install.sh
shellcheck install.sh tests/test_install.sh icons/render.sh
```

CI runs all three. The tests use a fake Dropbox socket and a fake `wl-copy`, so they never touch
a real account or clipboard. The installer test points `STRATA_DROPBOX_ARCHIVE` at a tarball of
the checkout to exercise the piped `curl | bash` path without downloading.

Coverage, with the standard library's `trace`:

```sh
python3 -I -B -m trace --count --missing --summary --coverdir=/tmp/cover \
  --module unittest discover -s tests | grep provider
grep -n '>>>>>>' /tmp/cover/*provider.cover    # lines no test ran
```

Only `dropbox_home()` and the `__main__` call show as uncovered; a subprocess test reaches them.

## Badges

Each badge is a coloured disc with a light ring, so it reads on both dark and light themes, and
a white [Lucide](https://lucide.dev/) glyph at Lucide's stroke weight, so it sits well beside
Strata's own icons. Edit the SVGs in `icons/src/`, then render:

```sh
icons/render.sh    # needs rsvg-convert (librsvg) and optipng
```

Check every badge at 16 px on a dark and a light background before committing.

## Screenshots

The README pictures come from a demo Strata, never your own Dropbox:

- Give it its own home with a sample `Dropbox` folder, and symlink `~/.local/state/omarchy` into
  that home so it follows your Omarchy theme.
- Run it under `dbus-run-session` so it does not hand off to your running Strata, with `HOME`
  and `XDG_CONFIG_HOME` pointing at the demo.
- Register a stand-in provider that wraps `provider.py` with a fake Dropbox client returning one
  of each state and the client's menu, and a fake clipboard.
- Use the Icons view for the badges, and collapse the sidebar (`Ctrl+B`).

Capture at native scale with `grim -g`, crop with even margins, and run `optipng -o2`. Show
images at half their pixel width in the README on a 2x display.

## Conventions

Standard library only, one file. A provider is something people install by copying, so a
dependency is a cost every user pays.

Comments carry what the code cannot: why an obvious alternative was rejected. They describe the
current state, never the change; git history holds that.

## Gotchas

- **Edits need a Refresh** in Strata before they run.
- **Registration files must not be group or world writable**, and cannot be symlinks.
- **Never act outside activation.** `get_shared_link` makes a public link and
  `icon_overlay_context_action` changes the account; drawing a menu only asks for the options.
- **Report what happened, not what was meant to.** Once a command is sent, a lost reply means
  `unknown`, not `rejected`; a batch that fails part way is `partial`.
- **Dropbox's menu fields are percent-encoded, `~` included.** Python's `quote` leaves `~`
  alone, so the tests encode it by hand.
- **Never wait on `wl-copy`'s output.** It forks to keep serving the clipboard, and the child
  holds any pipe open.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/), one logical change each. Describe
what the change does and why, in the body, with backticks around identifiers.

## Releases

[release-please](https://github.com/googleapis/release-please) reads the Conventional Commits on
`main` and keeps a release pull request open with the next version and the changelog. Merging
it bumps `__version__` in `provider.py`, tags `vX.Y.Z` and publishes the GitHub release, then
checks that the tag and `__version__` agree. Before 1.0, `feat` bumps the minor version and
`fix` the patch.

## Scope

Dropbox sync state and links in Strata. Settings such as selective sync or pausing belong to the
Dropbox client and stay there.
