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
demo/
  run_demo.py    端到端演示：乱序接力、静音、换班失权、重启，并导出可回听 WAV
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
```

## 角色与接口

### 工作人员（请求头 `X-Staff-Token`，建场次时返回）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/sessions` | 建场次，返回 `session_id` 与 `staff_token` |
| PUT | `/sessions/{sid}/duties` | 指定当班口译员与交接格 `start_slot`（首个当班必须从 0 开始） |
| PATCH | `/duties/{duty_id}` | 修改**未发布时段**的换班格 |
| PUT | `/sessions/{sid}/slots/{n}/silence` | 把等待无果的空格记为静音，此后该格封闭 |
| GET | `/sessions/{sid}/host` | 主持端视图：frontier、卡在哪一格及原因、逐格状态 |

### 口译端（请求头 `X-Duty-Token`，排班时按当班签发）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/duties/{duty_id}/slots/{n}/audio` | 上传单格单声道 WAV（body 为原始 WAV 字节） |

### 听众端（无需鉴权）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/sessions/{sid}/program?start=k&limit=m` | 从格 k 读取连续已发布节目，列出格号与声音/静音标记 |
| GET | `/sessions/{sid}/slots/{n}/audio` | 取某已发布格的 WAV（静音格返回 404 `slot_is_silence`） |

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
