# 电控板装配追溯 API（Board Traceability API）

制造厂追溯场景：一批电控板发现缺陷，需要从**装配与返修日志**中找出在指定
UTC 时刻仍装有该批次板件的**成品**（序列号 `P-` 开头），并给出多级装配的
事件路径，通知检修。

技术栈：**FastAPI + SQLite（WAL 模式）**，可通过 **Docker Compose** 一键启动。

---

## 1. 数据模型与 CSV 格式

CSV 为 UTF-8 编码（允许 BOM），表头支持中英文，列顺序不限，动作支持
`INSTALL/装入`、`REMOVE/拆出`：

| 字段 | 中文表头 | 含义 |
| --- | --- | --- |
| `event_no` | 事件号 | 全局唯一事件编号 |
| `event_ts` | UTC时间 | ISO-8601 UTC 时间，如 `2026-11-01T08:00:00Z`（无时区则按 UTC 解释） |
| `parent_part_no` | 父件号 | 装入位置（父装配件/成品序列号） |
| `child_part_no` | 子件号 | 被装物料号（如电控板 `EC-100`） |
| `child_batch` | 子件批次 | 子件批次号 |
| `action` | 动作 | `INSTALL`（装入）/ `REMOVE`（拆出） |

成品序列号以 `P-` 开头；追溯结果中链路顶端为 `P-` 序列号的即受影响成品。

样例见 [`samples/events_valid.csv`](samples/events_valid.csv)。

## 2. 业务规则

* **按批次 + 时刻追溯**：`GET /trace` 返回该时刻仍装有指定批次子件的全部
  成品，以及从缺陷板件到成品的多级装配事件路径（每一级的装入/拆出事件号、
  时间、动作、父子件、批次）。
* **拆出后不再命中**：时刻晚于（或等于）拆出事件的查询不会命中该成品；
  之后的重装/流转会产生新的归属链路。
* **允许补录较早的返修事件**：上传不要求按事件时间有序。每次上传都把
  新事件与库中全量日志合并，**按事件时间重放**（同一时刻按事件号排序）。
* **整份拒收**（HTTP 422，事务回滚，旧版本日志继续可查），任一情况即拒收：
  * **子件同时归属两处**：同一 `(子件号, 批次)` 未拆出又被装入另一父件；
  * **无对应装入的拆出**：REMOVE 找不到处于装入状态的匹配子件，或父件号
    与装入位置不一致；
  * **装配环**：新增边会形成 A⊆B⊆…⊆A 的循环。
* **幂等重传**：同 `event_no` 且内容完全一致 → 跳过、不重复入库；
  同 `event_no` 但内容不同 → 整份拒收（422），并返回存储值与上传值对照。
* **并发一致性**：SQLite WAL + 单写锁（`BEGIN IMMEDIATE`）。查询在一个
  只读快照事务内完成，上传提交期间查询只会看到**完整的一版日志**，看不到
  半写入状态；上传之间串行化。
* 每次上传（含被拒收的）都会在 `GET /uploads` 留下审计记录。

## 3. API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/uploads` | 上传事件 CSV（multipart 字段 `file`） |
| GET | `/trace?child_part_no=&batch=&at_time=` | 批次 + UTC 时刻追溯 |
| GET | `/events` | 已接收事件（按事件时间排序） |
| GET | `/uploads` | 上传批次审计记录 |
| GET | `/health` | 健康检查 |
| GET | `/docs` | Swagger UI 交互式文档（可直接演示） |
| GET | `/help` | 内嵌的中文演示指引页 |

`/trace` 响应示例：

```json
{
  "queried_at": "2026-11-01T09:30:00Z",
  "child_part_no": "EC-100",
  "child_batch": "B20261101",
  "affected_products": [
    {
      "root_part_no": "P-1001",
      "root_batch": "LOT-SA1",
      "is_finished_product": true,
      "path": [
        {"event_no": "E001", "action": "INSTALL", "parent_part_no": "SA-200", "child_part_no": "EC-100", "child_batch": "B20261101", "event_ts": "..."},
        {"event_no": "E002", "action": "INSTALL", "parent_part_no": "P-1001", "child_part_no": "SA-200", "child_batch": "LOT-SA1", "event_ts": "..."}
      ]
    }
  ],
  "open_chains": [],
  "total_affected_products": 1
}
```

