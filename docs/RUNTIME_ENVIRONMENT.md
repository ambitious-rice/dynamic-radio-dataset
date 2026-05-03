# Runtime Environment

Last updated: 2026-05-03 CST

## Python Environments

Use separate runtimes:

```text
CARLA/main: /share1/fzj/miniconda3/envs/carla0915/bin/python
Sionna/RF:  /share1/fzj/miniconda3/envs/sionna019/bin/python
```

The formal release runner sets `PYTHONPATH=scripts` and runs CARLA collection,
QA, finalize, verification, and pruning under the CARLA/main env. RF episode
workers run under the Sionna env with `PYTHONNOUSERSITE=1`.

`networkx==3.1` must be installed inside the isolated `carla0915` env. If it is
only available in user site-packages, route tracing can silently fall back from
CARLA `GlobalRoutePlanner` behavior and mismatch the RouteLibrary/TrafficPlan
catalog.

## Sionna GPU Runtime

GPU RF uses:

```text
CUDA_DEVICE_ORDER=PCI_BUS_ID
CUDA_VISIBLE_DEVICES=<worker GPU id>
DRD_SIONNA_RUNTIME_BASE=/dev/shm/fzj_drd_sionna_rf
```

Each RF worker slot gets a stable isolated runtime home:

```text
/dev/shm/fzj_drd_sionna_rf/worker_00_gpu_0
/dev/shm/fzj_drd_sionna_rf/worker_01_gpu_1
```

Within that home, `HOME`, `XDG_CACHE_HOME`, `DRJIT_CACHE_DIR`, and
`CUDA_CACHE_PATH` are set so Dr.Jit/OptiX/CUDA caches do not collide between
workers. If `/dev/shm` is unavailable, the code falls back to
`/share1/fzj/.drd_runtime/sionna_rf`.

## Shell/Tmux Notes

Root `/tmp` may be unusable when `/` is full. Use:

```bash
TMUX_TMPDIR=/share1/fzj/tmux_tmp tmux ...
```

Check CARLA before starting a new simulator:

```bash
ps -ef | rg "CarlaUE4|carla-rpc-port=2000"
```

Start CARLA only when collection needs it:

```bash
./CarlaUE4.sh -RenderOffScreen -nosound -quality-level=Low -carla-rpc-port=2000
```

Do not delete `DRD_SIONNA_RUNTIME_BASE` while RF workers are active.

