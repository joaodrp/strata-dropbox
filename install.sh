#!/bin/bash
# Install, update or remove the Dropbox provider for Strata.
#
#   curl -fsSL https://raw.githubusercontent.com/joaodrp/strata-dropbox/main/install.sh | bash
#   ./install.sh               # from a clone: installs that checkout
#   ./install.sh --uninstall
#
# Re-running updates in place. Strata picks the change up after
# Settings -> Integrations -> Refresh, or a restart. Set PYTHON to choose the
# interpreter Strata runs the provider with.
set -euo pipefail

source_url=${STRATA_DROPBOX_ARCHIVE:-https://github.com/joaodrp/strata-dropbox/archive/refs/heads/main.tar.gz}
providers="${XDG_CONFIG_HOME:-$HOME/.config}/strata/providers"
dir="$providers/dropbox"

die() { echo "error: $*" >&2; exit 1; }

if [[ ${1:-} == --uninstall ]]; then
    # Removes any registration with the id "dropbox", whoever installed it.
    if grep -qsE '"id": *"dropbox"' "$dir/provider.json"; then
        rm -rf -- "$dir"
        echo "Removed $dir. Refresh Settings -> Integrations in Strata."
    else
        echo "Nothing to remove at $dir." >&2
    fi
    exit 0
fi

# Strata runs provider commands as literal argv, so the interpreter path must
# be absolute, and the provider needs Python 3.10 or newer.
python=""
for candidate in ${PYTHON:+"$PYTHON"} /usr/bin/python3 "$(command -v python3 || true)"; do
    [[ $candidate == /* && -x $candidate ]] || continue
    if "$candidate" -I -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        python=$candidate
        break
    fi
done
[[ -n $python ]] || die "Python 3.10 or newer is required; set PYTHON to an absolute path to one."
command -v dropbox-cli >/dev/null || command -v dropbox >/dev/null \
    || echo "warning: the Dropbox client is not installed; files will show no badges." >&2

# Strata ignores registration folders that are symlinks, owned by someone else,
# or writable by group or others. Refuse rather than install where it won't load.
check_dir() {
    local path=$1
    [[ -e $path || -L $path ]] || return 0
    [[ ! -L $path ]] || die "$path is a symlink; Strata will not load providers from it."
    [[ -d $path ]] || die "$path exists and is not a directory."
    local owner mode
    owner=$(stat -c %u -- "$path")
    mode=$(stat -c %a -- "$path")
    [[ $owner == "$(id -u)" || $owner == 0 ]] || die "$path is owned by another user; Strata will not load it."
    (( (8#$mode & 8#022) == 0 )) || die "$path is writable by group or others (mode $mode); run: chmod go-w '$path'"
}
check_dir "$providers"
check_dir "$dir"

tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
# Use a checkout only when this script runs from a file. Piped into bash there
# is no script file, so download rather than trust the current directory.
here=""
if [[ -f ${BASH_SOURCE[0]:-} ]]; then
    here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
fi
if [[ ! -f $here/provider.py ]]; then
    curl -fsSL "$source_url" | tar -xz -C "$tmp" --strip-components=1
    here=$tmp
fi
"$python" -I -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$here/provider.py"
# The badges are whatever icons/ holds; a provider test keeps them in step with
# the states the provider reports.
badges=()
for png in "$here"/icons/*.png; do
    [[ -f $png ]] && badges+=("$(basename -- "$png" .png)")
done
(( ${#badges[@]} > 0 )) || die "no badge images found"

[[ -d $providers ]] || install -d -m 700 -- "$providers"
install -d -m 755 -- "$dir"
install -m 644 -- "$here/provider.py" "$dir/provider.py"
for badge in "${badges[@]}"; do
    install -m 644 -- "$here/icons/$badge.png" "$dir/$badge.png"
done
# The badge glyphs are Lucide's; their licence travels with them.
install -m 644 -- "$here/icons/LICENSE-lucide" "$dir/LICENSE-lucide"
# Strata loads icons from plain files beside provider.json.
"$python" -I - "$python" "$dir/provider.py" "${badges[@]}" >"$tmp/provider.json" <<'PY'
import json, sys
python, script, badges = sys.argv[1], sys.argv[2], sys.argv[3:]
print(json.dumps({"version": 1, "id": "dropbox", "name": "Dropbox", "category": "cloud",
                  "command": [python, "-I", script],
                  "icons": {b: f"{b}.png" for b in badges}}, indent=2))
PY
install -m 644 -- "$tmp/provider.json" "$dir/provider.json"
echo "Installed to $dir. In Strata: Settings -> Integrations -> Refresh."
