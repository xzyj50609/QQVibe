# 数据契约与历史冻结 DDL

当前消息库 schema version 4，使用 PRAGMA user_version。下文保留历史冻结DDL以验证迁移；当前群/成员DDL见[group-data-schema](group-data-schema.md)。


> 本文的每段 SQL **必须**被 `scripts/test-migration-contracts.py` 在开启 `PRAGMA foreign_keys=ON`
> 的 SQLite 上实际执行，并通过合法插入、孤儿拒绝、唯一键幂等、事务回滚、大整数 TEXT 往返。
> 这些检查只用内存合成库，不能代替真实运行验收。

日期：2026-09-29。本文是 [source-contract.md](source-contract.md) 的数据层配套。

原则（02 §3/§4）：每个账号一个 SQLite 库，位于 `QQVibeData/accounts/<account_key去掉a:后的32位hex>/messages.sqlite`；
所有 QQ 侧 ID（UIN/UID/peerUid/msgId/msgSeq）**一律 TEXT 保存**，禁止转 Number（V03）；查询参数化；
schema 版本化，迁移带回滚。

## 0. 本轮相对 5c337e5 的三处结构变更

| 变更 | 原因 |
|---|---|
| `conversations` 增 `UNIQUE(account_key, conversation_key)` | RS05：外键引用父表无此唯一约束，开启 `foreign_keys` 后第一条消息即 `foreign key mismatch` |
| `native_seq` `INTEGER` → `TEXT` | RS06：原始序号必须原样保留；实测整数列把 `"9223372036854775808"` 存成 REAL 并丢原值 |
| 删除 `time_tie`，新增 `local_seq INTEGER` | RS08：`(time_ms, sha256前16位)` 的 64 位截断碰撞未被排除，且浏览序与批次序第二元素不一致。改为**一个**严格全序 `(time_ms, local_seq)`，两个游标共用同一比较器（§3） |

## 1. 原始字段映射（QCE 6.3.0 → 规范消息）

来源：T01 实测（提交 86677cb 报告 §8）+ QCE 固定源码。样本均为合成值。

| QCE 原始字段 | 实测形态 | 规范字段 | 转换/校验 |
|---|---|---|---|
| `msgId` | 字符串，19 位数字（如 `"756241784712300001"`） | `native_id`（TEXT） | 原样保存；`native_id_kind="qce-msgId"`；非法（空/非串）计数拒绝 |
| `msgSeq` | **原始值即字符串**（T01 §8 记录为字符串形态；整数形态属未证） | `native_seq`（TEXT，可空） | **原样字符串入库，绝不 `int()`**；仅作会话内排序辅助与对照，不参与唯一键 |
| `msgTime` | 字符串秒级 epoch（如 `"1700000000"`） | `time_ms`（INTEGER，UTC 毫秒） | `ms = int(sec) * 1000`；**只允许此一处乘 1000**；原值保留在 `raw.msgTime`；形态异常（长度 ≥13 位视为毫秒误用）拒绝并计数 |
| `senderUin` | 字符串 UIN | `sender_uin`（TEXT） | 原样 |
| `senderUid` | 字符串 UID（如 `"u_..."`） | `sender_uid`（TEXT，可空） | 与 UIN 并存，别名表见 §5 |
| `sendType` | 2=本人 / 0=对方 / 3=系统 | `direction`（`self`/`peer`/`system`） | 主规则=`sender_uin==本人 uin`；`send_type` 原样另存；两者冲突 → `direction='conflict'` 并计数，不猜（V05） |
| `msgType` | 整数（2=文本等） | `msg_type`（INTEGER）+ `kind`（text/image/…/unknown） | 映射表版本化；unknown 保留占位 `[未知类型]` |
| `recallTime` | **0=未撤回；>0=已撤回的撤回时刻（秒）** | `recall_time`（TEXT，可空，原样）+ `status` | 判定规则见 §6.2：仅 `recallTime` 解析为整数且 **> 0** 才置 `recalled`；`"0"`/`0`/缺字段=未撤回；`null` 与空串=形态未知，保留旧状态并计入unknownRecall。0 与 null 不得都当撤回，也不得都当未撤回而漏掉形态异常 |
| `peerUid` + `chatType=1` | 单聊定位键 | `conversation_key` | 见 §5 |
| 正文 | — | `text`（TEXT，可空） | **只入库不进日志**；引用文本存 `quote` 单列，不计对方人格文本（R15） |

