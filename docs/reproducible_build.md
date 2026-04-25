# Reproducible Build Verification

`aionetx` release builds use `SOURCE_DATE_EPOCH` derived from the exact tagged
commit timestamp. That keeps wheel and sdist timestamps deterministic across
rebuilds of the same release ref.

## Scope

- Deterministic timestamp control is applied in the release workflow build jobs.
- The reference value is `git log -1 --format=%ct <ref>`.
- Reproducibility means the same source tree and build inputs produce identical artifacts.

## Release asset verification

For an official release, verify that the release identity and artifacts
all point to the same version:

1. Check that the GitHub Release tag, PyPI version, `pyproject.toml`
   version, and `CHANGELOG.md` entry agree.
2. Confirm that the tag is in the `MarcusKorinth/aionetx` repository.
3. Download the wheel, source distribution, and `aionetx-sbom.spdx.json`
   from the GitHub Release or the package index.
4. Verify GitHub artifact attestations for each release asset:

```bash
gh attestation verify aionetx-*.whl --repo MarcusKorinth/aionetx
gh attestation verify aionetx-*.tar.gz --repo MarcusKorinth/aionetx
gh attestation verify aionetx-sbom.spdx.json --repo MarcusKorinth/aionetx
```

The expected release identity is:

- source repository: `MarcusKorinth/aionetx`
- release workflow: `.github/workflows/release.yml`
- publishing mechanism: GitHub Actions OIDC trusted publishing

Reject artifacts whose attestations point to a different repository,
workflow, or release version. The reproducible build recipe below is a
secondary check that independent rebuilds produce the same wheel and
source distribution for the tagged ref.

## Third-party verification recipe

1. Check out the exact release ref.

```bash
git clone https://github.com/<owner>/aionetx.git
cd aionetx
git checkout vX.Y.Z
```

2. Create a clean environment and install the build frontend.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip build
```

3. Export the deterministic timestamp used by the release workflow.

```bash
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct HEAD)"
```

4. Build twice from clean output directories.

```bash
rm -rf build dist
python -m build
mv dist dist-first

rm -rf build dist
python -m build
mv dist dist-second
```

5. Compare artifact hashes.

```bash
sha256sum dist-first/* dist-second/*
```

Matching hashes for the wheel and sdist indicate a reproducible rebuild for that ref.

## Notes

- Rebuild the exact tagged commit, not a moving branch tip.
- Keep tool versions stable while comparing outputs.
- If a deeper diff is needed, compare wheel contents directly or use `diffoscope`.
