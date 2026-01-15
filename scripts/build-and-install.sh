#!/usr/bin/env bash
set -euo pipefail

# Fast helper to build the generated PKGBUILD in a temp dir and install the result.

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pkg_src_dir="${root_dir}/package"

workroot="$HOME/.cache/tuxedo-build"
rm -rf "$workroot"
mkdir -p "$workroot"
tmpdir=$(mktemp -d "${workroot%/}/build-XXXXXX")

cp "${pkg_src_dir}/PKGBUILD" "${pkg_src_dir}/config" "${pkg_src_dir}/patches.tar.gz" "${tmpdir}/"

pushd "${tmpdir}" >/dev/null
makepkg -C -f -s -i --noconfirm ${MAKEPKG_FLAGS:-}

popd >/dev/null