未知/新增字段：存入 `raw` JSON 列原样保留；解析器对未知 `msgType` 值计 `unknownTypes`，不静默丢弃（V08）。

## 2. 表结构（schema version 3；v2 核心表不变）

### 2.1 `conversations`

```sql
CREATE TABLE conversations (
  conversation_key TEXT PRIMARY KEY,        -- 见 §5 规则
  account_key      TEXT NOT NULL,           -- = accountKeyHash（§4）
  peer_uin         TEXT,
  peer_uid         TEXT,
  display_name     TEXT,                    -- 显示值，绝不参与身份判定
  kind             TEXT NOT NULL CHECK (kind IN ('friend')),
  selected         INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0,1)),
  first_time_ms    INTEGER,
  last_time_ms     INTEGER,
  data_revision    INTEGER NOT NULL DEFAULT 1,
  earliest_affected_time_ms INTEGER,        -- 与local_seq组成完整失效边界
  earliest_affected_local_seq INTEGER,      -- 不可单独按序号比较先后
  UNIQUE (account_key, conversation_key)
);
```

### 2.2 `messages`

```sql
CREATE TABLE messages (
  message_key      TEXT PRIMARY KEY,
  account_key      TEXT NOT NULL,
  conversation_key TEXT NOT NULL,
  native_id_kind   TEXT NOT NULL,            -- 'qce-msgId' | 'import-file-line'
  native_id        TEXT NOT NULL,            -- 原样字符串（19 位数字或文件行指纹 ID）
  native_seq       TEXT,                     -- 原始序号原样字符串，绝不转整数（RS06）
  local_seq        INTEGER NOT NULL CHECK (typeof(local_seq) = 'integer' AND local_seq >= 1), -- 不可变本地键
  sender_uin       TEXT,
  sender_uid       TEXT,
  send_type        TEXT,                     -- 原样保留，供交叉校验
  direction        TEXT NOT NULL CHECK (direction IN ('self','peer','system','conflict')),
  time_ms          INTEGER NOT NULL,
  msg_type         INTEGER,
  kind             TEXT NOT NULL,
  text             TEXT,
  quote            TEXT,
  status           TEXT NOT NULL DEFAULT 'normal'
                     CHECK (status IN ('normal','recalled','revised','conflict')),
  recall_time      TEXT,                     -- 原始 recallTime 字符串；null=上游未给该字段
  revision         INTEGER NOT NULL DEFAULT 1,
  raw              TEXT,
  normalize_version TEXT NOT NULL,
  UNIQUE (account_key, conversation_key, native_id_kind, native_id),
  UNIQUE (account_key, conversation_key, local_seq),
  FOREIGN KEY (account_key, conversation_key)
    REFERENCES conversations (account_key, conversation_key)
);
CREATE INDEX idx_messages_conv_order
  ON messages (account_key, conversation_key, time_ms, local_seq);
```

- `message_key = "m:" || lower(hex(sha256(account_key || '|' || conversation_key || '|' ||
  native_id_kind || '|' || native_id)))[0:32]`，由应用计算，入库后不变。
- 群聊在库层即被拒绝：`conversations.kind` CHECK 只允许 `'friend'`（X7/R16）。

### 2.3 `message_conflicts`

```sql
CREATE TABLE message_conflicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_key TEXT NOT NULL, conversation_key TEXT NOT NULL,
  native_id_kind TEXT NOT NULL, native_id TEXT NOT NULL,
  existing_revision INTEGER NOT NULL, incoming_raw TEXT NOT NULL,
  detected_at INTEGER NOT NULL,
  resolved INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0,1))
);
```

