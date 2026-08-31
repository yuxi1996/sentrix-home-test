# 记忆查询与图关系建立优化方案

本文对应两个可独立验收的代码优化方向：

1. 记忆查询架构：统一结构化、关键词、语义/向量、事件等检索通道，做硬过滤、RRF 融合、去重、证据归并、缓存和可解释追踪。
2. 图关系建立：把人物/图像关系假设变成稳定、可增量更新、可追溯的关系边；保留不确定性，发现冲突后不自动升级为事实。

代码已上传到：<https://github.com/yuxi1996/sentrix-home-test>

本次上传的优化层位于 `optimization/`，是可独立运行的适配层，不会直接修改 153 生产机上的 Sentrix。目标仓库是 PhotoBench 评测工程，不包含完整 Sentrix 后端，因此必须先在 153 做 shadow/A-B，再决定是否打开生产开关。

## 一、153 当前实现与本次改动

### 1. 当前记忆查询链路

153 当前 `backend/agent_runtime/tools.py` 的 `search_memories` 已包含：

- 语义拆槽：时间、地点、人物、事件、物体等槽位分别召回；
- 多路语义召回和事件成员重合；
- RRF 排序、候选断层截断、候选窗口和预览；
- `retrieval_trace`、`channel_trace`、证据账本和 ResultSet。

问题不在于“没有多路检索”，而在于多路逻辑分散在工具执行流程中：

- 查询计划、硬条件过滤和排序参数没有一个独立契约；
- 同一资产被不同通道召回时，证据和来源合并不够统一；
- 重复多轮查询缺少有范围的短 TTL 缓存；
- 候选分数、通道排名、证据来源没有统一输出格式，不利于定位 R/V/O/T/S/G/J 归因；
- 生产链路很难只替换检索融合层进行 A/B。

本次 `optimization/memory_query.py` 增加了 `MemoryQuery`、`HybridMemoryRetriever` 和 `RetrievalTrace`：

- `scope_id` 首先隔离，防止跨相册串证据；
- 时间、地点、人物、媒体类型在排序前过滤；
- 各通道使用加权 RRF，并保留每个通道的 rank/score/RRF；
- 以 `asset_id` 去重，但合并所有 `evidence_ids` 和 provenance；
- 排序以 `score` 后按 `asset_id` 稳定打破平局，便于复现；
- 使用有上限的 TTL LRU 缓存，不缓存跨 scope 结果；
- 输出候选数、过滤数、去重数、通道数量和 cache hit。

### 2. 当前图关系链路

153 当前已有：

- `backend/face_clustering.py`：质量感知、原型、视角和防 bridge 的人脸聚类；
- `backend/person_graph.py`：角色/关系候选清洗、对称关系归一、敏感内容过滤和图违规检查；
- `relationship_hypotheses`、`relationships` 等 SQLite 表；
- `apply_relationship_threshold`：按事件数/人物瞬间数做关系降级。

本次 `optimization/image_relation_graph.py` 在归一化之后增加持久化边界：

- 对称关系统一端点顺序，避免 A-B/B-A 产生两条边；
- 用 scope、subject、predicate、object 生成稳定 edge ID；
- 多个证据合并到同一边，保存 evidence/event/moment/source；
- 支持 `update()` 增量加入新照片，而无需全量重建；
- 以饱和函数聚合置信度，避免证据条数线性把分数推到 1；
- 默认至少 2 个独立证据且置信度达标才为 `confirmed`；
- 父亲/母亲/孩子/父母等不兼容边并存时标记 conflict，强制保持 `suggested`；
- 关系边始终带 revision，后续可映射到现有 `relationship_hypotheses` 表。

这部分不替代人脸模型，也不把模型猜测直接写成 confirmed fact。它只负责“证据聚合、状态机和一致性边界”。

## 二、代码使用方式

### 1. 记忆查询

```python
from optimization.memory_query import HybridMemoryRetriever, MemoryQuery

query = MemoryQuery(
    text="去年婚礼上的合影",
    user_goal="找出去年婚礼上兄弟们的合影",
    scope_id="album3",
    time_start="2025-01-01T00:00:00",
    time_end="2025-12-31T23:59:59",
    person_ids=("person-01",),
)

retriever = HybridMemoryRetriever(
    rrf_k=60,
    channel_weights={"structured": 1.25, "semantic_ann": 0.9},
    cache_size=128,
    cache_ttl_seconds=20,
)
results, trace = retriever.retrieve(
    query,
    {
        "structured": [{"asset_id": "asset-1", "rank": 1, "evidence_ids": ["obs-1"]}],
        "lexical": [{"asset_id": "asset-1", "rank": 3}],
        "semantic_ann": [{"asset_id": "asset-2", "rank": 1}],
    },
    k=18,
)
```

