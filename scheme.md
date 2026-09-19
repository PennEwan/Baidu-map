# 后续工作方案

依据 PR #26（POI 距离计算性能审计）与 PR #29（步行 RouteMatrix PoC）。两轮结论均为 **NO：不正式合入优化代码 / Matrix adapter**。下文只规划尚未完成、且不得绕开现有契约的工作。

相关资料：

- [POI距离计算性能测试报告](backend/docs/performance/poi-distance/POI距离计算性能测试报告.md)
- [POI距离计算优化建议](backend/docs/performance/poi-distance/POI距离计算优化建议.md)
- [POI批量距离矩阵实验报告](backend/docs/performance/poi-distance/POI批量距离矩阵实验报告.md)
- [POI批量距离矩阵接入建议](backend/docs/performance/poi-distance/POI批量距离矩阵接入建议.md)
- [交付报告书（2026-09-17）](backend/docs/performance/poi-distance/交付报告_20260917.md)

## 0. 已冻结的决策

| 事项 | 状态 | 约束 |
| --- | --- | --- |
| 四 worker 并发（方案 B） | 不合入 | 共享 `attempt_lock` 使网络并发仍为 1；最大 1.000821×，视为噪声 |
| 几何投影复用（方案 C） | 仅本地实验 | 完整设施 mock QPS=3 对照几乎无 wall-clock 收益 |
| 步行矩阵替代单点 walking | 不合入 | 无 steps/path/≤50m 端点证据；补发全部单点后 OD 翻倍且更慢 |
| 现有去重 / OD cache / 1200m 粗筛 / 早停 | 保留 | 不得再计为本轮新增收益，不得为跑分改采样量、候选上限、1000m 规则 |
| OSM 离线路径 | 不纳入本线 | 不调用百度 walking，不能伪装成等价提速 |
| B1 `poi_collect` | 未接入 `analyze_facilities` | 不得把采集目录当成已批量计算 POI 路线 |

验收门槛（任何正式优化 PR 必须同时满足）：相同输入下 POI/类别/去重/可达及 unknown/错误/设施统计一致；无新增真实限流；全局预算与取消回收正确；N=50/100 的 wall-clock 改善超过测量噪声。不得用 CPU 控制时间或纯 HTTP 数下降替代完整业务耗时。

## 1. 阶段一：账户、预算与真实基线

优先级最高。当前所有数字都来自 MockTransport，不能称为真实网络实测。

1. 核验控制台中步行路线（`directionlite/v1/walking`）与批量算路（`/routematrix/v2/walking`）的服务权限、每秒 OD 额度、日额度。`ANALYSIS_QPS` 源码默认 `None`，真实任务必须显式配置；模拟 QPS=3 / 假设 50 OD/秒都不是生产值。
2. 审核正式分析进程与独立 B1 采集器是否共用同一账号。单进程合规不能证明多进程合规；需要账户级统一限流方案，而不是各写各的 gate。
3. 在明确测试预算后，用与实验相同的中心 `(121.513926, 31.313077)` 和固定 POI 做少量真实 `baidu_walking` 对照。分阶段记账：POI 搜索、walking、排队/冷却、重试、geometry、报告。不记录凭据或带密钥 URL。
4. 公布均值 / min / max、请求 / 失败 / 重试、实际并发。至少三轮。据此约定“合格改善幅度”，不要预先虚构阈值。

出口条件：知道真实 QPS/日预算；有分阶段耗时账本；能指出主瓶颈是 walking、搜索、等待还是重试。

> 2026-09-16 状态：阶段一代码与测试已落地（`backend/app/stage_ledger.py`、`backend/tools/stage_baseline.py`、`backend/tests/test_stage_ledger.py`），真实步行基线 v2 完成（N=10、3 轮、pacing 上限 3，30 次请求、0 失败、0 重试，主瓶颈 walking，分阶段账本含 pacing/walking）。2026-09-16 核验的 walking 控制台值（3 次/秒、5000 次/日）属**旧 AK**；2026-09-17 换新 AK 后 [console-inventory.json](backend/docs/performance/poi-distance/stage-1/console-inventory.json) 已清空并标记未核验（新 AK 实测约 20 req/s 内未限流，见 [QPS 试测报告](backend/docs/performance/poi-distance/qps-probe/探测报告_20260917.md)）。矩阵（routematrix）权限与 OD 额度仍未核验，属阶段三门禁。live facilities 分阶段账本仍为 mock。详见 [阶段一报告](backend/docs/performance/poi-distance/stage-1/阶段一_账户预算与真实基线报告.md)。

## 2. 阶段二：跨任务安全缓存（低配额优先）

低配额下矩阵收益弱（约 8%–10%），缓存比并发更可能有效。

1. 先统计真实场景重复 OD 命中率。没有命中空间则不做跨任务 cache。
2. cache 键必须覆盖：provider、API 版本、metric、坐标系、起终点、UID。不同 UID 即使坐标相同也不得折叠（walking 携带 `destination_uid`，入口可能不同）。
3. 定义 TTL；无结果、瞬时失败、403/参数错误不得当成功缓存。暖缓存不得与冷基线对比制造收益。
4. 不同任务、等时圈与设施 provider 的 metric/UID 语义不同，不能只凭经纬度复用。

出口条件：有命中率数据；有键规范与失败不缓存策略；有回归证明不会把 unknown 写成不可达。