### 2.3.1 已见语义版本（2026-09-29 实现修正）

```sql
CREATE TABLE message_observations (
  message_key TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  PRIMARY KEY (message_key, fingerprint),
  FOREIGN KEY (message_key) REFERENCES messages (message_key)
);
```

每个规范消息版本按正文、引用、方向、时间、发送者、原始序号与撤回/冲突状态生成 SHA-256。
不将日志、抓取时间或无关 raw 元数据纳入指纹。消息、版本指纹、冲突证据、revision 和 checkpoint 同事务提交。
同一版本重放不重复记冲突或触发重算。未裁定的正文变化进入 conflict，原正文与 raw 保持对应；revised 不表示自动采用后来正文。
撤回证据不会被旧 normal、0 或未知撤回字段取消。

### 2.4 `sync_checkpoints`（fetch cursor，契约 §4.1）

```sql
CREATE TABLE sync_checkpoints (
  account_key TEXT NOT NULL,
  conversation_key TEXT NOT NULL,
  cursor_version INTEGER NOT NULL,
  platform TEXT NOT NULL CHECK (platform = 'qq'),
  window_start_ms INTEGER NOT NULL,
  window_end_ms INTEGER NOT NULL,            -- 本任务计划窗口；epoch 毫秒
  scanned_through_ms INTEGER NOT NULL DEFAULT 0,  -- **已确认扫描完成**的前沿
  last_commit_time_ms INTEGER NOT NULL DEFAULT 0,  -- 本地提交时刻，非服务器时间
  last_commit_count INTEGER NOT NULL DEFAULT 0,
  last_task_status TEXT NOT NULL CHECK
    (last_task_status IN ('complete','complete-empty','partial','error')),
  attempts INTEGER NOT NULL DEFAULT 0,
  overlap_ms INTEGER NOT NULL DEFAULT 2000,
  state TEXT NOT NULL CHECK (state IN
    ('IDLE','FETCHING','STAGING','COMMITTED','ERROR','DISCONNECTED','CATCHING_UP','PARTIAL')),
  last_error TEXT,
  partial_reason TEXT,
  UNIQUE (account_key, conversation_key),
  FOREIGN KEY (account_key, conversation_key)
    REFERENCES conversations (account_key, conversation_key)
);
```

**没有 `last_commit_seq`。** QCE 6.3.0 的 `fetch` 响应里没有可持久化的上游提交序号
（T01 §8 与本轮源码核对：返回体是 `messages/totalCount/currentPage/totalPages/hasNext/cacheHit/fetchedAt`），
旧稿 §4.1 写 `lastCommitSeq` 属**凭空字段**（RS08）。续读所需的状态改由
`window_start_ms/window_end_ms/scanned_through_ms/last_task_status/attempts` 五元组表达（§4）。

### 2.5 `identity_aliases`（UIN↔UID↔peerUid，带证据）

```sql
CREATE TABLE identity_aliases (
  account_key TEXT NOT NULL,
  uin TEXT NOT NULL, uid TEXT NOT NULL DEFAULT '', peer_uid TEXT NOT NULL DEFAULT '',
  alias_kind TEXT NOT NULL,       -- 'qce-users-lookup' | 'qce-friends' | 'self-info' | 'user-confirmed'
  evidence TEXT NOT NULL,         -- 来源端点+获取时间；不存正文
  confidence TEXT NOT NULL CHECK (confidence IN ('confirmed','probable','pending')),
  created_at INTEGER NOT NULL,
  UNIQUE (account_key, uin, uid, peer_uid, alias_kind)
);
```

`uid`/`peer_uid` 参与唯一键却允许 NULL 时，SQLite 会把多行 `(a, NULL, b)` 视为互不相等而重复插入，
故此处用 `NOT NULL DEFAULT ''` 作缺失哨兵。合并规则：仅 `confirmed`（同键多来源一致或用户确认）
参与身份合并；`pending` 只进待处理清单（V04）；昵称永不作为证据。

### 2.6 `import_manifest`（T12 补导入口预留）

