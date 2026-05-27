# MultiScene20 CARLA State Archive

This branch contains split parts for `MultiScene20.tar.gz`, a CARLA-only state export for reusing trajectories and TX placement on another server.

Reconstruct:

```bash
cat MultiScene20.tar.gz.part-* > MultiScene20.tar.gz
sha256sum -c MultiScene20.tar.gz.sha256
PYTHONPATH=scripts python3 scripts/drd.py import-carla-state --source MultiScene20.tar.gz --destination-root .
```

Archive SHA256:

```text
f4614ad64c73dd37184cd1d53ecc62249cf98ed521535301a6d7b7d2d3abf595  MultiScene20.tar.gz
```

Archive contents: 33542 whitelisted CARLA-state source files plus one archive manifest file.
Do not treat this as RF output; rerun `prepare-rf-cache` and `process-multi-scene-rf` on the target server.
