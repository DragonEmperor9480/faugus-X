#!/bin/sh
# Faugus X installer. Installs alongside Faugus; never touches a Faugus install.
#
#   ./packaging/faugus-x/install.sh                 install for this user (~/.local)
#   sudo ./packaging/faugus-x/install.sh --system   install for everyone (/usr/local)
#   ./packaging/faugus-x/install.sh --uninstall     remove (add --system if installed that way)
#
# Packagers: --prefix=/usr --destdir="$pkgdir"
#
# Layout:
#   $PREFIX/lib/faugus-x/          the app (its own copy of the faugus package)
#   $PREFIX/bin/faugus-x           command
#   $PREFIX/share/applications/    app menu entry
#   $PREFIX/share/icons/hicolor/   icon
set -eu

APP_ID="io.github.DragonEmperor9480.faugus-x"
PREFIX="${HOME}/.local"
DESTDIR=""
ACTION="install"

for arg in "$@"; do
	case "$arg" in
		--system)     PREFIX="/usr/local" ;;
		--prefix=*)   PREFIX="${arg#--prefix=}" ;;
		--destdir=*)  DESTDIR="${arg#--destdir=}" ;;
		--uninstall)  ACTION="uninstall" ;;
		-h|--help)    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "Unknown option: $arg" >&2; exit 1 ;;
	esac
done

ROOT="${DESTDIR}${PREFIX}"
LIBDIR="${ROOT}/lib/faugus-x"
BINDIR="${ROOT}/bin"
APPDIR="${ROOT}/share/applications"
ICONDIR="${ROOT}/share/icons/hicolor/scalable/apps"
DOCDIR="${ROOT}/share/licenses/faugus-x"

if [ "$ACTION" = "uninstall" ]; then
	rm -rf "$LIBDIR" "$DOCDIR"
	rm -f "$BINDIR/faugus-x" "$APPDIR/$APP_ID.desktop" "$ICONDIR/$APP_ID.svg"
	if [ -z "$DESTDIR" ]; then
		# "Open when a controller connects" watcher.
		rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/faugus-x-controller.desktop"
		pkill -f "faugus.controller_watch" 2>/dev/null || true
	fi
	echo "Faugus X removed from $ROOT."
	echo "Your games, artwork and settings (~/.config/faugus-x) were kept."
	exit 0
fi

SRC=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)

# Check runtime dependencies (skipped when packaging into a DESTDIR).
if [ -z "$DESTDIR" ]; then
	missing=""
	python3 - <<'EOF' || missing="yes"
import importlib.util, sys
mods = ["gi", "requests", "PIL", "vdf", "psutil"]
bad = [m for m in mods if importlib.util.find_spec(m) is None]
try:
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_version("Manette", "0.2")
except Exception as error:
    bad.append(f"GTK4/libadwaita/libmanette ({error})")
if bad:
    print("Missing dependencies: " + ", ".join(bad), file=sys.stderr)
    sys.exit(1)
EOF
	if [ -n "$missing" ]; then
		echo "Install them first (same as Faugus):" >&2
		echo "  python-gobject python-requests python-pillow python-vdf python-psutil icoextract gtk4 libadwaita libmanette" >&2
		exit 1
	fi
fi

echo "Installing Faugus X to $ROOT"
rm -rf "$LIBDIR"
mkdir -p "$LIBDIR" "$BINDIR" "$APPDIR" "$ICONDIR" "$DOCDIR"

cp -r "$SRC/faugus" "$SRC/assets" "$LIBDIR/"
find "$LIBDIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
install -m 755 "$SRC/faugus-launcher" "$LIBDIR/faugus-x"

# Translations (optional: needs msgfmt from gettext).
if command -v msgfmt >/dev/null 2>&1; then
	for po in "$SRC"/languages/*/faugus-launcher.po; do
		[ -f "$po" ] || continue
		lang=$(basename "$(dirname "$po")")
		mkdir -p "$LIBDIR/languages/$lang/LC_MESSAGES"
		msgfmt -o "$LIBDIR/languages/$lang/LC_MESSAGES/faugus-launcher.mo" "$po"
	done
fi

cat > "$BINDIR/faugus-x" <<EOF
#!/bin/sh
exec "${PREFIX}/lib/faugus-x/faugus-x" "\$@"
EOF
chmod 755 "$BINDIR/faugus-x"

install -m 644 "$SRC/assets/$APP_ID.svg" "$ICONDIR/$APP_ID.svg"
install -m 644 "$SRC/LICENSE" "$DOCDIR/LICENSE"

cat > "$APPDIR/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Faugus X
GenericName=Game Launcher
Comment=Console-style launcher for your Windows and Linux games
Exec=${PREFIX}/bin/faugus-x %f
Icon=$APP_ID
Terminal=false
Categories=Game;
Keywords=games;launcher;proton;wine;umu;
MimeType=application/x-ms-dos-executable;application/x-msi;application/x-ms-shortcut;application/x-bat;text/x-ms-regedit
StartupWMClass=$APP_ID
EOF

if [ -z "$DESTDIR" ]; then
	command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$APPDIR" 2>/dev/null || true
	# Only refresh an icon cache that already exists (a new, partial cache in ~/.local can hide icons).
	if [ -f "${ROOT}/share/icons/hicolor/icon-theme.cache" ] && command -v gtk-update-icon-cache >/dev/null 2>&1; then
		gtk-update-icon-cache -q -t -f "${ROOT}/share/icons/hicolor" 2>/dev/null || true
	fi
	# KDE Plasma keeps its own app-menu cache.
	command -v kbuildsycoca6 >/dev/null 2>&1 && kbuildsycoca6 >/dev/null 2>&1 || true
	command -v kbuildsycoca5 >/dev/null 2>&1 && kbuildsycoca5 >/dev/null 2>&1 || true
fi

echo "Done. Start \"Faugus X\" from your app menu, or run: faugus-x"
case ":$PATH:" in
	*":$BINDIR:"*) ;;
	*) [ -z "$DESTDIR" ] && echo "Note: $BINDIR is not in your PATH, so use the app menu or $BINDIR/faugus-x." ;;
esac
