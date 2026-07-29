#!/bin/sh
# Package an installed MIT/GNU Scheme as a signed, notarized, stapled
# disk image.
#
# usage: macos-make-dmg.sh [-p PROFILE] [-t] IDENTITY PREFIX [DMG]
#
#   -p PROFILE  a notarytool keychain profile, as created by
#               "xcrun notarytool store-credentials PROFILE".  If given,
#               the image is submitted to Apple and stapled.  If
#               omitted, the image is only built and signed.
#   -t          also write NAME.tar.gz beside the image, from the same
#               staging area, so both artifacts hold the same bits.
#               Written only once everything else has succeeded, so a
#               failed notarization does not leave a tarball that looks
#               shippable.
#   IDENTITY    a codesigning identity, e.g. "Developer ID Application:
#               Your Name (TEAMID)".  Notarization requires a Developer
#               ID; "Apple Development" certificates are rejected.
#   PREFIX      an install tree, i.e. the --prefix given to configure.
#   DMG         output path; defaults to ./NAME.dmg, where NAME is
#               derived from the installed executable, e.g.
#               mit-scheme-12.1-aarch64le-darwin.
#
# The tree is staged and modified before packaging, so PREFIX itself is
# left untouched.  Two things happen in the staging area:
#
#   - bin/mit-scheme is replaced by a wrapper that derives the library
#     path from its own location.  "make install" bakes the library
#     path into the executable, so without this the copy the user drags
#     out of the image fails with "searched for file all.com in these
#     directories: <your build machine's path>".
#
#   - every Mach-O in the tree is signed with the hardened runtime and
#     the entitlement in macos-entitlements.plist, via
#     macos-codesign.sh.  Note that includes
#     lib/mit-scheme-ARCH-VERSION/macosx-starter, not just bin:
#     notarization rejects the whole submission over any unsigned
#     executable anywhere in the image.
#
# Note that only the image is stapled.  "stapler staple" accepts app
# bundles, installer packages and disk images; a bare Mach-O executable
# has nowhere to put the ticket.  The executables inside are covered by
# the notarization record and validate online, so a user who copies
# them out while offline may still see a Gatekeeper prompt.

set -e

USAGE="usage: ${0} [-p PROFILE] [-t] IDENTITY PREFIX [DMG]"

PROFILE=
TARBALL=

while getopts p:t opt; do
    case ${opt} in
        p) PROFILE=${OPTARG} ;;
        t) TARBALL=yes ;;
        *) echo "${USAGE}" >&2
           exit 1 ;;
    esac
done
shift $((OPTIND - 1))

IDENTITY=${1}
PREFIX=${2}
DMG=${3}

if [ -z "${IDENTITY}" ] || [ -z "${PREFIX}" ]; then
    echo "${USAGE}" >&2
    exit 1
fi

if [ ! -d "${PREFIX}/bin" ]; then
    echo "${0}: ${PREFIX} is not an install tree (no bin/)" >&2
    exit 1
fi

if [ -n "${PROFILE}" ] && [ "${IDENTITY}" = "-" ]; then
    echo "${0}: cannot notarize an ad-hoc signature; use a Developer ID" >&2
    exit 1
fi

HERE=$(cd "$(dirname "${0}")" && pwd)

