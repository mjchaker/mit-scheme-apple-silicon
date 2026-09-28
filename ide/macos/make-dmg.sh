#!/bin/sh
# Package the MIT Scheme IDE as a macOS disk image.
#
# usage: make-dmg.sh [-s IDENTITY] [-p PROFILE] [-t TOOLDIR] [-o DMG]
#
#   -s IDENTITY  a codesigning identity, e.g. "Developer ID Application:
#                Your Name (TEAMID)".  The app bundle and the image are
#                signed with it.  Without -s the image is unsigned, and
#                a Mac that downloads it will refuse to open the app
#                until the user chooses Open in the Finder's context
#                menu (or removes the quarantine attribute).
#   -p PROFILE   a notarytool keychain profile, as created by "xcrun
#                notarytool store-credentials PROFILE".  If given (which
#                requires -s), the image is submitted to Apple and
#                stapled.
#   -t TOOLDIR   for builds on a machine that is not a Mac: a directory
#                holding the "hfsplus" and "dmg" programs from
#                libdmg-hfsplus (https://github.com/planetbeing/libdmg-hfsplus).
#                Default: whatever is on PATH.
#   -o DMG       output path; default ./MIT-Scheme-IDE-VERSION.dmg.
#
# On macOS the image is built with hdiutil and carries an Applications
# symlink for drag-and-drop installation.  Elsewhere it is built with
# mkfs.hfsplus and libdmg-hfsplus into the same UDIF (zlib-compressed)
# format, minus the symlink, which libdmg-hfsplus cannot create.  Both
# mount on any Mac.
#
# The bundle itself comes from make-app.sh; see there for what it
# contains and how it finds Python and mit-scheme on the user's machine.
# This script mirrors src/etc/macos-make-dmg.sh, which packages the
# interpreter.

set -e

USAGE="usage: ${0} [-s IDENTITY] [-p PROFILE] [-t TOOLDIR] [-o DMG]"

IDENTITY=
PROFILE=
TOOLDIR=
DMG=

while getopts s:p:t:o: opt; do
    case ${opt} in
        s) IDENTITY=${OPTARG} ;;
        p) PROFILE=${OPTARG} ;;
        t) TOOLDIR=${OPTARG} ;;
        o) DMG=${OPTARG} ;;
        *) echo "${USAGE}" >&2
           exit 1 ;;
    esac
