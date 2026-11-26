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
  dispatch.py    跨会场补班求解器（纯逻辑：语言/可用性/时间不重叠、最少换人）
  main.py        create_app() 工厂与全部 HTTP 接口、业务规则
  asgi.py        uvicorn 入口（relay_backend.asgi:app，RELAY_DB 选库）
tests/
  conftest.py    夹具与两个可区分频率的演示 WAV
  test_relay.py  18 个规则测试（含两人接力乱序上传、重启续接、并发）
  test_curated.py 精选的草稿并发合入、缝隙永久关闭、批准撤回下架、重启
  test_dispatch.py 跨会场补班：登记、首排/补班、锁定、无解保留、并发与重启
demo/
  run_demo.py    端到端演示：乱序接力、静音、换班失权、重启，并导出可回听 WAV
  run_curated_demo.py 端到端演示：精选草稿、发行、回听、撤回下架
  run_dispatch_demo.py 跨会场补班：缺人挪动另一会场未开播段、播出后同变更被拒
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

# 跨会场补班演示（SQLite 落到 demo/out_dispatch/）
python3 demo/run_dispatch_demo.py
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
| POST/GET | `/sessions/{sid}/billing/versions` | 提交核账/更正账版，或列出不可变账版 |
| GET | `/sessions/{sid}/billing/versions/{v}` | 查旧账版：截止格、按当班/条目的有声格数与金额 |
| GET | `/sessions/{sid}/balances` | 各收款方当前应付、已付、待回执占用和可为负余额 |
| POST/GET | `/sessions/{sid}/payment-orders` | 按当前账版和 `request_id` 幂等批量开单，或列出付款单 |
| GET | `/sessions/{sid}/payment-orders/{id}` | 查付款单及逐条付款指令/回执状态 |
| POST | `/sessions/{sid}/payment-orders/{id}/receipts` | 整批登记部分付款项的已付/未付回执（冲突整批拒绝） |

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

## 核账与付款

财务接口同样使用工作人员的 `X-Staff-Token`，不连接真实支付系统；所有金额均为
非负整数分。

### 核账账版

`POST /sessions/{sid}/billing/versions`：

```json
{
  "base_version": 2,
  "cutoff_slot": 8,
  "entries": [
    {"duty_id": 1, "start_slot": 0, "end_slot": 4,
     "payee_id": "interpreter-A", "unit_amount_cents": 1200},
    {"duty_id": 2, "start_slot": 4, "end_slot": 8,
     "payee_id": "interpreter-B", "unit_amount_cents": 1500}
  ]
}
```

* 核账范围固定为 `[0, cutoff_slot)`；`cutoff_slot` 不能超过当前已发布连续前缀
  `frontier`，也不能小于上一账版的截止格。
* 只统计已发布的有声格。每个有声格必须且只能匹配一条 `duty_id` 是该格实际录制
  当班、且区间包含该格的条目。静音格、前缀外的暂存上传、精选中的重复引用均不
  计费。
* 首次提交传 `base_version: 0`；之后每次更正都提交**整个核账范围**的新条目。
  接受后版本号加一，旧版的截止格、条目/当班有声格数和分项金额冻结保留。

### 付款单、占用与回执

`POST /sessions/{sid}/payment-orders`：

```json
{
  "request_id": "payout-2026-11-26-01",
  "account_version": 3,
  "payee_ids": ["interpreter-A", "interpreter-B"]
}
```

* 某收款方的可开单金额为
  `current_payable_cents - paid_cents - occupied_cents`；仅当结果严格为正时生成
  付款项。
* 新单必须基于当前账版。开单后，待回执指令金额计入 `occupied_cents`；更正账版
  不会修改或撤销旧指令，因此余额
  `balance_cents = payable_cents - paid_cents - occupied_cents` 可能为负，后续
  新应付会自然抵补。
* 相同 `request_id` 与相同内容重试返回原付款单/原指令；同标识不同内容返回 409。
  写事务使用 `BEGIN IMMEDIATE` 串行化，并发请求不会重复占用同一正余额。

`POST /sessions/{sid}/payment-orders/{order_id}/receipts`：

```json
{"receipts": [
  {"payment_item_id": 12, "status": "paid",
   "external_receipt_no": "BANK-8842", "amount_cents": 4800},
  {"payment_item_id": 13, "status": "unpaid"}
]}
```

* 可只提交本批有结论的部分付款项；其余保持 `pending`。
* 已付回执必须带本场唯一的外部回执号，且金额与原指令一致。重复相同结果是幂等
  no-op；矛盾状态/金额/回执号会使整批原子拒绝。
* 只有 `unpaid` 会释放占用，之后可用**新的** `request_id` 按当前余额再开单；
  重试旧开单请求仍返回旧单，不会另开。
* `GET /sessions/{sid}/balances` 与账版、单据、回执均持久化在同一个 SQLite
  文件中，重启后可继续核对。

