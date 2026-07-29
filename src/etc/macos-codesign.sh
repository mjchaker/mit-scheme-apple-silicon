#!/bin/sh
# Sign an installed MIT/GNU Scheme for distribution on macOS.
#
# usage: macos-codesign.sh IDENTITY [DIR]
#
#   IDENTITY  a codesigning identity, e.g. "Developer ID Application:
#             Your Name (TEAMID)".  Use "-" to sign ad hoc, which is
#             fine for local use but cannot be notarized.
#   DIR       a tree to walk; every Mach-O file under it is signed.
#             Defaults to the prefix of the running install if it can
#             be guessed.
#
# Pass the whole install prefix, not just bin.  An install has a second
# executable, lib/mit-scheme-ARCH-VERSION/macosx-starter, which arrives
# carrying only the linker's ad-hoc signature.  Notarization rejects
# the entire submission over it -- no Developer ID, no secure
# timestamp, no hardened runtime -- even though nothing in bin is
# wrong.
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
DIR=${2}

if [ -z "${IDENTITY}" ]; then
    echo "usage: ${0} IDENTITY [DIR]" >&2
    exit 1
fi

HERE=$(cd "$(dirname "${0}")" && pwd)
ENTITLEMENTS=${HERE}/macos-entitlements.plist

if [ ! -f "${ENTITLEMENTS}" ]; then
    echo "${0}: missing ${ENTITLEMENTS}" >&2
    exit 1
fi

if [ -z "${DIR}" ]; then
    SCHEME=$(command -v mit-scheme || true)
    if [ -z "${SCHEME}" ]; then
        echo "${0}: no DIR given and mit-scheme is not on PATH" >&2
        exit 1
    fi
    BINDIR=$(dirname "$(readlink -f "${SCHEME}" 2>/dev/null || echo "${SCHEME}")")
    DIR=$(dirname "${BINDIR}")
fi

# -type f does not follow symlinks, so the symlinked entry-point names
# are skipped and each real file is signed exactly once.  Match bare
# Mach-O rather than Mach-O executables: shared libraries need signing
# too, since the hardened runtime enables library validation and would
# otherwise refuse to load an FFI plugin at all.
find "${DIR}" -type f | while IFS= read -r f; do
    case $(file -b "${f}") in
        *Mach-O*)
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
find "${DIR}" -type f | while IFS= read -r f; do
    case $(file -b "${f}") in
        *Mach-O*)
            codesign --verify --strict --verbose=2 "${f}" 2>&1 | sed "s|^|  |"
            ;;
    esac
done