### 2. 图关系建立

```python
from optimization.image_relation_graph import RelationGraphBuilder, RelationObservation

builder = RelationGraphBuilder(min_evidence=2, confirm_confidence=0.78)
snapshot = builder.build([
    RelationObservation(
        scope_id="album3", subject_id="person-01", predicate="朋友",
        object_id="person-02", evidence_id="asset-100",
        event_id="event-7", confidence=0.81, source="person_moment",
    ),
])

# 新导入的照片只追加新证据；重复 evidence_id 不会重复计数。
snapshot = builder.update([
    RelationObservation(
        scope_id="album3", subject_id="person-02", predicate="朋友",
        object_id="person-01", evidence_id="asset-101",
        event_id="event-8", confidence=0.84, source="person_moment",
    ),
])
```

## 三、接入 153 的建议顺序

### 记忆查询接入点

在 153 的 `backend/agent_runtime/tools.py` 中保留现有槽位解析和各原始召回器，只把现有 `per_asset_ranks`、事件成员和结构化结果适配成通道字典，然后交给 `HybridMemoryRetriever`：

```text
semantic_slots
    ├─ structured / lexical / semantic_ann / visual_ann / event
    └─ HybridMemoryRetriever
          ├─ scope + hard filters
          ├─ weighted RRF
          ├─ asset dedupe + evidence merge
          └─ ResultSet / preview / retrieval_trace
```

建议先使用环境变量控制：

```bash
export SENTRIX_OPTIMIZED_RETRIEVAL=shadow
```

`shadow` 只计算新结果和新 trace，不改变模型可见结果；比较稳定后再使用 `on`。切换期间固定模型、scope、QA、并发、候选上限和时间范围，不能同时调整 RRF 参数和模型。

### 图关系接入点

建议在 `face_clustering`、事件绑定、`person_moments` 都完成后调用 builder，再把结果写入现有 `relationship_hypotheses`：

```text
face instances / person moments / event links
    └─ RelationObservation[]
          └─ RelationGraphBuilder
                ├─ canonical edge id
                ├─ evidence aggregation
                ├─ conflict detection
                └─ suggested / confirmed
```

第一阶段只写 `suggested` 和诊断快照；第二阶段才允许符合门槛的边进入 `confirmed` 查询投影。已有 `relationships` 表的 confirmed fact 不应被新推断直接覆盖，应通过 revision/supersedes 产生新版本。

## 四、测试指标和验收门槛

以下是建议的 A/B 指标。表中“门槛”是上线前建议值，不是本轮已经取得的成绩。

| 方向 | 指标 | 定义 | 建议门槛 |
|---|---|---|---:|
| 记忆查询 | Recall@18 | GT 资产被候选窗口召回的比例 | 不低于 baseline，目标 +5% |
| 记忆查询 | Precision@18 | 候选中 GT 资产比例 | 不下降超过 2 个百分点 |
| 记忆查询 | MRR | 第一个正确资产的倒数排名均值 | 目标 +5% |
| 记忆查询 | nDCG@18 | 处理相关性等级后的排序质量 | 目标 +5% |
| 记忆查询 | evidence coverage | 回答所需证据是否在候选/账本中 | ≥ 0.95 |
| 记忆查询 | duplicate rate | 候选重复资产或重复证据比例 | ≤ 0.01 |
| 记忆查询 | contradiction rate | hard filter 后仍出现冲突候选比例 | ≤ 0.02 |
| 记忆查询 | p50/p95 retrieval latency | 不含 Judge 的检索耗时 | p95 不增加 20% |
| 记忆查询 | cache hit ratio | 相同 scope/query/filter 的命中比例 | 多轮集 ≥ 0.20 |
| 图关系 | edge precision/recall/F1 | 关系边与人工 GT 的精确率、召回率、F1 | F1 目标 +10% |
| 图关系 | pairwise identity F1 | 人脸/身份聚类同人对的 pairwise F1 | 不低于 baseline |
| 图关系 | conflict rate | 关系边中被冲突标记的比例 | ≤ 0.05，且不漏报冲突 |
| 图关系 | duplicate edge rate | 同一无向关系的重复边比例 | 0 |
| 图关系 | evidence traceability | 每条 confirmed 边可回溯到证据的比例 | 1.00 |
| 图关系 | incremental update latency | 新增照片后只更新受影响边的耗时 | 相对全量下降 50% |
| 端到端 | exact/core/Judge | 完全正确、核心正确、Judge 质量 | exact/core 不下降 |
| 端到端 | JSON parse / within steps | 结构化输出和预算内完成率 | parse ≥ 0.99，steps = 1.00 |
| 端到端 | TTFT / tok/s / E2E | 模型首 token、吞吐、端到端耗时 | 分别记录，不用日志估算 |

