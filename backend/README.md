# 15 分钟生活圈双算法后端

后端同时提供两种 900 秒生活圈算法：百度边界搜索（E8.2，`local-multicross-e82`）使用真实步行端点证据做径向搜索与局部多边界重建；Hybrid v1.5 以 OSM 路网提供参考、百度详细步行路线核验。两者并存以供用户选择和开发对比，界面不标注速度或精度优劣。

## 启动

Python 3.11+，在backend目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt -e ../life-circle-algorithm
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

首次配置参考 `.env.example`，保留本地服务端BAIDU_MAP_AK、ANALYSIS_QPS和OSM数据路径。不要将服务端密钥放入前端。系统环境优先于.env。已有.env无需覆盖。

OSM图缓存、版本、覆盖边界、障碍和风险层见 [数据说明](../data/osm/README.md)。启动和/health不加载城市图，第一次Hybrid任务才惰性加载；缺失数据明确显示degraded。

## 接口

两个任务入口相互独立：

- `POST /api/analyses`：百度边界搜索（E8.2），使用原 `center` / `coordinateSystem` / `budget` / `clientRequestId` 请求。默认预算 400，保留 200/800 档；结果 `algorithm=local-multicross-e82`，只提供真实计算的 15 分钟圈，未知或未收敛结果保持部分结果语义。请求通过薄适配层进入团队 E8.2 核心，不经过旧自适应网格实现。
- `POST /api/v1/analysis/hybrid`：OSM＋百度算法，使用 HybridRequest：

```json
{"origin":{"lng":121.513925,"lat":31.313079},"coordinate_system":"bd09ll","config":{"max_baidu_requests":400},"client_request_id":"example-unique-id"}
```

两个前缀都支持任务查询、结果和取消；Hybrid 还支持按请求 ID 查询恢复。请求契约不可混用，服务端不会静默改用另一算法。Hybrid 结果额外提供只读的 `displayGeometry`：扣除 OSM 水体前的圈面外壳，仅用于地图外轮廓展示（不填色、不画内孔），计算几何、面积统计与诊断仍以 `geometry` 等原字段为准；旧响应缺少该字段时前端退回原几何外环显示。

E8.3（POI 引导联合构圈）仍为离线实验，不接入浏览器或生产 HTTP 入口，代码保留在 `backend/tools/endpoint_e83_*`。旧自适应网格实现保留作历史基线。

独立 `/api/v1/analysis/osm_offline` 继续作为离线基线接口。`ANALYSIS_PROVIDER=baidu` 控制纯百度任务的 Provider；`synthetic` 仅用于离线测试。Hybrid 始终从自己的入口运行。

每个管理器限制本算法的并发任务，并共享百度 QPS 限流器。Hybrid 账本位于 `.hybrid-ledgers`；重启不自动续跑。任务 completed 不等于精度验收通过；Hybrid 的 `facilitiesStatus=not_integrated` 表示设施没有接入。

