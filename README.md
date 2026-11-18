# 水样铅浓度超标判定系统

实验室数据员用于判断**某站某日水样的平均铅浓度能否明确超过 10 μg/L** 的操作网页。
FastAPI + SQLite 实现，Docker Compose 一键启动。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000
```

本地开发（无需 Docker）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

运行自测：

```bash
pip install pytest httpx
python -m pytest tests/ -q
```

## 使用流程

1. **上传样本 CSV**（必需列，单位 μg/L；`date` 为 UTC 日期 `YYYY-MM-DD`，兼容表头别名 `utc_date`）：

   ```csv
   sample_id,station_id,date,concentration,random_unc,batch_id
   S001,ST-A,2026-11-01,12.3,0.4,B01
   ```

   示例文件见 `examples/samples.csv`。任何一行校验失败则整批拒绝，不产生部分写入。

2. **录入校准批的系统标准不确定度**（网页表单或 `POST /api/batches`）。

3. **重算**：网页上按站日“重算”或“重算全部待重算”；详情页可查看**本次判定所用批次**（批号、样本数、系统标准不确定度）与判定结果。

## 计算模型

对某站日的 n 个样本（等权）：

- 均值 `m = Σxᵢ / n`
- 合成方差 `u_c² = ( Σu_random,i² + Σ_b (n_b · u_sys,b)² ) / n²`
  - 随机误差互相独立；
  - 同批校准误差完全相关（批内按 `n_b·u_sys,b` 线性相加）；
  - 不同批次独立（批间方差相加）。
- 95% 测量区间：`m ± 1.96 · u_c`（正态假设）。

判定规则（阈值 10 μg/L）：

| 条件 | 判定 |
|---|---|
| 区间下限 > 10 | **超标** |
| 区间上限 < 10 | **未超标** |
| 其他（含端点恰等于 10） | **无法确定** |
| 任一涉及批次未录入系统标准不确定度 | **缺少校准数据，无法判定**（不得给出结论，区间留空，均值仅供参考） |

## 更正与失效语义

- **同号样本再次提交**＝更正该样本（整行替换）；**同批次再次录入**＝更正该批系统标准不确定度。
- 更正与“受影响站日现行结论失效”在**同一事务**提交：样本更正影响其原站日与新站日；批次更正影响所有使用该批的站日。
- 失效结论保留为历史（`superseded`），永不恢复为现行；重算只会插入新结论。
  因此**重算失败、服务重启、并发更正都不会复活旧结论**：
  - 写操作经 `BEGIN IMMEDIATE` 序列化，重算的读取与写入在同一事务内；
  - 部分唯一索引保证每个站日至多一条现行结论；
  - 全部状态持久化于 SQLite（WAL），重启不丢。
- 不受影响的站日的现行结论**照常可查**。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 总览页（上传、录批、站日判定表、重算入口） |
| GET | `/station-days/{station}/{date}` | 站日详情页（样本、所用批次、现行/历史结论） |
| POST | `/api/samples/upload` | 上传样本 CSV（multipart，字段名 `file`） |
| POST | `/api/batches` | 录入/更正校准批，JSON `{"batch_id","systematic_unc"}` |
| GET | `/api/batches` | 校准批列表 |
| GET | `/api/station-days` | 站日列表（`status`: `concluded`/`pending`） |
| GET | `/api/station-days/{station}/{date}` | 站日详情 JSON（含 `live_batches`、结论所用批次快照、历史） |
| POST | `/api/station-days/{station}/{date}/recompute` | 重算单个站日 |
| POST | `/api/recompute-pending` | 重算全部待重算站日 |

## 配置

- `APP_DB_PATH`：SQLite 数据库路径，默认 `./data/app.db`（Compose 中为 `/app/data/app.db`，挂载命名卷持久化）。

## 项目结构

```
app/
  logic.py     # 纯计算：均值、合成不确定度、区间、判定（无 I/O，可单测）
  db.py        # SQLite 连接、schema、写事务
  services.py  # 更正/失效/重算事务
  main.py      # FastAPI 路由（网页 + JSON API）、CSV 校验
  web.py       # 服务端 HTML 渲染
tests/         # pytest：计算模型 + 端到端（含失败/重启/并发场景）
```