记忆检索的 `Recall@K`、`Precision@K`、`MRR`、`nDCG@K` 可以直接用 `optimization/metrics.py`。图关系用 `graph_edge_metrics`；身份聚类还可以使用现有 153 `face_clustering.py` 的 pairwise 指标口径。

## 五、153 当前基线和截图参考值

### 153 实际跑过的基线

当前已核对到的 153 run：

- run：`20260831-141104-album3-14-gemma4-12b-it-reuse-0e0886`；
- 10/10 完成，Judge 10 条有效；
- answer quality：`1.0/2`；exact：`0.4`；core：`0.6`；
- retrieval precision/recall/F1：`0.710 / 0.880 / 0.786`；
- task decision：`0.800`；JSON parse：`0.929`；within steps：`1.000`；
- TTFT：`573.9 ms`；token/s：`17.5`；E2E mean：`66.5 s`；
- agent throughput：`0.128 QA/s`；loop：`6.4`；
- 工具 p50：`search_memories 11.6 s`、`inspect_photo 4.29 s`、`query_photo_people 10 ms`。

这是优化前参考基线，不代表优化层已经在 153 生效。特别是检索指标的 GT 口径、Judge 是否包含在耗时统计中，必须在正式 A/B 中保持一致。

### 用户截图中的模型参考值

以下数据来自用户提供的 Benchmark 截图，本仓库本轮没有重新下载或复核，作为横向参考，不可当作本项目的实测结果：

| Benchmark/指标 | Qwen2.5-Omni-7B | Qwen2.5-Omni-3B | gemma4:e2b | Qwen3-VL-4B-Thinking | MiniCPM-V-4.6 |
|---|---:|---:|---:|---:|---:|
| BFCL 函数调用完全正确 | 48.74% | 40.54% | 43.82% | 61.42% | 26.27% |
| BFCL 函数名匹配 | 92.48% | 79.41% | 89.92% | 93.36% | 64.69% |
| BFCL JSON 解析 | 99.36% | 99.60% | 98.16% | 100.00% | 92.56% |
| BIRD SQL 执行匹配 | 21.80% | 14.00% | 14.60% | 28.80% | 2.00% |
| BIRD JSON 解析 | 99.40% | 99.20% | 78.40% | 99.20% | 49.20% |
| MMStar 回答正确率 | 57.80% | 50.00% | 32.40% | 58.20% | 51.80% |
| BFCL V4 Memory 任务完成 | 5.38% | 6.45% | 10.75% | 23.23% | 8.82% |
| MCPMark Filesystem | 0/30 | 0/30 | 0/30 | 1/30 | 0/30 |
| MCPMark Postgres | 0/21 | 0/21 | 0/21 | 0/21 | 0/21 |
| EgoLife 官方 caption-回答 | 34.52% | 32.14% | 33.33% | 40.48% | 33.33% |

该截图只能用于说明 BFCL、BIRD、MMStar、Memory、MCPMark、EgoLife 的指标种类；优化前后对比仍应以同一台机器、同一数据、同一模型和同一评测脚本重跑为准。

## 六、数据集访问方式

### A. 153 本地数据（推荐优先使用）

153 上已核对存在的目录如下：

```text
/home/asus/Github/Sentrix-Home-Web/services/photobench/data
/home/asus/album3-max
/home/asus/photobench-e2e
/home/asus/benchmarks
/home/asus/sentrix-benchmarks/photobench-face-ab-20260823
/home/asus/data/photobench-face-ab-20260823
/home/asus/data/photobench-validation-1184
/home/asus/data/household-benchmark-max
```

其中：

