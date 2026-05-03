# CARLA + Sionna 动态无线电地图项目概览

本文是当前项目的简明入口。旧版长篇代码梳理已归档到：

```text
docs/archive/CARLA_SIONNA_CODE_OVERVIEW_ZH_legacy.md
```

归档文件包含过时的随机采集、旧脚本和旧 `passed_target_count` 语义，除非
做历史追溯，不应作为当前实现依据。

## 当前目标

构建 CARLA + Sionna RT 动态无线电地图数据集。模型输入是静态无车无线电
地图和动态交通 BEV 网格，输出是同一交通状态下的动态无线电地图。

## 当前代码入口

核心代码位于：

```text
scripts/dynamic_radio_dataset/
```

推荐 CLI：

```bash
python3 scripts/drd.py --help
```

不要把新核心逻辑写入根目录 `scripts/` 的大脚本。

## 当前流水线

```text
RouteLibrary
  -> TrafficPlan bank
  -> preflight validation
  -> CARLA collection
  -> trajectory QA
  -> Sionna/RSS processing
  -> per-TX QA
  -> episode-TX index
```

轨迹 QA 失败的 episode 不能进入 Sionna/RSS。正式训练/主流程默认处理所有
TX；`target_tx_first` 只用于调试或 smoke。

## 当前基线配置

- 地图：`Town10HD_Opt`
- 场景：单个路口 `town10_junction_0189`
- TX：3 个固定路边 TX
- 时长：8 秒，10 fps，共 80 帧
- RSS：128 x 128
- 车辆：requested-total 3-6
- RF：每个通过轨迹 QA 的 episode 处理全部 TX

这是当前数据集配置，不是代码结构限制。生产代码必须继续通过 `scene_id`、
`tx_catalog`、RouteLibrary 元数据和 index 行支持未来多场景/多路口扩展。

## TrafficPlan 车辆角色

当前 `TrafficPlan` 是 role-aware：

```text
primary_controlled: 必需的路线控制 RF 关键车辆
auxiliary_controlled: 可选的路线控制辅助车辆
background_tm: CARLA Traffic Manager 控制的环境车辆
```

`vehicle_count` 表示 requested-total 实际车辆数。必需 controlled 车辆缺失是
硬失败；可选 auxiliary 或 background TM 数量不足应记录为 metadata，不应单独
作为硬失败。所有实际生成的车辆都必须写入 `frames/actor_states.jsonl`。

## 当前硬规则

- 不要恢复随机 `collect_one_episode()` 作为正式采集路径。
- 不要硬编码 route/plan/episode/attempt/TX id。
- 不要用降低阈值、伪造 RSS、跳过失败 TX 或重塑坏数组来让 smoke/pilot 通过。
- TX clearance、corridor hit/crossing、core visit、route mix、label crossing
  count 和旧 passed-target count 只作为诊断字段。
- CARLA/Sionna/RSS/QA 失败必须写明 `failure_code`、日志路径、stage metadata
  和汇总计数。

## 文档入口

- `.agent/HANDOFF.md`：当前交接状态，默认先读。
- `.agent/DATASET_PIPELINE.md`：数据格式、QA、采集和 CLI 合同。
- `.agent/PLANS.md`：当前里程碑和 pilot/main gate。
- `docs/README.md`：文档地图。
- `docs/archive/`：历史报告，仅在需要追溯时读取。
