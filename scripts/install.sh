#!/bin/sh
# One-step setup and launcher for the COMP2 drone racing simulator on macOS
# and Linux.
#
#   curl -LsSf https://raw.githubusercontent.com/Squadrone-Australia/Drone-Competition-2/main/scripts/install.sh | sh
#
# Gets the code (git clone/pull, or a tarball download when git is missing),
# installs uv, creates venv/ with Python 3.13 or 3.14, installs the app and the
# squadrone client into it, then starts the simulator. It also writes a
# launcher ("Start COMP2.command" on macOS, start-comp2.sh on Linux) that runs
# this script again: every start pulls updates, reinstalls only when the code
# changed, and still starts offline as long as the existing install works.
#
# Environment variables:
#   COMP2_DIR        install folder (default: ./Drone-Competition-2, or the
#                    repository the script is run from)
#   COMP2_NO_LAUNCH  set to 1 to stop after installing (used by CI)
#
# The work happens in main(), called on the last line: sh reads a script as it
# runs, and the git pull below may rewrite this very file.

set -eu

REPO=https://github.com/Squadrone-Australia/Drone-Competition-2
API=https://api.github.com/repos/Squadrone-Australia/Drone-Competition-2
PORT=8765
STEPS=6
export PYTHONUTF8=1
# Where the uv installer puts uv; a launcher's shell may not have it on PATH.
PATH="$HOME/.local/bin:$PATH"