## 检查

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m tools.export_contract
```


自动测试使用虚拟密钥和模拟 HTTP 响应，不使用真实 AK、不消耗配额。覆盖健康检查、配置优先级、缺少密钥、跨域、响应验证、网络失败、禁止重试与日志脱敏。当前依赖会产生 Starlette 测试客户端的兼容性弃用提示，不影响测试结果。

## B1 百度 POI 数据层

三类 POI 的独立内部服务、固定 4×4 查询计划、分页／分类／UID 去重、审计账本和回放入口见[任务 B1 接口与验收说明](docs/任务B_POI接口与验收说明.md)。2026-09-14 已按 QPS=2 完成 102 次真实请求，正式采集接受 88 个设施，3 个查询序列受限，详见[测试与验收报告](docs/reviews/2026-09-14/任务B_测试与验收报告.md)。新运行仍默认拒绝联网；先用合成夹具运行 `python -m tools.poi_collect --mode plan` 和 `--mode replay`，真实运行必须具备独立配置授权及账本。

## POI 距离性能与阶段一基线

步行矩阵两轮结论均为不合入，后续路线以根目录 [scheme.md](../scheme.md) 为准。阶段一账户、预算与真实基线已收尾：工具为 `python -m tools.stage_baseline --mode inventory|mock|live`，live 必须 `--accept-quota`，pacing 默认上限 3，只写新文件、不覆盖既有结果；分阶段账本在 `app/stage_ledger.py`。旧 AK 的 walking 核验值（3 次/秒、5000 次/日）已因 2026-09-17 换新 AK 清空，[console-inventory.json](docs/performance/poi-distance/stage-1/console-inventory.json) 现为未核验；可经 `--console-inventory` 合并到新输出。新 AK QPS 试测见 [QPS 试测报告](docs/performance/poi-distance/qps-probe/探测报告_20260917.md)（约 20 req/s 内未限流），矩阵额度仍未核验。QPS 试测工具为 `python -m tools.qps_probe --mode plan|live`（live 需 `--accept-quota`，`--concurrency` 上限 3，`--budget` 上限 60）。阶段三真实成对测试已于 2026-09-17 执行（新 AK，冻结计划 6 次请求）：矩阵可返回但评审 `verdict=incompatible`（时长系统性偏短），矩阵替代维持 NO，见 [真实成对测试结果](docs/performance/poi-distance/stage-3/真实成对测试结果_20260917.md)。真实步行基线数据、出处警告与剩余缺口见[阶段一报告](docs/performance/poi-distance/stage-1/阶段一_账户预算与真实基线报告.md)。

阶段二准备、第一步与逐 OD 记录能力已完成，但**未实现**跨任务缓存：导出器 `python -m tools.od_export --format boundary-reference-v2 --input <真实产物> --output <新文件>` 与命中率分析器 `python -m tools.od_cache_review --input <OD 明细> --output <新文件>` 均为零请求、拒绝覆盖、不输出凭据；`python -m tools.stage_baseline --mode live|mock --observations-output <新文件> [--task-id ID]` 可在挂账本运行时落 `od-observations-v1`（含设施 UID 与端点证据）。已导出唯一的已授权本地真实参考集（48 条耗时，33 唯一键、15 条相邻线段共享端点重复，因缺端点证据全部不可缓存）。是否实现 cache 取决于真实可缓存跨任务命中率，仍需一次带账本的真实运行；键与门禁见 [阶段二设计](docs/performance/poi-distance/stage-2/阶段二_跨任务安全缓存设计与命中率口径.md)。

阶段三准备已就绪：成对收集器 `python -m tools.matrix_pair --mode plan|live`（plan 零请求冻结 5 OD 计划；live 需 `--accept-quota`、冻结计划与 qps≤3）与契约检查器 `python -m tools.matrix_contract --input <成对案件> --output <新文件> --label paired-live`（零请求、拒绝 matrix 端点证据、0/缺失记 unknown、检测选路背离）。真实成对调用待控制台权限与小额预算，协议见 [阶段三契约测试协议](docs/performance/poi-distance/stage-3/阶段三_矩阵契约测试协议.md)；矩阵替代结论维持 **NO**。

复核人按 [N02 验收记录](docs/N02-验收记录.md) 检查结果。只有真实调用证据为 `success`，才能勾选“至少一次百度真实请求成功”；测试通过或健康检查成功不能替代此项。

## N04 / N05 新入口

已同步算法提交 `2d63015`（团队主分支合并 `50d3d6b`），并接入当前服务：

- `GET /api/v1/analysis/mock/complete`：另有 `partial`、`failed`、`empty` 三组。
- `POST /api/v1/analysis/synthetic`：实际执行合成时间场算法，不调用百度。
- `/docs`：交互式请求与响应模型；[契约说明](docs/N05-接口契约.md)。
- [参数、调用与边界样例](docs/N04-算法参数与边界.md)、[验收记录](docs/N04-N05-验收记录.md)。

从 backend 执行 `python -m tools.export_contract` 可重建四组 Mock、JSON Schema 和 OpenAPI 快照。使用上述 `.venv` Python。

以上不调用真实百度。两套算法的真实验收必须分别记录入口、配置与调用预算，不能用一套结果替代另一套。


[当前状态](../CURRENT_STATE.md) · [Hybrid设计](../HYBRID_ISOCHRONE_DESIGN.md) · [2.1失败报告](reports/baidu-v21-live-20260917-network/report.md)