```sql
CREATE TABLE import_manifest (
  import_id TEXT PRIMARY KEY,          -- 文件指纹 sha256
  source_kind TEXT NOT NULL,           -- 'qce-single-json' | 'qce-chunked-jsonl'
  account_key TEXT NOT NULL,
  rows_total INTEGER, rows_ok INTEGER, rows_rejected INTEGER, rows_conflict INTEGER,
  earliest_native_time_ms INTEGER, latest_native_time_ms INTEGER,
  imported_at INTEGER NOT NULL,
  schema_version TEXT NOT NULL
);
```

行级"文件内"ID：`native_id_kind='import-file-line'`，
`native_id = lower(hex(sha256(import_id || ':' || 行号)))`；**不声明跨文件同一消息**（02 §4.2）；
与实时数据合并仅在 `qce-msgId` 证据充足时进行。

## 3. 唯一的严格全序（计划作者裁决 local-core-v1）

```text
canonical_order = (time_ms ASC, local_seq ASC)
```

- `local_seq` 是同一会话内唯一、不可变的正整数，允许间隔。UNIQUE只保证唯一，**不保证连续**。
- 首次入库在消息事务中分配未用过的本地整数；可在写入串行化下使用 `MAX(local_seq)+1`。重复摄取同一原生定位键保留原local_seq。批量初次写入先按确定的输入顺序处理；同秒以已分配的本地键稳定排序，不承诺由此恢复源未证实的亚秒顺序。
- 回补更旧消息也分配新本地键，靠较早time_ms排到前面，**绝不改写已有消息local_seq**。局部消息使用撤回/修订标记而非物理删除，避免键重用；整账号清除同时清其游标/结果。
- 同一会话内作用域固定，因此 `(time_ms, local_seq)` 是严格全序；`(time_ms, 'qq:'+conversation_key, local_seq)` 是同一序的三元投影。
- 当前行的键为 `(t,n)` 且长消息未读完时，以 `(t,n-1)` 为开区间下界。整数中不存在严格位于n-1与n之间的值，所以第一条仍是当前行；**无需n-1对应某一条真实消息**。7、31、90这样的键同样成立，不需要改变batch_engine的减一算术。

### 3.1 补导/修订时的失效（不重排本地键）

- 消息更新与 `data_revision` 递增、完整失效边界的保存同事务完成。
- `earliestAffectedPosition = (earliest_affected_time_ms, earliest_affected_local_seq)`，按完整二元组取最小值。不能只取local_seq的最小值：后来补入的旧消息可能是最早时间、最大的本地序号。
- 新插入旧消息以其新位置为候选边界；修订/撤回以旧、新位置中更早者为候选。保存已有未处理边界与本次候选的最小值。
- 修改过期数据依据的任务在提交前检查revision；旧游标按revision拒绝，按最早受影响批次之前的检查点重建到尾部，缺有效检查点则重建对应模型的会话画像。旧成功结果保留到新结果完整提交。
- 不重排不是不重算。回补改变语境仍需失效和画像重建；原生ID及message_key保持原值。

### 3.2 游标到序的映射（不再各排各的）

| 游标 | 形态 | 与 canonical order 的映射 |
|---|---|---|
| browse | `(anchorMessageKey, direction, offset)` | `anchorMessageKey` → 该行 `(time_ms, local_seq)`；`direction=before` 取 `<` 前 `offset` 条，`after` 取 `>` 后 `offset` 条；`offset >= 0` |
| batch | `(time_ms, 'qq:'||conversation_key, local_seq)` | 三元组第 1、3 元素即 canonical order 的两个键；第 2 元素是作用域标签，用于 `batch_state` 分流而不参与排序 |
| fetch | 见 §4 | 不用位置游标，用窗口 + 已扫描前沿 |

`batch_state.py:23 SHARD` 校验按 `qq:` 前缀分流，**不伪造 `message__message_0.db`，不删校验**（02 §4.3 红线）。

## 4. fetch 续读的唯一解释（RS08）

**窗口推进规则**（每条都可由 `sync_checkpoints` 列复算）：

