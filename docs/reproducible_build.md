# Reproducible Build Verification

`aionetx` release builds use `SOURCE_DATE_EPOCH` derived from the exact tagged
commit timestamp. That keeps wheel and sdist timestamps deterministic across
rebuilds of the same release ref.

## Scope

- Deterministic timestamp control is applied in the release workflow build jobs.
- The reference value is `git log -1 --format=%ct <ref>`.
- Reproducibility means the same source tree and build inputs produce identical artifacts.

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