- PhotoBench manifest/QA：`/home/asus/Github/Sentrix-Home-Web/services/photobench/data/`；
- 原始相册照片：`/home/asus/album3-max/photos/`；
- 已有 face A/B 数据库和运行产物：`/home/asus/sentrix-benchmarks/photobench-face-ab-20260823/`、`/home/asus/data/photobench-face-ab-20260823/`；
- 153 评测面板工程：`/home/asus/Github/Sentrix-Home-Web/services/photobench/`。

当前 QA 规模核对如下：

| 数据集/相册 | QA 集合或规模 |
|---|---|
| album3 | compact 10、full 38、behavior 6、paraphrase 88、OCR 8 |
| album3-14 | compact 10、behavior 6 |
| album3-kling | compact 10、full 38、behavior 6、paraphrase 88、OCR 8 |
| album3-max | 100；max-answerable 70、max-clarify 11、max-refuse 19 |
| album3-max-video10 | image-related 439、single-video 48、mixed 487 |

SSH 访问方式（凭据不要写入仓库、脚本或命令历史）：

```bash
ssh asus@192.168.0.153
cd /home/asus/Github/Sentrix-Home-Web/services/photobench
find data -maxdepth 2 -type f -name 'manifest.json' -o -name '*.jsonl'
```

评测面板和数据 API：

```bash
curl http://192.168.0.153:8771/api/manifests
curl 'http://192.168.0.153:8771/api/qa-dataset?album_id=album3&qa_set=compact'
```

Sentrix 服务的基地址为 `http://192.168.0.153:8091`；使用其健康检查和现有业务 API 时，先以当前服务 OpenAPI/路由为准，不要把数据库文件直接复制到生产服务。面板评测编排服务是 `http://192.168.0.153:8771`。账号密码通过安全渠道提供，本文和 Git 仓库不保存密码。

### B. 公开评测集

如需外部复现，可以按截图中的任务选择公开数据：

- BFCL / BFCL V4 Memory：工具调用、结构化参数和记忆任务完成；
- BIRD：自然语言到 SQL 及 SQL 执行匹配；
- MMStar：多模态理解与回答正确率；
- MCPMark：Filesystem、Postgres 工具任务完成率；
- EgoLife：caption-回答以及视频/音频多模态问答。

公开数据只用于模型/通用能力横向对比，不能替代 153 的相册检索 GT。下载时需按各数据集官方许可和官方划分保存，并把下载版本、commit、文件 SHA-256 写入 run manifest。

## 七、推荐 A/B 实验流程

1. 固定 153 的 Sentrix 服务版本、模型端点、GPU、scope、QA JSONL 和并发为 1。
2. 先跑当前实现，记录 run ID、manifest SHA-256、retrieval trace、E2E 和 Judge 分项。
3. 以 `SENTRIX_OPTIMIZED_RETRIEVAL=shadow` 跑同一批问题，验证新旧候选、证据集合和冲突数量。
4. 检查 shadow 差异：新结果不能跨 scope，不能丢掉已有 GT，不能增加 hard filter violation。
5. 只打开记忆查询 `on`，重复同一批 QA；再单独打开图关系增量构建，不能两个变量同时改变。
6. 冷缓存跑 3 次、热缓存跑 3 次；报告均值、标准差、p50、p95，并单列失败样本。
7. 对 `suggested`/`confirmed` 关系做人工抽样复核，至少覆盖高置信、低置信、冲突和新增照片四类。
8. 只有当 Recall、evidence coverage、冲突率、端到端 Judge 指标和延迟都通过门槛，才把新结果写入默认查询投影。

## 八、本地回归命令

优化层和原有 PhotoBench 单元测试均可运行。若环境未安装评测服务依赖，先安装：

```bash
python3 -m pip install -r requirements.txt
```

运行优化层和完整回归：

```bash
python3 -m unittest tests.test_optimizations -v
python3 -m unittest discover -s tests -v
```

完整回归需要 `httpx` 等评测依赖；如果只验证本次新增逻辑，第一条命令不依赖模型服务。

离线指标脚本的最小 JSONL 格式：

```json
{"query_id":"q1","relevant_asset_ids":["asset-1","asset-2"]}
```

```json
{"query_id":"q1","asset_ids":["asset-3","asset-1","asset-2"]}
```

执行：

```bash
python3 scripts/run_optimization_eval.py \
  --memory-truth /path/to/memory-truth.jsonl \
  --memory-pred /path/to/memory-pred.jsonl \
  --k 18
```

原始照片、视频、153 数据库、历史结果和任何 API key 没有上传到 GitHub；仓库只包含可审计代码、manifest/QA 定义和复现实验说明。