1. 新任务：`window_start_ms = scanned_through_ms - overlap_ms`（首次为 0 → 由 T03 的历史起点策略给定），
   `window_end_ms = min(now_ms, window_start_ms + 单次最大跨度)`，任务内固定不漂移。
2. 任务只有在探针返回**合法终态**（缓存读完且 `hasNext=false`，即 R2-1 的 `complete` / `complete-empty`）
   时才把 `scanned_through_ms` 推进到 `window_end_ms`。
3. 返回 `partial`（预算/无进展/坏项）时：`scanned_through_ms` **保持不变**，`attempts += 1`，
   `state='PARTIAL'`，`last_task_status='partial'`。下一轮**用同一窗口、更大预算**重读；
   去重靠 `UNIQUE(account_key, conversation_key, native_id_kind, native_id)`，重读幂等。
   `attempts` 达到上限（默认 3）后停在 `PARTIAL` 并让缺口可见，不得静默推进窗口。
4. **窗口 end 不等于扫描边界**：旧稿"提交几页后把下一窗口推进到 end−2 秒"在此规则下不可能发生，
   因为推进只看 `last_task_status`。旧消息积压未读完时，`scanned_through_ms` 就停在已确认处。
5. 不持久化 `page`：QCE 的已加载缓存范围在进程重启后不保证存在（CP06 尚未实测），
   因此续读单位是"整个窗口重读 + 唯一键去重"，而不是伪造一个跨重启的页游标。

`overlap_ms = 2000` 的**依据与限度**（不作完整性证明）：
`msgTime` 是秒级（T01），`ms = sec*1000` 落在该秒起点，同一秒内晚到的消息仍在本秒下界之前，
故 1000ms 覆盖秒截断；余量1000ms仅是尚待实测的暂定值；T01没有提供可量化的时钟偏差证据。
**规则2只确认这次接口扫描范围结束，不证明迟到消息或腾讯历史完整**；把数字调大不增加任何完整性保证（RS08）。

## 5. 身份键与别名迁移（RS06）

- `conversation_key = "u:" || peer_uid`（UID 为锚）。`peer_uid` 缺失时**首版拒绝入库**并计入
  `pending_identity`（不落 `uin:` 形态的键）。理由：一旦允许 `uin:` 形态，后续从 UIN 换到 UID
  就必须迁移已有消息、结果、选择状态与别名，而旧稿只写"待并"未定义过程。首版选择"拒绝不完整身份"，
  并保留 `identity_aliases` 记录证据，待 CP06/好友表分页探查补齐后再决定是否引入 `uin:` 形态。
- **本人身份与 `account_key` 的构造（固定锚，禁止字段出现后换库）**：
  1. 本人UIN来自已核实的 `data.napcat.selfInfo.uin`（其他版本路径须单独验证）。要求ASCII十进制非零字符串，去掉外围空白；原始值保留，规范UIN不含前导零。
  2. `canonical_identity = "qq:uin:" + canonical_uin`；`account_key = "a:" + sha256(canonical_identity UTF-8).hexdigest()[:32]`。
  3. UID仅作有证据的别名，后来出现/暂时缺失都不得改变account_key。缺UIN时拒绝新的在线同步，不降级成另一种账号锚；已有离线库仍可按已选账户浏览。
  4. 文件系统目录使用key中的32位hex，即 `accounts/<account_key[2:]>/`，不把包含冒号的 `a:...` 直接作为Windows目录名。
  5. 同步开始及提交前均取同一来源身份并重算key，发生变化停止该批写入。新增账号与已选离线账号分开处理。
- `forget_account` 只动内存；磁盘删除仅经 `account_store.delete` 且限 `QQVibeData` 数据根内（契约 §1.5）。

## 6. 入库门与守恒

