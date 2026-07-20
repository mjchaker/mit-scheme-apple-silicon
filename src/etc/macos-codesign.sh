#!/bin/sh
# Sign an installed MIT/GNU Scheme for distribution on macOS.
#
# usage: macos-codesign.sh IDENTITY [BINDIR]
#
#   IDENTITY  a codesigning identity, e.g. "Developer ID Application:
#             Your Name (TEAMID)".  Use "-" to sign ad hoc, which is
#             fine for local use but cannot be notarized.
#   BINDIR    directory holding the scheme executable; defaults to the
#             bindir of the running install if it can be guessed.
#
# The hardened runtime (--options runtime) is required for
# notarization, and it in turn requires the entitlement in
# macos-entitlements.plist, because compiled Scheme executes out of the
# heap.  See that file, and PORTING-APPLE-SILICON.md, for why
# allow-jit is not the right entitlement here.
#
# After signing, notarizing is the usual two steps (they contact
# Apple, so they are deliberately left out of this script):
#
#   xcrun notarytool submit mit-scheme.zip --keychain-profile PROFILE --wait
#   xcrun stapler staple <the .app or installer>
#
# A bare Unix-style tree of executables cannot be stapled; staple the
# disk image or installer you ship instead.

set -e

IDENTITY=${1}
BINDIR=${2}

if [ -z "${IDENTITY}" ]; then
    echo "usage: ${0} IDENTITY [BINDIR]" >&2
    exit 1
fi

HERE=$(cd "$(dirname "${0}")" && pwd)
ENTITLEMENTS=${HERE}/macos-entitlements.plist

if [ ! -f "${ENTITLEMENTS}" ]; then
    echo "${0}: missing ${ENTITLEMENTS}" >&2
    exit 1
fi

if [ -z "${BINDIR}" ]; then
    SCHEME=$(command -v mit-scheme || true)
    if [ -z "${SCHEME}" ]; then
        echo "${0}: no BINDIR given and mit-scheme is not on PATH" >&2
        exit 1
    fi
    BINDIR=$(dirname "$(readlink -f "${SCHEME}" 2>/dev/null || echo "${SCHEME}")")
fi

# Sign the real executables; the installed names are symlinks to them,
# and codesign follows those to the same file.
for f in "${BINDIR}"/*; do
    [ -f "${f}" ] || continue           # skip the symlinks
    case $(file -b "${f}") in
        *Mach-O*executable*)
            echo "signing ${f}"
            codesign --force --timestamp \
                     --sign "${IDENTITY}" \
                     --options runtime \
                     --entitlements "${ENTITLEMENTS}" \
                     "${f}"
            ;;
    esac
done

echo
echo "verifying:"
for f in "${BINDIR}"/*; do
    [ -f "${f}" ] || continue
    case $(file -b "${f}") in
        *Mach-O*executable*)
            codesign --verify --strict --verbose=2 "${f}" 2>&1 | sed "s|^|  |"
            ;;
    esac
done
