#!/bin/sh
# One-step setup for the COMP2 drone racing simulator on macOS and Linux.
#
#   curl -LsSf https://raw.githubusercontent.com/Squadrone-Australia/Drone-Competition-2/main/scripts/install.sh | sh
#
# Gets the code (git clone/pull, or a tarball download when git is missing),
# installs uv, creates venv/ with Python 3.13 or 3.14, installs the app and the
# squadrone client into it, then starts the simulator. Run it again to update.
#
# Environment variables:
#   COMP2_DIR        install folder (default: ./Drone-Competition-2, or the
#                    repository the script is run from)
#   COMP2_NO_LAUNCH  set to 1 to stop after installing (used by CI)

set -eu

REPO=https://github.com/Squadrone-Australia/Drone-Competition-2
PORT=8765
STEPS=6
export PYTHONUTF8=1

step() { printf '\033[36m[%s/%s] %s\033[0m\n' "$1" "$STEPS" "$2"; }
note() { printf '      %s\n' "$1"; }
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

# On macOS /usr/bin/git is a stub that pops up the Xcode tools installer
# unless the command line tools are present.
git_usable() {
	have git || return 1
	if [ "$(uname -s)" = Darwin ]; then xcode-select -p >/dev/null 2>&1 || return 1; fi
	return 0
}

# --- 1. Platform -------------------------------------------------------------
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
if have curl && curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/"; then
	fail "port $PORT is in use - the simulator is probably already running. Close it (Ctrl+C in its window) and run this again."
fi

# Where to install: COMP2_DIR, else the repository this script or the current
# folder belongs to, else a new folder here. Under `curl | sh`, $0 is "sh".
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

# --- 2. Code -------------------------------------------------------------------
step 2 "Getting the code into $DIR"
if git_usable && [ -d "$DIR/.git" ]; then
	git -C "$DIR" pull --ff-only || note "Warning: git pull failed (local changes?). Continuing with the code you have."
elif git_usable && [ ! -e "$DIR" ]; then
	git clone "$REPO.git" "$DIR" || fail "git clone"
elif [ -d "$DIR" ] && ! is_repo "$DIR" && [ -n "$(ls -A "$DIR")" ]; then
	fail "$DIR exists but is not the COMP2 repository. Set COMP2_DIR to another folder."
else
	git_usable || note "git not found - downloading an archive instead (run this script again to update)."
	tmp=$(mktemp -d)
	trap 'rm -rf "$tmp"' EXIT
	fetch "$REPO/archive/refs/heads/main.tar.gz" "$tmp/main.tar.gz" || fail "downloading the code"
	tar -xzf "$tmp/main.tar.gz" -C "$tmp"
	mkdir -p "$DIR"
	# Copy over the top: keeps venv/ and the contestant's own files.
	cp -R "$tmp/Drone-Competition-2-main/." "$DIR/"
	rm -rf "$tmp"
	trap - EXIT
fi
is_repo "$DIR" || fail "no COMP2 code found in $DIR"

# --- 3. uv ---------------------------------------------------------------------
step 3 "Checking for uv (Python installer)"
if ! have uv; then
	note "Installing uv from https://astral.sh/uv ..."
	fetch https://astral.sh/uv/install.sh | sh || fail "uv install"
	PATH="$HOME/.local/bin:$PATH"
	have uv || fail "uv was installed but cannot be found. Open a new terminal and run this again."
fi
uv --version

# --- 4. venv -------------------------------------------------------------------
step 4 "Preparing the Python environment (venv)"
VENV=$DIR/venv
PY=$VENV/bin/python
if [ -x "$PY" ] && "$PY" -c 'import sys; sys.exit(not (3, 13) <= sys.version_info[:2] < (3, 15))' 2>/dev/null; then
	:
else
	[ -e "$VENV" ] && note "Existing venv is broken or not Python 3.13/3.14 - recreating it."
	# --seed puts pip in the venv so the README's manual update commands keep working.
	# The Python versions CI tests (.github/workflows/ci.yml); a newer release
	# may not have numpy/opencv/av wheels yet.
	uv venv "$VENV" --python ">=3.13,<3.15" --seed --clear || fail "creating the venv"
fi
"$PY" --version

# --- 5. Packages ---------------------------------------------------------------
step 5 "Installing the simulator and the squadrone client"
# Old name of the client package; the current one may share its files.
uv pip uninstall --python "$PY" squadrone-client >/dev/null 2>&1 || true
# Editable: a later git pull takes effect without reinstalling.
uv pip install --python "$PY" -e "$DIR" -e "$DIR/client" || fail "installing packages"
"$PY" -c 'import comp2, squadrone, cv2, av' || fail "import check"

# --- 6. Launch -----------------------------------------------------------------
printf '\n\033[32mCOMP2 is installed.\033[0m\n'
printf '  Select this interpreter in your IDE:  %s\n' "$PY"
printf '  Start the simulator later with:       "%s" -m comp2\n\n' "$PY"
[ "${COMP2_NO_LAUNCH:-}" = 1 ] && exit 0
step 6 "Starting the simulator (Ctrl+C to stop)"
cd "$DIR"
exec "$PY" -m comp2