小型行为测试见 `tests/test_billing.py`，其中包含“缺格补齐后后段原声才可计费”
以及“旧付款指令在费率更正导致负余额后仍可回执、重试旧请求不多付”的端到端
用例。

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

## 跨会场补班（dispatch groups）

大会有多个同时开播的分会场、共用一批口译员时，用**调度组**统一排班。接入须经
本场工作人员许可（登记时引用其建场凭据所管理的 `session_id`），**不另建账号
体系**：调度组只签发一个组级 `dispatcher_token`，当班凭据仍是每个班段各自的
`duty_token`。**未接入的场次照常使用原人工排班接口，行为完全不变。**

### 数据模型

* 一个调度组登记若干场次，**每场只能加入一个组**；登记时该场必须尚未排班、
  没有任何音频或静音记录。
* 登记提供：场次的**绝对开播时间**（带时区的 ISO-8601）、**节目总格数**、
  **允许换人的格号**（换人点，0 与总格数是隐含边界）和**所需语言**。
* 从格 0 到节目结束按换人点切成**班段**；每格仍按场次自身的 `slot_seconds`
  （标准 2 秒）换算成实际时间，跨会场的时间重叠按绝对时间判定。
* 组内按**人员 ID** 管理口译员，每人有擅长语言和若干**可用时间窗**（窗口按
  并集合并，首尾相接即可）。

### 接口（请求头 `X-Dispatcher-Token`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/dispatch-groups` | 建调度组，返回 `group_id` 与 `dispatcher_token` |
| POST | `/dispatch-groups/{gid}/sessions` | 把一个干净场次登记进组（含开播时间、格数、换人点、语言） |
| POST | `/dispatch-groups/{gid}/schedule` | 提交全量人员可用时间，服务给出并落实一份完整班表 |
| GET | `/dispatch-groups/{gid}/coverage` | 授权调度员看当前覆盖，含每个班段的当班凭据 `duty_token` |
| GET | `/dispatch-groups/{gid}` | 组详情：人员/语言/窗口与不含凭据的覆盖视图 |

`POST .../schedule` 是**首次排班与后续补班的同一入口**，请求体是全量替换的
人员名单（`interpreters`，每人语言 + 可用窗口），可选 `base_version` 做乐观
并发控制；返回新版本号、逐班段安排、本次换人清单与被取消资格的暂存提交数。

### 排班与补班规则

1. **每班段恰有一名**满足语言要求（擅长语言与所需语言有交集）的人。
2. 人员的可用时间窗必须**完整覆盖班段**；同一人的两个班段在**实际时间上不能
   重叠**，跨会场同样生效，首尾相接允许。
3. **已发布即锁定。** 一个班段只要任一格已进入连续发布前缀（含静音），该段
   不能换人、人员固定；新可用时间**只须覆盖尚未发布的部分**（首个未发布格到
   班段结束），已发布前缀不重新校验。
4. **最少换人。** 在满足全部约束的方案中，更换已有班段人员的数量最少；同样
   少的方案按人员 ID 的确定性顺序任选。
5. **整批原子。** 人员名单、各会场班表、上传资格在同一个 `BEGIN IMMEDIATE`
   事务里一并生效或一并不变。无解时返回 409 `schedule_infeasible`，
   `uncoverable_segments` 逐项给出无法同时覆盖的班段与原因，库内仍是旧名单、
   旧班表。
6. **班表即权限。** 新班表生效后，原上传/回听/静音/核账接口一律按它运行：
   * 未换人的班段保留原当班 ID 与 `duty_token`；换人段旧当班被标记
     `superseded`（行与凭据保留可审计），新当班发新凭据。
   * 换人段里原当班的 active 提交被标记 `disqualified` 且**不会自动复活**——
     即使后来同一个人重新当班该段，也必须重新明确提交；旧当班不能再传该段。
   * 已播声音与核账所用来源（duty id、WAV 字节）不变。
7. **护栏。** 已接入场次不能再走 `PUT /sessions/{sid}/duties` 或
   `PATCH /duties/{id}` 绕过组内安排；没有班表时不能靠确认静音提前开场；
   上传与静音都不能超出登记的总格数。
8. **持久化与并发。** 每次成功落实写一条不可变修订
   （`dispatch_revisions` + 逐段冻结记录），重启后以最后一次成功修订为准；
   写事务串行化，配合 `base_version` 乐观校验，并发补班/上传/静音不会用过时
   的可调整范围覆盖刚播出的格，也不会留下某会场已换班、另一会场仍持旧表的
   半步状态。

### 小型可复跑例子

`demo/run_dispatch_demo.py` 演示两个同时开播的会场：

1. bob 的可用时间缩短后，他在会场 B **尚未播到**的 `[4,8)` 班段被跨会场调给
   carol（换人最少），未换人班段凭据保留、旧当班失权；
2. 重启后先让 bob 的该班段播出一格（静音也算发布）使其锁定，再提交**完全相同**
   的可用时间变更，这次被整批拒绝（`locked_segment_holder_unavailable`），
   原班表、凭据与上传资格保持不变。

