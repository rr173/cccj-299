# 口译声道接力后端

线上会议的一条可回听口译声道的接力服务：同一场次使用**从 0 开始、每格 2 秒的
节目格（slot）**，工作人员安排当班口译员、在指定格换班，口译端按格上传本地
短单声道 WAV，听众读取没有缺口的连续已发布节目并回听。纯 Python + FastAPI +
SQLite，不做实时编解码，也没有界面。

## 目录结构

```
relay_backend/
  db.py          SQLite 表结构与连接/事务工具
  wav.py         标准库生成/校验单声道 PCM WAV（演示用 8kHz/16bit/2s 正弦音）
  main.py        create_app() 工厂与全部 HTTP 接口、业务规则
  asgi.py        uvicorn 入口（relay_backend.asgi:app，RELAY_DB 选库）
tests/
  conftest.py    夹具与两个可区分频率的演示 WAV
  test_relay.py  18 个规则测试（含两人接力乱序上传、重启续接、并发）
  test_curated.py 精选的草稿并发合入、缝隙永久关闭、批准撤回下架、重启
demo/
  run_demo.py    端到端演示：乱序接力、静音、换班失权、重启，并导出可回听 WAV
  run_curated_demo.py 端到端演示：精选草稿、发行、回听、撤回下架
```

## 运行

```bash
pip install -r requirements.txt

# 启动服务（数据库位置由 RELAY_DB 控制，默认 ./relay.db）
RELAY_DB=/tmp/relay.db uvicorn relay_backend.asgi:app --port 8000

# 跑测试
python3 -m pytest tests/ -q

# 跑端到端演示（WAV 导出到 demo/out/，可用任意播放器回听）
python3 demo/run_demo.py

# 精选流程演示（WAV 导出到 demo/out_curated/）
python3 demo/run_curated_demo.py
```

## 角色与接口

### 工作人员（请求头 `X-Staff-Token`，建场次时返回）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/sessions` | 建场次，返回 `session_id`、`staff_token` 与 `reviewer_token` |
| PUT | `/sessions/{sid}/duties` | 指定当班口译员与交接格 `start_slot`（首个当班必须从 0 开始） |
| PATCH | `/duties/{duty_id}` | 修改**未发布时段**的换班格 |
| PUT | `/sessions/{sid}/slots/{n}/silence` | 把等待无果的空格记为静音，此后该格封闭 |
| GET | `/sessions/{sid}/host` | 主持端视图：frontier、卡在哪一格及原因、逐格状态 |
| GET | `/sessions/{sid}/curated/draft` | 读精选草稿：版本号、逐项出现（`appearance_id` + 来源格号）与缝隙 |
| POST | `/sessions/{sid}/curated/draft/edits` | 提交一批草稿编辑（整批生效或整批拒绝） |
| POST | `/sessions/{sid}/curated/releases` | 把**当前非空**草稿版本发行，要求每格都有有效批准 |

### 素材审核员（请求头 `X-Reviewer-Token`，建场次时签发）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/sessions/{sid}/curated/reviewer` | 工作人员取回本场审核员凭据（旧库补发） |
| GET | `/sessions/{sid}/curated/approvals` | 列出批准记录（工作人员亦可读） |
| POST | `/sessions/{sid}/curated/approvals` | 批准已发布节目的一段格 `{start_slot, end_slot}` |
| DELETE | `/sessions/{sid}/curated/approvals/{id}` | 撤回一条批准；若在播版因此失去覆盖则整版下架 |

### 听众端（无需鉴权）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/sessions/{sid}/program?start=k&limit=m` | 从格 k 读取连续已发布原节目，列出格号与声音/静音标记 |
| GET | `/sessions/{sid}/slots/{n}/audio` | 取某已发布原格的 WAV（静音格返回 404 `slot_is_silence`） |
| GET | `/sessions/{sid}/curated` | 在播精选版：格序（position）、原格号（`source_slot`）与取音地址 |
| GET | `/sessions/{sid}/curated/slots/{p}/audio` | 取在播精选第 p 格的 WAV（静音项 404 `entry_is_silence`） |

精选不做音频合成：每个 position 直接引用原节目某格的 WAV 字节。

## 关键规则与实现方式

1. **识别到具体当班而非人名。** 每次排班产生一条 `duties` 记录和独立
   `duty_token`。同一口译员掉线重连仍用原 token 继续当班；再次当班产生新
   duty、新 token。上传按 duty 鉴权，人名只作展示。
2. **换班在指定格生效。** duty 按 `start_slot` 排序，格 n 归属起点不大于 n
   的最后一个当班；前一班只负责到交接格前一格。
3. **已发布格不可回填或替换。** 已进入连续前缀的格一律拒绝写入
   （`slot_finalized`）；同一当班对同一格的 active 提交有部分唯一索引
   兜底，重试即使针对未发布格也被拒（`already_submitted`），不能借重试换声音。