2026-09-29 T04 修正：`qq_normalize.normalize_message(s)` 读取 V6 原始行（elements/textElement 为正文），
`normalize_export` 分单文件和 manifest+JSONL 容器，`read_export` 是有字节/行数上限的本地文件入口。
容器先核验显式 owner、单聊参与者与 peer UID，未知 owner 返回 pending-identity，不推测；分块不能越出选定目录。
导出 ISO 时间保留毫秒与时区，recalled 布尔值没有撤回时间时不伪造时间戳。引用单独保存，缺失引用不会触发网络读取。
固定源码形态的合成 golden 位于 bridge/fixtures/qq/qce6-golden.json；支持 V6 结构，未声称完成真人导出验收。

### 6.1 校验规则

1. 时间：秒→毫秒单一转换点；`time_ms` 必须能由 `raw.msgTime` 反推（V06）。
2. ID：TEXT 全链路；`>2^53` 数值以字符串对照用例固定（X4/V03），本文件 §2.2 的 `native_seq TEXT`
   与测试的往返断言即其落点。
3. 方向：`sender_uin == 本人 uin` 为主，`send_type` 交叉；矛盾进 `conflict` 并计数（V05）。
4. 正文安全：`text/quote/raw` 入库，**不进日志/错误信息/探针摘要**（R32/V40）。
5. 引用、占位、系统消息不计入画像有效文本（V18）。
6. 会话类型：`kind != 'friend'` 的输入直接拒绝（X7/R16）。
7. 统计守恒：每批 `rows_total = rows_ok + rows_rejected + rows_conflict`（R23）。
8. 消息与 checkpoint **同一事务**；`state='COMMITTED'` 与消息可见同生同死（V11/R17）。

### 6.2 撤回判定（唯一规则）

```text
recall_time_raw 缺字段、JSON null、空串或字面量 'null' -> status 不变，形态计入 unknownRecall
recall_time_raw 可解析为整数 i：
    i  >  0  -> status = 'recalled'（撤回时刻=按 §1 单位规则换算的毫秒）
    i == 0   -> status 不变（未撤回）
    i  <  0  -> status = 'conflict'，计入 unknownRecall
```
0 与"字段缺失"都**不**置 recalled；只有严格正值才是撤回（RS06）。

### 6.3 冲突与幂等

- 定位键 `(platform, account_key, conversation_key, native_id_kind, native_id)`；
  同键不同内容 → 写 `message_conflicts`、旧行 `status='conflict'`、`revision` 递增，不覆盖（V10）。
- 同键同内容重复摄取 → `INSERT ... ON CONFLICT DO NOTHING` 语义，行数不变（X1 重摄不增不减）。

## 7. 合成样例（全部虚构）

```text
messages
message_key | conversation_key | native_id            | native_seq            | local_seq | direction | time_ms        | kind | status
m:a1b2...   | u:u_peer_synth01 | 756241784712300001   | 9223372036854775808   |     1     | peer      | 1700000000000  | text | normal
m:c3d4...   | u:u_peer_synth01 | 756241784712300002   | 9223372036854775809   |     2     | peer      | 1700000000000  | text | normal  ← 同秒第二条（X1）
m:e5f6...   | u:u_peer_synth01 | 756241784712300003   | 9223372036854775810   |     3     | self      | 1700000005000  | text | normal
m:g7h8...   | u:u_peer_synth01 | 756241784712300001   |            (同上)     |     1     | peer      | 1700000000000  | text | recalled ← 同 ID 撤回到达：status 变更，不新增行（X6）
```

`native_seq` 那列**存的就是 19–20 位数字字符串本身**；`typeof(messages.native_seq)` 必须是 `'text'`。

```text
sync_checkpoints
conversation_key | window_start_ms | window_end_ms | scanned_through_ms | last_task_status | state     | attempts
u:u_peer_synth01 | 1699999998000   | 1700000000000 |   1700000000000    | complete         | COMMITTED |   0
```

## 8. 版本与迁移