# The real executable is the one Mach-O file in bindir; the installed
# names are symlinks to it.  Its name carries the architecture and
# version we need for both the wrapper and the image name.
EXE=
for f in "${PREFIX}"/bin/*; do
    [ -f "${f}" ] || continue
    [ -L "${f}" ] && continue
    case $(file -b "${f}") in
        *Mach-O*executable*) EXE=$(basename "${f}") ;;
    esac
done

if [ -z "${EXE}" ]; then
    echo "${0}: found no Mach-O executable in ${PREFIX}/bin" >&2
    exit 1
fi

ARCH=$(echo "${EXE}" | sed 's|^mit-scheme-||; s|-[0-9][0-9.]*$||')
VERSION=$(echo "${EXE}" | sed 's|.*-||')
NAME=mit-scheme-${VERSION}-${ARCH}-darwin
: "${DMG:=${NAME}.dmg}"

STAGE=$(mktemp -d "${TMPDIR:-/tmp}/mit-scheme-dmg.XXXXXX")
NOTARY_LOG=$(mktemp "${TMPDIR:-/tmp}/mit-scheme-notary.XXXXXX")
trap 'rm -rf "${STAGE}" "${NOTARY_LOG}"' EXIT INT TERM

ROOT=${STAGE}/${NAME}

echo "staging ${PREFIX} as ${NAME}"
ditto "${PREFIX}" "${ROOT}"
find "${STAGE}" -name .DS_Store -delete

# Make it relocatable.  Leave the real executable under its versioned
# name; mit-scheme becomes the wrapper and the rest become symlinks to
# the wrapper, so every entry point gets the derived library path.
#
# Remove mit-scheme before writing it.  As installed it is a symlink to
# the real executable, and a redirect would follow that symlink and
# overwrite the executable with the wrapper.
rm -f "${ROOT}/bin/mit-scheme"
cat > "${ROOT}/bin/mit-scheme" <<EOF
#!/bin/sh
HERE=\$(cd "\$(dirname "\${0}")" && pwd)
AUXDIR=\$(dirname "\${HERE}")/lib/mit-scheme-${ARCH}-${VERSION}
if [ -z "\${MITSCHEME_LIBRARY_PATH}" ]; then
    MITSCHEME_LIBRARY_PATH=\${AUXDIR}; export MITSCHEME_LIBRARY_PATH
fi
exec "\${HERE}/${EXE}" "\${@}"
EOF
chmod 755 "${ROOT}/bin/mit-scheme"

for link in scheme mit-scheme-${ARCH}; do
    rm -f "${ROOT}/bin/${link}"
    ln -s mit-scheme "${ROOT}/bin/${link}"
done

# Guard against the wrapper having landed on top of the executable.
case $(file -b "${ROOT}/bin/${EXE}") in
    *Mach-O*executable*) ;;
    *) echo "${0}: staged ${EXE} is not a Mach-O executable" >&2
       exit 1 ;;
esac

if [ ! -d "${ROOT}/lib/mit-scheme-${ARCH}-${VERSION}" ]; then
    echo "${0}: warning: no lib/mit-scheme-${ARCH}-${VERSION};" \
         "the wrapper's library path will not resolve" >&2
fi

echo
"${HERE}/macos-codesign.sh" "${IDENTITY}" "${ROOT}"

echo
echo "building ${DMG}"
rm -f "${DMG}"
hdiutil create -quiet -srcfolder "${STAGE}" -volname "${NAME}" \
        -format UDZO -fs HFS+ "${DMG}"

echo "signing ${DMG}"
codesign --force --timestamp --sign "${IDENTITY}" "${DMG}"

# Package the staging area as a tarball too.  Code signatures live
# inside the Mach-O, so they survive tar; the binaries here are the
# same ones the image carries, and so are covered by the same
# notarization.  COPYFILE_DISABLE and --no-mac-metadata keep the
# AppleDouble "._" companions out of the archive.
make_tarball() {
    [ -n "${TARBALL}" ] || return 0
    TGZ=$(cd "$(dirname "${DMG}")" && pwd)/${NAME}.tar.gz
    echo
    echo "building ${TGZ}"
    rm -f "${TGZ}"
    ( cd "${STAGE}" &&
      COPYFILE_DISABLE=1 tar --no-mac-metadata -czf "${TGZ}" "${NAME}" )
}

if [ -z "${PROFILE}" ]; then
    make_tarball
    echo
    echo "${DMG} is signed but not notarized.  To notarize:"
    echo "  ${0} -p PROFILE '${IDENTITY}' '${PREFIX}' '${DMG}'"
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

make_tarball