done
shift $((OPTIND - 1))
if [ $# -ne 0 ]; then
    echo "${USAGE}" >&2
    exit 1
fi

if [ -n "${PROFILE}" ] && [ -z "${IDENTITY}" ]; then
    echo "${0}: notarization (-p) needs a signing identity (-s)" >&2
    exit 1
fi

HERE=$(cd "$(dirname "${0}")" && pwd)
IDE=$(dirname "${HERE}")
VERSION=$(sed -n 's/^VERSION = "\([^"]*\)"/\1/p' "${IDE}/mit_scheme_ide.py" | head -1)
: "${VERSION:=0.0}"
: "${DMG:=MIT-Scheme-IDE-${VERSION}.dmg}"
VOLNAME="MIT Scheme IDE"
APPNAME="MIT Scheme IDE.app"

STAGE=$(mktemp -d "${TMPDIR:-/tmp}/mit-scheme-ide-dmg.XXXXXX")
NOTARY_LOG=$(mktemp "${TMPDIR:-/tmp}/mit-scheme-ide-notary.XXXXXX")
trap 'rm -rf "${STAGE}" "${NOTARY_LOG}"' EXIT INT TERM

"${HERE}/make-app.sh" -o "${STAGE}"
APP=${STAGE}/${APPNAME}

rm -f "${DMG}"

if [ "$(uname -s)" = "Darwin" ]; then
    ln -s /Applications "${STAGE}/Applications"

    if [ -n "${IDENTITY}" ]; then
        echo
        echo "signing ${APPNAME}"
        codesign --force --deep --options runtime --timestamp \
                 --sign "${IDENTITY}" "${APP}"
        codesign --verify --strict --verbose=2 "${APP}" 2>&1 | sed "s|^|  |"
    fi

    echo
    echo "building ${DMG}"
    hdiutil create -quiet -srcfolder "${STAGE}" -volname "${VOLNAME}" \
            -format UDZO -fs HFS+ "${DMG}"

    if [ -z "${IDENTITY}" ]; then
        echo
        echo "${DMG} is unsigned.  To sign (and notarize) it:"
        echo "  ${0} -s 'Developer ID Application: NAME (TEAMID)' [-p PROFILE] -o '${DMG}'"
        exit 0
    fi

    echo "signing ${DMG}"
    codesign --force --timestamp --sign "${IDENTITY}" "${DMG}"

    if [ -z "${PROFILE}" ]; then
        echo
        echo "${DMG} is signed but not notarized.  To notarize:"
        echo "  ${0} -s '${IDENTITY}' -p PROFILE -o '${DMG}'"
        exit 0
    fi

    echo
    echo "submitting ${DMG} to Apple"
    set +e
    xcrun notarytool submit "${DMG}" --keychain-profile "${PROFILE}" --wait \
          > "${NOTARY_LOG}" 2>&1
    set -e
    cat "${NOTARY_LOG}"
    if ! grep -q "status: Accepted" "${NOTARY_LOG}"; then
        ID=$(sed -n 's|^ *id: *||p' "${NOTARY_LOG}" | head -1)
        echo >&2
        echo "${0}: notarization did not succeed" >&2
        if [ -n "${ID}" ]; then
            echo "for the reason:" >&2
            echo "  xcrun notarytool log ${ID} --keychain-profile ${PROFILE}" >&2
        fi
        exit 1
    fi

    echo
    echo "stapling ${DMG}"
    xcrun stapler staple "${DMG}"
    echo
    echo "verifying:"
    xcrun stapler validate "${DMG}" 2>&1 | sed "s|^|  |"
    spctl --assess --type open --context context:primary-signature \
          --verbose=2 "${DMG}" 2>&1 | sed "s|^|  |"
    exit 0
fi

# ---------------------------------------------------------------------
# Not a Mac: mkfs.hfsplus + libdmg-hfsplus.

if [ -n "${IDENTITY}" ]; then
    echo "${0}: signing needs macOS (codesign); build unsigned here, or on a Mac" >&2
    exit 1
fi

# TOOLDIR may hold the programs directly, or be a libdmg-hfsplus build
# directory, where they sit in hfs/ and dmg/.
find_tool() {
    for d in "${TOOLDIR}" "${TOOLDIR}/hfs" "${TOOLDIR}/dmg"; do
        if [ -n "${TOOLDIR}" ] && [ -f "${d}/${1}" ] && [ -x "${d}/${1}" ]; then
            echo "${d}/${1}"
            return
        fi
    done
    command -v "${1}" || true
}

HFSPLUS=$(find_tool hfsplus)
DMGTOOL=$(find_tool dmg)
MKFS=$(command -v mkfs.hfsplus || true)
for pair in "hfsplus:${HFSPLUS}" "dmg:${DMGTOOL}" "mkfs.hfsplus:${MKFS}"; do
    if [ -z "${pair#*:}" ]; then
        echo "${0}: need ${pair%%:*}; on Debian/Ubuntu: apt install hfsprogs," \
             "and build libdmg-hfsplus (pass its build directory with -t)" >&2
        exit 1
    fi
done

# Size the volume: the bundle plus room for the catalog and allocation
# files, rounded up to whole MiB, at least 4 MiB.
KB=$(du -sk "${STAGE}" | cut -f1)
MB=$(( (KB + 1024 * 3) / 1024 ))
[ "${MB}" -ge 4 ] || MB=4

IMG=${STAGE}.hfs
echo
echo "building ${DMG} (${MB} MiB HFS+ volume)"
dd if=/dev/zero of="${IMG}" bs=1048576 count="${MB}" status=none
"${MKFS}" -v "${VOLNAME}" "${IMG}" > /dev/null

"${HFSPLUS}" "${IMG}" mkdir "/${APPNAME}"
"${HFSPLUS}" "${IMG}" addall "${APP}" "/${APPNAME}" > /dev/null
# addall does not carry permission bits over; the launcher must be executable.
"${HFSPLUS}" "${IMG}" chmod 755 "/${APPNAME}/Contents/MacOS/mit-scheme-ide"

echo
echo "contents of ${VOLNAME}:/${APPNAME}/Contents"
"${HFSPLUS}" "${IMG}" ls "/${APPNAME}/Contents" | sed 's|^|  |'

"${DMGTOOL}" dmg "${IMG}" "${DMG}" > /dev/null
rm -f "${IMG}"

echo
echo "${DMG} is unsigned (built without macOS)."
echo "To sign and notarize, run this script on a Mac with -s and -p."