4. **掉线前录好的音频可在负责范围内补传。** 上传只校验该格当前是否仍归属本
   当班，与连接状态无关；前面有缺口时声音先收下但不发布（主持端显示
   `buffered_ahead`），掉线暂缺不会被误判为静音。
5. **失去资格的声音不会静默恢复。** 换班/改班后，对不再负责的格，原 active
   提交被标记为 `disqualified` 并永久保留；即使原当班后来重新负责该格，旧
   记录也不会自动复活，必须重新明确提交（主持端显示 `disqualified_only`）。
6. **只发布无缺口的连续前缀。** frontier 从格 0 逐格推进，每格必须有归属
   当班的 active 音频或工作人员的静音标记；首个缺口之后全部不发布。
7. **静音是显式人工裁决。** 记静音后该格永久封闭（`slot_marked_silence`），
   重复标记、对有合格声音/已发布格标记均被拒。
8. **改班只能动未发布时段。** 起点已落入发布前缀的当班不可移动，新起点也不
   能进入发布前缀；首个当班必须始终覆盖格 0。
9. **重启续接。** 全部状态（含 WAV 字节）在 SQLite 中，重新打开同一库文件
   即恢复，已发布声音原样可回听。
10. **并发安全。** 每请求独立 SQLite 连接，写事务 `BEGIN IMMEDIATE` 串行化，
    `busy_timeout` 处理锁等待；部分唯一索引保证同一当班每格至多一条 active。

### 主持端逐格状态

`published`（已发布声音）/ `silence`（静音）/ `buffered_ahead`（声音已到但
前面有缺口）/ `waiting`（等归属当班上传）/ `disqualified_only`（只剩失权旧
声音，须重新提交）/ `not_assigned`（尚无当班覆盖）。`blocked` 字段直接给出
当前卡住的格号与原因。

## 官方精选

精选是原节目之外的另一条发布线，原节目的格、声音与发布前缀都不受影响。每场
只有一份草稿和至多一个在播发行版。

### 草稿模型

* 草稿是若干**摘录**（excerpt）的有序串。每段摘录引用原节目里一段连续格
  `[start_slot, end_slot]`，可以重排、重复引用同一段原声；每次出现是一条独立
  记录，有稳定 `excerpt_id`，互不替代。
* 相邻摘录之间（以及首尾边界）有一个**缝隙**（gap），各有独立 `gap_id`。
  缝隙被填入后**永久关闭**：即使后来把填入的摘录删空，旧缝隙也不会重新打开，
  也不能再被旧请求沿用。
* 草稿版本号 `version` 是已接受编辑批次的计数。读草稿时得到当前版本与逐项
  id；提交编辑时带上它作为 `base`。
* 发行时按顺序把摘录展平成逐格条目：一段 `[3, 5]` 在发行版里是三个位置，
  各自引用原格 3、4、5。

### 批量编辑

`POST /sessions/{sid}/curated/draft/edits`：

```json
{"version": 3, "operations": [
  {"kind": "insert",  "gap_id": 12, "start_slot": 5, "end_slot": 6},
  {"kind": "replace", "excerpt_ids": [7, 8], "start_slot": 9, "end_slot": 9},
  {"kind": "replace", "excerpt_ids": [3]}
]}
```

* `insert` 在指定缝隙插入一段摘录；`replace` 把草稿中一段**连续**摘录替换成
  新的原格段，省去 `start_slot` 即删空。
* 一批内不能重复选中同一摘录、同一缝隙，也不能在要删换的摘录紧邻的缝隙插入；
  任一操作不满足则**整批拒绝**，什么都不改。
* 已接受批次的冲突按稳定 id 与"上次被改动版本"判定：
  - 位置移动（别处插入/删除导致的序号变化）**不算**冲突；
  - 引用同一段原声的两次出现互相独立，改一次不影响另一次；
  - 删换检查被选中的摘录及其内部相邻关系是否在 `base` 之后被改过；
  - 插入检查缝隙本身（含首尾边界）是否在 `base` 之后被改过；
  - 填过又清空的缝隙视为已改过且已关闭，旧 `gap_id` 不能再用于插入。
* 被拒时返回 409 `batch_rejected`，`detail.operations` 逐项给出失效的操作下标
  与原因。

### 批准与发行

* 素材审核员对**已发布**原节目的一段格批准（区间可重叠），也可按批准记录撤回。
  一个格只要仍被任一未撤回的批准覆盖就可用；草稿编辑不受批准限制。
* `POST /curated/releases` 只能发行**当前版本**且**非空**的草稿，并要求每个被
  引用格都有有效批准，否则 409。
* 每场至多一个在播（`live`）版本。发行新版会把旧版置为 `superseded`；后续改稿
  不影响在播内容。
* 撤回批准若使在播版任一引用格失去覆盖，整版**同时**下架（`taken_down`）。
  下架是原子的：列格与取音都不会看到半下架状态。已下架/被替换的发行版不能再
  经精选接口列格或取音。后来补批也不会恢复旧版，必须重新发行。
* 批准、撤回、发行、读取都在 SQLite 事务里完成，重启后全部状态保留。
