# Vendored browser dependencies

## esptool-js

`assets/vendor/esptool-js-bundle.js` is based on `esptool-js` 0.5.6 from
Espressif's [upstream repository](https://github.com/espressif/esptool-js),
licensed under Apache-2.0. The npm tarball is pinned by the SHA-512 integrity
value and the upstream/local bundle SHA-256 values in
`assets/vendor/esptool-js-bundle.lock.json`. The bundle includes pako 2.1.0,
which is licensed under MIT and Zlib as identified by its embedded license
banner.

The upstream bundle is byte-identical to the source imported in commit
`4d448029acc187f24d6fa17fa08e692d963e278e`. The checked-in bundle also carries
the repository's WebSerial lock-release patch from commit
`478a89559cad29392e1363689bc32331b6ae4085`.

To reproduce and verify the upstream bundle:

```bash
bundle_tmp=$(mktemp -d)
npm pack --ignore-scripts esptool-js@0.5.6 --pack-destination "$bundle_tmp"
tar -xOf "$bundle_tmp/esptool-js-0.5.6.tgz" package/bundle.js \
  > "$bundle_tmp/esptool-js-0.5.6.bundle.js"
sha256sum "$bundle_tmp/esptool-js-0.5.6.bundle.js"
```

The result must match `upstream_bundle_sha256`. Review the patch in the recorded
commit, apply it to that upstream bundle, and require the resulting hash to
match `vendored_bundle_sha256` before updating the checked-in asset.

Dependency updates must provide a new immutable npm version, license evidence,
upstream and final bundle hashes, and a reviewed patch/diff. Run the full Node
and Python test commands from the README before committing the lockfile and
bundle together.