`open_chains` 为顶端还不是成品（无 `P-` 序列号）的在制链路，便于追溯在制品。

## 4. Docker Compose 启动（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

SQLite 数据保存在命名卷 `trace-data`（容器内 `/data/trace.db`），
容器重建后数据保留；`docker compose down -v` 可清空。

## 5. 本地运行（无 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
bash demo.sh                 # 自动起服务并跑一遍上传/追溯演示
# 或手动：
TRACE_DB_PATH=./trace.db uvicorn app.main:app --reload --port 8000
```

### 在 /docs 中手工演示

1. `POST /uploads` → `file` 选择 `samples/events_valid.csv` → Execute；
2. `GET /trace` 输入 `child_part_no=EC-100`、`batch=B20261101`、
   `at_time=2026-11-01T09:30:00Z` → 命中 `P-1001`；
3. 把时刻改成 `2026-11-01T10:30:00Z`（拆出后）→ 0 命中；
4. 再上传 `samples/invalid_two_parents.csv` / `invalid_orphan_remove.csv` /
   `invalid_cycle.csv`，观察 422 拒收与详细错误，且旧查询结果不变；
5. 重传同一个合法 CSV → `new_events=0, duplicate_events=11`。

## 6. 样例文件说明

| 文件 | 预期结果 |
| --- | --- |
| `events_valid.csv` | 合法：两条独立多级链路 + 拆出/换批/成品流转，11 个事件 |
| `invalid_two_parents.csv` | 拒收：子件（同一批次）同时归属两个父件 |
| `invalid_orphan_remove.csv` | 拒收：无对应装入的拆出 |
| `invalid_cycle.csv` | 拒收：装配环 |
| `backfill_repair_accepted.csv` | 接受：补录早于现有事件的返修（装入+拆出） |
| `backfill_orphan_rejected.csv` | 拒收：补录的拆出按事件时间重放后无对应装入 |
| `conflict_dup_event_no.csv` | 拒收：事件号相同但内容不同 |

## 7. 测试

```bash
pip install -r requirements.txt httpx pytest
python -m pytest tests/ -q
```

测试覆盖：合法上传与多级追溯、拆出后不命中、换批与成品流转、补录接受/
拒收、三类结构违规、幂等与同号冲突、坏时间戳/缺列 400、上传与查询并发时
快照始终完整（17 个用例）。

## 8. 目录结构

```
app/
  config.py      # 配置（DB 路径、P- 前缀）
  database.py    # SQLite 连接、WAL、建表、写锁
  timeutil.py    # UTC 解析与微秒时间戳
  csvparse.py    # CSV 解析（中英文表头/动作别名）
  replay.py      # 事件时间重放、结构校验、时刻追溯
  service.py     # 上传事务：幂等检查 → 全量重放 → 原子提交/审计
  schemas.py     # 响应模型
  main.py        # FastAPI 路由
samples/         # 本地演示 CSV
tests/           # pytest 用例（含并发测试）
docker-compose.yml / Dockerfile / demo.sh
```

## 9. 设计说明与边界

* 事件时间内部存为 UTC 微秒整数，保证重放顺序确定（时间戳并列按事件号）。
* 追溯沿“时刻有效边”自底向上 DFS；当同一中间件物料号存在多个并行实例而
  事件中无法区分实例批次时，链路向上展开采用**宁可多报候选、不漏报缺陷
  成品**的保守策略；样例数据中各装配件实例均有独立批次，路径精确。
* SQLite 单写者模型适合本场景的写入规模；如需水平扩展可将存储替换为
  Postgres，接口与重放逻辑不变。