step() { printf '\033[36m[%s/%s] %s\033[0m\n' "$1" "$STEPS" "$2"; }
note() { printf '      %s\n' "$1"; }
warn() { printf '\033[33m      Warning: %s\033[0m\n' "$1"; }
fail() { printf '\033[31mCOMP2 setup failed: %s\033[0m\n' "$1" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
is_repo() { [ -f "$1/comp2/__main__.py" ] && [ -f "$1/client/pyproject.toml" ]; }

# fetch URL [OUTFILE]: curl or wget, to stdout when OUTFILE is omitted.
fetch() {
	if have curl; then
		if [ $# -gt 1 ]; then curl -LsSf "$1" -o "$2"; else curl -LsSf "$1"; fi
	elif have wget; then
		if [ $# -gt 1 ]; then wget -qO "$2" "$1"; else wget -qO- "$1"; fi
	else
		fail "neither curl nor wget is installed"
	fi
}

# The commit main points at, or nothing when GitHub can't be reached.
latest_commit() {
	if have curl; then
		curl -sf --max-time 15 -H 'Accept: application/vnd.github.sha' "$API/commits/main" 2>/dev/null || true
	elif have wget; then
		wget -qO- --timeout=15 --header='Accept: application/vnd.github.sha' "$API/commits/main" 2>/dev/null || true
	fi
}

# On macOS /usr/bin/git is a stub that pops up the Xcode tools installer
# unless the command line tools are present.
git_usable() {
	have git || return 1
	if [ "$(uname -s)" = Darwin ]; then xcode-select -p >/dev/null 2>&1 || return 1; fi
	return 0
}

# 0 when the simulator answers on the port, 1 when the port is free; fails
# when another program has it. Without curl the check is skipped.
sim_running() {
	have curl || return 1
	page=$(curl -s --max-time 2 "http://127.0.0.1:$PORT/") || return 1
	case "$page" in *Squadrone*) return 0 ;; esac
	fail "port $PORT is used by another program. Close it and run this again."
}

open_url() {
	if [ "$(uname -s)" = Darwin ]; then open "$1"; else xdg-open "$1" >/dev/null 2>&1 || note "Open $1 in your browser."; fi
}

# A venv the simulator can start from as it is.
venv_works() {
	[ -x "$PY" ] &&
		"$PY" -c 'import sys; sys.exit(not (3, 13) <= sys.version_info[:2] < (3, 15))' 2>/dev/null &&
		"$PY" -c 'import comp2, squadrone, cv2, av' >/dev/null 2>&1
}

# Steps 3-5. Returns non-zero on failure so the caller can fall back to a
# working install (set -e does not apply inside a function used with ||).
install_packages() {
	step 3 "Checking for uv (Python installer)"
	if ! have uv; then
		note "Installing uv from https://astral.sh/uv ..."
		fetch https://astral.sh/uv/install.sh | sh || return 1
		have uv || fail "uv was installed but cannot be found. Open a new terminal and run this again."
	fi
	uv --version || return 1

	step 4 "Preparing the Python environment (venv)"
	if [ -x "$PY" ] && "$PY" -c 'import sys; sys.exit(not (3, 13) <= sys.version_info[:2] < (3, 15))' 2>/dev/null; then
		:
	else
		[ ! -e "$VENV" ] || note "Existing venv is broken or not Python 3.13/3.14 - recreating it."
		# --seed puts pip in the venv so the README's manual update commands keep working.
		# The Python versions CI tests (.github/workflows/ci.yml); a newer release
		# may not have numpy/opencv/av wheels yet.
		uv venv "$VENV" --python ">=3.13,<3.15" --seed --clear || return 1
	fi
	"$PY" --version || return 1

	step 5 "Installing the simulator and the squadrone client"
	# Old name of the client package; the current one may share its files.
	uv pip uninstall --python "$PY" squadrone-client >/dev/null 2>&1 || true
	# Editable: code changes take effect as they are pulled; this step only
	# reruns to pick up changed dependencies.
	uv pip install --python "$PY" -e "$DIR" -e "$DIR/client" || return 1
	"$PY" -c 'import comp2, squadrone, cv2, av' || return 1
	if [ -n "$VERSION" ]; then echo "$VERSION" >"$STAMP"; fi
}

main() {
	# --- 1. Platform ---------------------------------------------------------
	step 1 "Checking this computer"
	OS=$(uname -s)
	case "$OS" in
	Darwin | Linux) ;;
	*) fail "unsupported system '$OS' - on Windows use scripts/install.ps1" ;;
	esac
	if [ "$OS" = Linux ]; then
		# opencv-python links against the system OpenGL and GLib libraries.
		LDCONFIG=$(command -v ldconfig || echo /sbin/ldconfig)
		if [ -x "$LDCONFIG" ]; then
			libs=$("$LDCONFIG" -p 2>/dev/null || true)
			case "$libs" in *libGL.so.1*) ;; *) missing=1 ;; esac
			case "$libs" in *libglib-2.0.so.0*) ;; *) missing=1 ;; esac
			if [ "${missing:-0}" = 1 ]; then
				fail "OpenCV needs the OpenGL and GLib system libraries. Install them, then run this again:
      Debian/Ubuntu:  sudo apt-get install -y libgl1 libglib2.0-0
      Fedora:         sudo dnf install -y mesa-libGL glib2"
			fi
		fi
	fi
	# Updating under the running app could break it, so just show it.
	if sim_running; then
		note "The simulator is already running - opening it in your browser."
		open_url "http://localhost:$PORT/"
		exit 0
	fi

	# Where to install: COMP2_DIR, else the repository this script or the
	# current folder belongs to, else a new folder here. Under `curl | sh`,
	# $0 is "sh".
	SCRIPT_REPO=$(cd "$(dirname "$0")/.." 2>/dev/null && pwd || true)
	if [ -n "${COMP2_DIR:-}" ]; then
		DIR=$COMP2_DIR
	elif [ -n "$SCRIPT_REPO" ] && is_repo "$SCRIPT_REPO"; then
		DIR=$SCRIPT_REPO
	elif is_repo "$PWD"; then
		DIR=$PWD
	else
		DIR=$PWD/Drone-Competition-2
	fi
	mkdir -p "$(dirname "$DIR")"
	DIR=$(cd "$(dirname "$DIR")" && pwd)/$(basename "$DIR")
	VENV=$DIR/venv
	PY=$VENV/bin/python
	# The code version the venv was last installed from (written in step 5).
	STAMP=$VENV/.comp2-installed
	# With a working install, a failed update is a warning, not the end.
	WORKING=0
	if venv_works; then WORKING=1; fi

	# --- 2. Code -------------------------------------------------------------
	step 2 "Getting the latest code into $DIR"
	VERSION=
	if git_usable && [ -d "$DIR/.git" ]; then
		git -C "$DIR" pull --ff-only >/dev/null 2>&1 ||
			warn "could not update (offline, or local changes). Using the code you have."
		VERSION=$(git -C "$DIR" rev-parse HEAD)
	elif git_usable && [ ! -e "$DIR" ]; then
		git clone "$REPO.git" "$DIR" || fail "git clone"
		VERSION=$(git -C "$DIR" rev-parse HEAD)
	elif [ -d "$DIR" ] && ! is_repo "$DIR" && [ -n "$(ls -A "$DIR")" ]; then
		fail "$DIR exists but is not the COMP2 repository. Set COMP2_DIR to another folder."
	else
		git_usable || note "git not found - using a tarball download instead."
		# Without git, .comp2-version holds the commit the files came from, so
		# the archive is only downloaded when main has moved on.
		marker=$DIR/.comp2-version
		have_ver=
		if is_repo "$DIR" && [ -f "$marker" ]; then have_ver=$(cat "$marker"); fi
		latest=$(latest_commit)
		if [ -n "$latest" ] && [ "$latest" = "$have_ver" ]; then
			VERSION=$have_ver
		elif [ -z "$latest" ] && is_repo "$DIR"; then
			warn "could not check for updates (offline?). Using the code you have."
			VERSION=$have_ver
		else
			ref=${latest:-refs/heads/main}
			tmp=$(mktemp -d)
			if fetch "$REPO/archive/$ref.tar.gz" "$tmp/code.tar.gz" && tar -xzf "$tmp/code.tar.gz" -C "$tmp"; then
				mkdir -p "$DIR"
				# Copy over the top: keeps venv/ and the contestant's own files.
				cp -R "$tmp"/Drone-Competition-2-*/. "$DIR/"
				if [ -n "$latest" ]; then echo "$latest" >"$marker"; fi
				VERSION=$latest
			elif is_repo "$DIR" && [ "$WORKING" = 1 ]; then
				warn "could not download the update. Using the code you have."
				VERSION=$have_ver
			else
				rm -rf "$tmp"
				fail "downloading the code"
			fi
			rm -rf "$tmp"
		fi
	fi
	is_repo "$DIR" || fail "no COMP2 code found in $DIR"

	installed=
	if [ -f "$STAMP" ]; then installed=$(cat "$STAMP"); fi
	if [ "$WORKING" = 1 ] && [ -n "$VERSION" ] && [ "$VERSION" = "$installed" ]; then
		# Same code as the last install: no uv or network needed.
		note "Already up to date."
	elif ! install_packages; then
		# Offline with an install that still works: start that one.
		venv_works || fail "installing COMP2 (see the messages above)"
		warn "could not finish updating. Starting the version you have."
	fi

	# --- 6. Launch -----------------------------------------------------------
	# The launcher finds the folder from its own location, so it survives a move.
	if [ "$OS" = Darwin ]; then
		LAUNCHER="$DIR/Start COMP2.command" # Finder runs .command files in Terminal
	else
		LAUNCHER="$DIR/start-comp2.sh"
	fi
	printf '#!/bin/sh\ncd "$(dirname "$0")" && exec sh scripts/install.sh\n' >"$LAUNCHER"
	chmod +x "$LAUNCHER"

	printf '\n\033[32mCOMP2 is ready.\033[0m\n'
	printf '  Select this interpreter in your IDE:  %s\n' "$PY"
	if [ "$OS" = Darwin ]; then
		printf '  Next time, double-click "Start COMP2" in %s\n' "$DIR"
	else
		printf '  Next time, run: "%s"\n' "$LAUNCHER"
	fi
	printf '  It updates COMP2, then starts it.\n\n'
	[ "${COMP2_NO_LAUNCH:-}" != 1 ] || exit 0
	step 6 "Starting the simulator (Ctrl+C to stop)"
	cd "$DIR"
	exec "$PY" -m comp2
}

main "$@"