> 2026-09-16 准备完成：键规范与失败不缓存策略沿用 `od_cache_key` / `observation_cacheable`；新增零请求命中率分析器 `backend/tools/od_cache_review.py`（输出 `od-cache-review-v1`，拒绝覆盖、无凭据、`networkRequests: 0`）与 6 项测试，并用合成示例给出统计口径。第一步已执行：新增导出器 `backend/tools/od_export.py`，将唯一的已授权本地真实产物（固定边界参考集 48 条真实耗时）导出并复核——33 唯一键、15 重复（相邻线段共享端点，非生产命中率），因冻结文件缺端点证据全部不可缓存。随后补齐逐 OD 记录能力：`StageLedger.record_od`，`LimitedProvider` 与设施 `route()` 在挂载账本时记录（含 UID 与端点证据），`stage_baseline --observations-output/--task-id` 可落 `od-observations-v1`，mock 全链路已验。2026-09-17 已用新 AK 完成首次带账本的真实运行（固定中心，30 次请求）：30 条 OD 全部可缓存（首次实时端点证据齐全），单任务自重复 0.667（3 轮同点，非生产跨任务）；跨任务命中率仍为 0，样本不足以过门禁，跨任务 cache 仍未实现。详见 [阶段二准备设计](backend/docs/performance/poi-distance/stage-2/阶段二_跨任务安全缓存设计与命中率口径.md)。

## 3. 阶段三：矩阵契约，而不是传输加速

矩阵线的下一瓶颈是**端点/选路兼容性**，不是再写一个更快的 mock。

必须先回答：

- raw 距离/时间是否与 `directionlite` 等价（含 UID 绑路）
- 缺结果 / 零值如何与现有 unknown 对齐（不得把 0 当覆盖）
- 无 steps 时能否满足 ≤50m `endpoint_verified`；不能伪设为 true
- 最短距离选路 vs 矩阵单值是否一致（报告已有 950m/700s vs 1050m/600s 逻辑反例）

做法：

1. 权限与小额预算明确后，同一组 OD 成对打两个 API。比较原始数值、端点证据、无结果与失败。真实跨 API 误差目前 **未测得**。
2. 若平台无法提供等价证据，停止把矩阵当正向覆盖依据，保留单点 Provider。
3. 不得用“只验证部分候选”换时间，除非另行证明不会错判其余 POI。
4. 批次失败会放大健康 OD 的重试计费（诊断：2 个失败 OD 在 batch=5 时变成 10、batch=20 时变成 20）。任何后续设计必须按 OD 加权记账，不能按 HTTP 次数声称更省配额。

出口条件：要么证明业务语义等价，要么书面关闭矩阵替代路径。

> 2026-09-16 准备完成：新增零请求成对契约检查器 `backend/tools/matrix_contract.py`（`matrix-contract-review-v1`，拒绝 matrix 携带端点证据、0/缺失记 unknown、检测选路背离，仅 `label=paired-live` 可过门禁）与 9 项测试，并给出合成示例；新增成对收集器 `backend/tools/matrix_pair.py`（`--mode plan` 零请求冻结 5 OD 计划，`--mode live` 需 `--accept-quota`、冻结计划与 qps≤3，配额/限流/权限错误立即停止，单点+矩阵按 OD 记账）与 9 项 MockTransport 测试。真实成对测试已于 2026-09-17 执行（新 AK，冻结计划，6 次请求）：矩阵可正常返回（HTTP 200/status 0），契约评审 `gateEligible=true`、`verdict=incompatible`（3 match / 2 mismatch，时长系统性偏短 -20~-62s），矩阵替代结论维持 **NO**。剩余控制台项为矩阵 OD/秒与日额度。详见 [真实成对测试结果](backend/docs/performance/poi-distance/stage-3/真实成对测试结果_20260917.md) 与 [阶段三协议](backend/docs/performance/poi-distance/stage-3/阶段三_矩阵契约测试协议.md)。

## 4. 阶段四：仅在三条件同时成立后才做正式实现

三个必要条件：正确性、真实配额安全、N=50/100 明显 wall-clock 提速。

若成立，另开代码审查，不把 `.tmp/poi-distance/` 或 `.tmp/poi-matrix/` 直接搬进生产。正式设计应包含：

- provider capability 显式区分单点 / 矩阵
- 按 OD 加权的共享 limiter；保留到达节拍保证，不要只改 Semaphore 或拆掉 `attempt_lock`
- 动态 batch = min(官方上限 50, 账户每秒额度, 待发预算)；批次 50 只是高配额理想模拟中的容量最优，禁止硬编码为生产配置
- 截止、取消、逐格 unknown、整批失败重试账本、受限单点 fallback
- 不预取会被早停丢掉的设施候选（会改变请求量和停止顺序）
- 新的到达节拍、429/冷却、跨进程、重试加权测试

## 5. 明确不做的事

- 为跑分减少采样点、每类 8 候选、1200m 粗筛或早停
- 用矩阵 raw 数值标记 reachable / coverage / `endpoint_verified`
- 在 3 条路线/秒额度下发 50 OD 大批次绕开限制
- 提交 `.tmp/poi-distance/`、`.tmp/poi-matrix/` 实验实现
- 读取或打印真实 AK；把 mock 结果写成真实百度实测
- 把 OSM 离线或 B1 采集目录算进本线 speedup

## 6. 建议执行顺序

```text
账户权限与日/秒预算
        ↓
少量真实分阶段基线（搜索 / walking / 等待 / 重试 / geometry）
        ↓
低配额：跨任务 OD cache 命中率 → 安全 cache（若有空间）
        ↓
矩阵 vs 单点成对契约测试（数值、UID、端点、unknown）
        ↓
契约失败 → 关闭矩阵替代，保持单点
契约成立且 N=50/100 真实收益达标
        ↓
正式 provider + OD 加权 limiter + 动态 batch（新 PR）
```

当前正式合入优化建议仍为 **NO**。阶段一只剩控制台配额核验（人工项）；核验后进入阶段二，先统计真实重复 OD 命中率。