- `PRAGMA user_version` 存 schema 版本（当前 **3**；核心 v2 表保持不变，追加 message_observations 保证状态变化后的重放幂等）。v2 升级先用 SQLite Backup API 保存可恢复副本，再在一个事务中建新表和记录现存消息指纹；失败保留旧库。早期 v2 冲突的规范语义不可完全从旧 raw 还原，升级后第一次见到该版本会补录，随后幂等；本次仅用合成临时 v2 库验证，不迁移用户库。
  本文所有 DDL 只描述 version 2。version 1 从未被产品代码使用（契约未冻结、库未建立），
  因此**不提供 1→2 升级脚本**，只提供 2 的 up/down 对（`DROP` 顺序逆序）。
- 备份/恢复：库级一致性备份（关闭后复制或 backup API），不做 WAL 主文件热拷贝（05 §6）。

## 9. 本文的可执行验证

`scripts/test-migration-contracts.py` 从本文抽取 ```sql 围栏块，在
`PRAGMA foreign_keys=ON` 的内存 SQLite 上执行，并断言：

| 断言 | 覆盖 |
|---|---|
| 建表全部成功 | RS05 |
| 合法插入第一条消息成功 | RS05 |
| 无父会话的孤儿消息被 `FOREIGN KEY constraint failed` 拒绝 | RS05 |
| 同定位键重复摄取不增行；同秒两条不同 `native_id` 都在 | X1/幂等 |
| `typeof(native_seq)='text'` 且原字符串逐字符往返（用 `9223372036854775808`） | RS06/V03 |
| local_seq唯一正整数且不可变；回补保留旧键、使用完整二元位置失效 | local-core-v1/§3.1 |
| `checkpoint` 与消息同事务，回滚后两者都不留痕 | V11/R17 |
| `kind='group'` 被 CHECK 拒绝 | X7 |
| 游标比较器与 `cursor[2]-1` 的 charOffset 演算（同秒、稀疏本地键与末页不足一页的长消息续算） | X8/V37 |

**CREATE TABLE 成功本身不算通过。** 上表任一项失败，本文即不可冻结。

## 2026-09-30 文件导入实现补充（不改变 local-core-v1 DDL）

`qq_import_reader.py` 在后台流式读取单 JSON 的 messages 数组或 manifest 的 JSONL 分块；元数据可在 messages 之后。总量上限为128 MiB、20万行，单个待解码值/JSONL行另有2百万字符的缓冲上限。缺文件、非法路径/reparse、读取期间变化、截断JSON和声明计数不符均失败；坏JSONL行和不合法消息逐行计入拒绝数。

`qq_import.py` 先把原始行和规范行写入临时SQLite，不写账号消息库。使用既有全容器身份规则核对参与者、本人UIN和对方UID；本人未声明时等待明确选择，可手动填写未出现在消息中的本人UIN。缺对方UID时必须补充已核对的UID，不从昵称/内容猜测。

确认提交需要本轮jobId和previewToken；存在拒绝行时还需明确acceptPartial。规范行按原始顺序在同一个消息事务内流式摄取，父会话创建也在该事务内；取消/异常全部回滚，不覆盖原消息或同步checkpoint。按native ID合并，正文相同但ID不同仍保留。已登记同一UID对应不同已知UIN时拒绝合并。预览使用已读取的稳定副本，源文件在预览完成后的变化不改变确认内容。

长事务使用独立消息库连接，避免持有QQSource读锁；读者看到完整旧事务或完整新事务。QQAccountAPI清除账号前取消并排空对应导入，退出前也排空。新账号登记在提交前持久化，若首次提交失败可能保留一个空的可发现账号，不会留下部分消息。预览在进程退出后不续接；再次导入同一文件按原生ID幂等重放。真实同步共同写入的设备验收仍待正式连接器接入；现有“导入后原生行重放/新增”只为合成来源形态验证。

证据：`bridge/test_qq_import.py`，`scripts/smoke-qq-import-volume.py`及第十轮回报。此补充不增加 schema 版本，也不改不可变local_seq、完整失效边界和既有分析重建规则。

## 2026-09-30 同步辅助状态表（核心DDL保持local-core-v1）

`qq_sync_work_v1`是单独版本化的辅助状态表，按`account_key/conversation_key/kind`保存旧历史或近期核对工作，含乐观`revision`和严格验证的JSON窗口、区间栈、覆盖起点、尝试数及上次完成范围。首次创建前使用SQLite备份API保留可恢复副本；不改消息表、不可变local_seq、前向checkpoint或user_version。建表和写入使用事务；消息摄取与辅助revision一起提交，旧revision发布必须整体回滚。

库读取/备份/恢复校验辅助表形态、账号与payload；恢复备份时清除辅助覆盖，重新扫描，避免把新库的进度套到旧内容上。移除选择不删除消息与持久进度，重新选中后从保存窗口恢复。表和验证器定义以`bridge/qq_sync_work.py`为唯一实现来源；第十二轮报告及`bridge/test_qq_history_sync.py`记录恢复、取消、密集窗口和迟到消息反例。

## 2026-09-30 作者正文与展示元数据（normalize version qq-v3）

raw元素的`textElement:null`表示未含该元素，不是坏文本；非null的错误结构仍拒绝。QCE导出的非空`content.elements`有结构时，仅拼接text元素`data.text`作为作者正文，引用从独立reply数据读取；不能把渲染预览`content.text`里的媒体/回复标签当作对方写作。没有结构元素的既有V6平面格式继续用声明的纯正文。仅媒体/仅引用且无作者文本的行保持unknown占位，保留原始类型与raw，不扩大msgType映射。

qq-v3标识此正文提取规则修正；库DDL/user_version不变。已有旧规则导入行需重导或原生扫描重放，触发既有同ID修订与分析失效；本轮未批量改用户库。`qqDisplay`是受限的只读展示投影，不入库、不改分析正文；字段与范围见source-contract的第十三轮补充。


## 2026-09-30：接入/补导来源辅助表v1

核心schema_version仍为3，核心messages/conversations/checkpoint字段、immutable local_seq、去重和分析语义不改。首次有带来源的正式写入时，先用SQLite Backup API保存唯一 `.ingest-audit-backup-*`，随后一个事务创建versioned辅助表与索引；读取旧库不创建任何表。辅助DDL由 `bridge/qq_ingest_audit.py:DDL` 唯一定义，独立测试真实执行、拒绝污染结构、外键/跨scope、迁移失败与回滚。备份验证也核对辅助DDL及run/消息scope；旧备份没有这些表依然可恢复，已含来源的备份一并保存和恢复。

- qq_ingest_runs_v1：单账号/单会话每批持久记录，含来源分类/格式、解析快照指纹或接口版本、状态/安全原因、请求区间、记录区间、事务完成前取的时间、输入/拒绝/结果计数与数据代次。含每个成功空窗口，也保存partial/error，与实际检查点及消息同次事务提交。
- qq_ingest_observations_v1：每批/每消息/每内容指纹唯一，关联run及messages外键，记录规范化版本和首次入库/重复/撤回/修订/冲突处置。指纹复用原message_observations语义，不改变正文选择规则。同批重复折叠来源观察，但received/unchanged计数保留原接收数量。
- idx_qq_ingest_runs_scope、idx_qq_ingest_observations_message：分别服务会话历史和逐条来源分页；全部批次保留，读取每页最多50批、每消息每批最多5个版本样本，并明示总数。

取消、身份变化、选择移除、检查点或审计写入失败都会撤销同次事务的消息、版本观察、来源关联和run。辅助表迁移本身可先完成，但不得留下假成功批次。旧消息原始来源没有证据时标未记录；后续实际重复读取只能新增当次观察，不宣称恢复了第一次来源。


F23索引统计补充（同日）：`qq_ingest_message_sources_v1`每消息保存已实际观察的来源bitset（forward1/history2/reconcile4/file-import8）与账号/会话，和完整观察表在同事务更新。覆盖索引`idx_qq_ingest_message_sources_scope`用于只读计数，避免界面每次遍历全部重复观察再DISTINCT。它不是独立事实源；备份/恢复用双向EXCEPT核对与完整观察日志导出的集合完全一致，污染/多余/缺失投影拒绝。本轮所有创建和迁移检查仅在临时合成库执行，未检查或改动真实用户库。
