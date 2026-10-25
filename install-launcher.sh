#!/usr/bin/env bash
# Install U-HARNESS UI into the Fedora desktop: an icon in the application
# grid, a menu entry that launches the window, and a `uharness-ui` command on
# PATH. Nothing is copied out of this repo except the icon and the .desktop
# file - the launcher is symlinked, so edits here take effect immediately.
#
#     ./install-launcher.sh              # install (idempotent, safe to re-run)
#     ./install-launcher.sh --uninstall  # remove everything it added
#
# Everything lands under $HOME, so no sudo is needed:
#
#     ~/.local/bin/uharness-ui                                   symlink
#     ~/.local/share/applications/uharness-ui.desktop             menu entry
#     ~/.local/share/icons/hicolor/scalable/apps/uharness-ui.svg  icon
set -euo pipefail

source="${BASH_SOURCE[0]}"
while [ -L "$source" ]; do
  dir="$(cd -P "$(dirname "$source")" && pwd)"
  source="$(readlink "$source")"
  [[ "$source" != /* ]] && source="$dir/$source"
done
here="$(cd -P "$(dirname "$source")" && pwd)"

data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
bin_dir="$HOME/.local/bin"
apps_dir="$data_home/applications"
icons_dir="$data_home/icons/hicolor/scalable/apps"

launcher_src="$here/uharness-ui-launcher"
desktop_src="$here/uharness-ui.desktop"
icon_src="$here/uharness-ui.svg"

launcher_dst="$bin_dir/uharness-ui"
desktop_dst="$apps_dir/uharness-ui.desktop"
icon_dst="$icons_dir/uharness-ui.svg"

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }

# Refresh the caches the desktop reads, when the tools are installed. A missing
# tool is never an error: the entry still works, the grid just notices later.
refresh_caches() {
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$apps_dir" >/dev/null 2>&1 || true
  fi
  if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$data_home/icons/hicolor" >/dev/null 2>&1 || true
  fi
}

uninstall() {
  local removed=0
  for path in "$launcher_dst" "$desktop_dst" "$icon_dst"; do
    if [ -e "$path" ] || [ -L "$path" ]; then
      rm -f "$path"
      say "removed $path"
      removed=$((removed + 1))
    fi
  done
  refresh_caches
  if [ "$removed" -eq 0 ]; then
    say "nothing to remove - U-HARNESS UI was not installed here"
  else
    say "U-HARNESS UI removed from the desktop"
  fi
}

case "${1:-}" in
  -h|--help)
    sed -n '2,14p' "$source" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  --uninstall|--remove)
    uninstall
    exit 0
    ;;
  "")
    ;;
  *)
    warn "unknown option: $1 (try --help)"
    exit 2
    ;;
esac

for required in "$launcher_src" "$desktop_src" "$icon_src"; do
  if [ ! -e "$required" ]; then
    warn "missing $required - run this from the uharness repo"
    exit 2
  fi
done

mkdir -p "$bin_dir" "$apps_dir" "$icons_dir"

# 1. The command. A symlink, not a copy, so the installed command always runs
#    the code in this repo (the launcher resolves its own symlink).
chmod +x "$launcher_src"
ln -sf "$launcher_src" "$launcher_dst"
say "linked   $launcher_dst -> $launcher_src"

# 2. The menu entry. @LAUNCHER@ and @HOME@ are filled in from this machine's
#    real paths, because a .desktop file cannot expand $HOME itself.
sed -e "s|@LAUNCHER@|$launcher_dst|g" \
    -e "s|@HOME@|$HOME|g" \
    "$desktop_src" >"$desktop_dst"
chmod +x "$desktop_dst"
say "wrote    $desktop_dst"

# 3. The icon, in the scalable theme directory so any size looks sharp.
install -m 0644 "$icon_src" "$icon_dst"
say "wrote    $icon_dst"

refresh_caches

# Validate after installing, so a typo in the entry is reported here rather
# than quietly producing an icon that does nothing.
if command -v desktop-file-validate >/dev/null 2>&1; then
  if desktop-file-validate "$desktop_dst"; then
    say "valid    desktop entry passes desktop-file-validate"
  else
    warn "desktop-file-validate complained about $desktop_dst"
  fi
else
  warn "desktop-file-validate not installed (dnf install desktop-file-utils)"
fi

case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *)
    warn "$bin_dir is not on PATH - the icon still works, but the"
    warn "'uharness-ui' command needs: export PATH=\"\$HOME/.local/bin:\$PATH\""
    ;;
esac

say ""
say "Done. 'U-HARNESS UI' is now in the application grid."
say "  from the grid:  Activities -> type 'uharness'"
say "  from a shell:   uharness-ui --workspace ~/some/project"
say "  a prompt:       uharness-ui \"summarise README.md\""
say ""
say "Set UHARNESS_WORKSPACE in ~/.config/environment.d/ to choose the folder"
say "the icon opens on, or use Conversation > Change workspace in the window."
