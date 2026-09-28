#!/bin/sh
# Assemble "MIT Scheme IDE.app", a macOS application bundle around
# ide/mit_scheme_ide.py.
#
# usage: make-app.sh [-o OUTDIR]
#
#   -o OUTDIR   where to write the bundle (default: the current directory)
#
# The bundle needs no build step and no Python packages: it is the IDE
# source, an icon, an Info.plist, and a small launcher that finds a
# python3 with Tk on the user's Mac and runs the IDE with it.  It can be
# assembled on any POSIX system (this script is plain sh); it just runs
# on macOS.  make-dmg.sh calls this, then wraps the result in a disk
# image.
#
# The launcher looks for Python in this order: $MIT_SCHEME_IDE_PYTHON,
# the python.org framework install (which bundles Tk), Homebrew,
# MacPorts, /usr/local, then whatever "python3" is on PATH.  Finder
# starts apps with a minimal PATH, so the launcher also adds the usual
# Homebrew/MacPorts/~/opt/mit-scheme locations before the IDE looks for
# mit-scheme.

set -e

USAGE="usage: ${0} [-o OUTDIR]"
OUTDIR=.

while getopts o: opt; do
    case ${opt} in
        o) OUTDIR=${OPTARG} ;;
        *) echo "${USAGE}" >&2
           exit 1 ;;
    esac
done
shift $((OPTIND - 1))
if [ $# -ne 0 ]; then
    echo "${USAGE}" >&2
    exit 1
fi

HERE=$(cd "$(dirname "${0}")" && pwd)
IDE=$(dirname "${HERE}")
SRC=${IDE}/mit_scheme_ide.py
ICON=${HERE}/MITSchemeIDE.icns

for f in "${SRC}" "${ICON}"; do
    if [ ! -f "${f}" ]; then
        echo "${0}: missing ${f}" >&2
        exit 1
    fi
done

VERSION=$(sed -n 's/^VERSION = "\([^"]*\)"/\1/p' "${SRC}" | head -1)
: "${VERSION:=0.0}"

APP="${OUTDIR}/MIT Scheme IDE.app"
CONTENTS="${APP}/Contents"

echo "assembling ${APP} (version ${VERSION})"
rm -rf "${APP}"
mkdir -p "${CONTENTS}/MacOS" "${CONTENTS}/Resources/examples"

cp "${SRC}" "${CONTENTS}/Resources/mit_scheme_ide.py"
cp "${ICON}" "${CONTENTS}/Resources/MITSchemeIDE.icns"
cp "${IDE}/README.md" "${CONTENTS}/Resources/README.md"
if [ -d "${IDE}/examples" ]; then
    cp "${IDE}"/examples/*.scm "${CONTENTS}/Resources/examples/"
fi

printf 'APPL????' > "${CONTENTS}/PkgInfo"

cat > "${CONTENTS}/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleDisplayName</key>
    <string>MIT Scheme IDE</string>
    <key>CFBundleExecutable</key>
    <string>mit-scheme-ide</string>
    <key>CFBundleIconFile</key>
    <string>MITSchemeIDE</string>
    <key>CFBundleIdentifier</key>
    <string>org.gnu.mit-scheme.ide</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>MIT Scheme IDE</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>${VERSION}</string>
    <key>CFBundleVersion</key>
    <string>${VERSION}</string>
    <key>CFBundleSignature</key>
    <string>????</string>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.developer-tools</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSHumanReadableCopyright</key>
    <string>GNU General Public License, version 2 or later</string>
    <key>CFBundleDocumentTypes</key>
    <array>
        <dict>
            <key>CFBundleTypeName</key>
            <string>Scheme source</string>
            <key>CFBundleTypeRole</key>
            <string>Editor</string>
            <key>CFBundleTypeExtensions</key>
            <array>
                <string>scm</string>
                <string>ss</string>
                <string>sld</string>
                <string>sls</string>
                <string>pkg</string>
            </array>
            <key>CFBundleTypeIconFile</key>
            <string>MITSchemeIDE</string>
            <key>LSHandlerRank</key>
            <string>Alternate</string>
        </dict>
    </array>
</dict>
</plist>
EOF

cat > "${CONTENTS}/MacOS/mit-scheme-ide" <<'EOF'
#!/bin/sh
# Launcher for MIT Scheme IDE.app: run the IDE with a python3 that has Tk.

HERE=$(cd "$(dirname "$0")" && pwd)
RESOURCES=$(dirname "${HERE}")/Resources

# Finder starts applications with a minimal PATH.  Add the places where
# mit-scheme and python3 are usually installed so the IDE can find them.
PATH=/opt/homebrew/bin:/usr/local/bin:/opt/local/bin:${HOME}/opt/mit-scheme/bin:${PATH}
export PATH

try_python() {
    [ -n "$1" ] || return 1
    command -v "$1" >/dev/null 2>&1 || return 1
    "$1" -c 'import tkinter' >/dev/null 2>&1 || return 1
    exec "$1" "${RESOURCES}/mit_scheme_ide.py" "$@"
}

try_python "${MIT_SCHEME_IDE_PYTHON}" "$@"
try_python /Library/Frameworks/Python.framework/Versions/Current/bin/python3 "$@"
try_python /opt/homebrew/bin/python3 "$@"
try_python /usr/local/bin/python3 "$@"
try_python /opt/local/bin/python3 "$@"
try_python python3 "$@"
try_python /usr/bin/python3 "$@"

MSG="MIT Scheme IDE needs Python 3 with Tk (the tkinter module) and could not find one.

Install Python from python.org, which includes Tk, or with Homebrew:
    brew install python-tk

Then open MIT Scheme IDE again."
if command -v osascript >/dev/null 2>&1; then
    osascript -e "display alert \"Python with Tk not found\" message \"${MSG}\" as critical" >/dev/null 2>&1
fi
echo "${MSG}" >&2
exit 1
EOF
chmod 755 "${CONTENTS}/MacOS/mit-scheme-ide"

# The bundle's own view of things: no stray editor files, no __pycache__.
find "${APP}" -name '.DS_Store' -delete
find "${APP}" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

echo "done: ${APP}"
