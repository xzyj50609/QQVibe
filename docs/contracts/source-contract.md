# 来源契约（R2-3 修正版，基线 1.2.2）


> 补齐被生产代码实际调用而旧稿漏写的方法（§2.0）、把 DTO 改成生产代码里真实存在的键（§2.2/§7）、
> 删除凭空的 `lastCommitSeq`、把三游标收敛到**同一个**严格全序（§4）。
> **§2.0 的方法集合与 §2/§7 的 `dto` 块由 `scripts/test-migration-contracts.py` 与生产代码 AST
> 自动比对**——少一个方法或多一个字段即测试失败。测试通过前本文件仍是待复核草案，
> 不授权实现 QQSource；仅 08 §5 的独立 T03-A 放行。下文原状态说明保留追溯。

日期：2026-09-28。基线：WechatVibe **1.2.2 / `02b7708`**（QQVibe 合并提交 `50b8bfb`）。

> **状态：随 G1-resubmission 提交，待计划作者复核。** 本文件按 07 RV07 要求补齐了方法签名/返回结构/异常、数据表与唯一键（见 [data-schema.md](data-schema.md)）、同步状态机、三游标、补导 revision 失效、清理/取消、`qq` 来源扩展与详情 DTO。标注 **[实机待证]** 的条目依赖 P03 的 CP04/CP06 实测（协议见 P03-sync-evidence-protocol.md），不阻塞契约评审，但阻塞 T03 之后对应实现项的验收。
>
> 行号均为提交 `50b8bfb` 时点；后续一律按符号重定位（锚点表见 baseline.md §8）。草案 早期来源契约草案 保留为历史，不再维护。

## 1. 消费者清单（按 1.2.2 重新定位）

### 1.1 来源类型判断与访问点

| 1.2.2 锚点 | 现状 | QQ 适配动作 |
|---|---|---|
| `backend_service.py:371` | `isinstance(self.source, WeChatSource) or self.source.db is not None`（health 门） | 改为来源能力查询；QQ"已连接无消息"是正常空态，不是 503 |
| `backend_service.py:1652` | 第二处 `isinstance(self.source, WeChatSource)`（`message_windows` 分流） | QQSource 实现 `require_messages_ready()` 后走鸭子类型分支 |
| `backend_service.py:154,210,213` | `forget_account` / `close` 鸭子调用（账号清理路径） | QQSource 实现同签名；清理范围限 `QQVibeData`（R19/R20） |
| `backend_service.py:280,323,347` | `verified_identity` 鸭子调用 | 同签名同异常语义（见 §2.2） |
| `backend_service.py:327,351` | `require_messages_ready` 鸭子调用 | 见 §2.3 |
| `backend_service.py:802,1423` | `getattr(self.source, "lock", None)` | QQSource 提供 `threading.RLock` |
| `backend_service.py:926,1478` | `request_scope` 鸭子调用（`:927,1480,1582` 以 `nullcontext` 兜底） | QQSource 提供上下文管理器（可为轻量 no-op） |
| `real_http.py`（应用装配点） | 构造 `Backend(WeChatSource(...))` | QQ 模式显式注入 QQSource；禁止微信探测/密钥扫描 |
| `real_backend.py` | 引用 WeChatSource | 装配层分流 |

### 1.2 历史浏览/搜索的直接内部调用

> 2026-09-29 第四轮实现：普通 history、search 与 API insights around 三处已按来源分流到 qq_history_browser；QQ 模块只通过 read_library(expected_account) 的受锁/代次边界读取规范库，复用的 date_bounds 只是纯日期转换。分页/搜索/around 的行为回归见 test_qq_history.py。账号管理和实机闭环仍按当前进展单独验收。

`backend_service.py:38` 导入 `history_browser.browse/saved_results/search`，三个调用点：`:602`（insights around）、`:1669`（history 分页）、`:1689`（search）。

**关键事实（RV07 复核确认）**：`history_browser.py` 不是来源无关的——它直接使用 `source.lock`、`source._db(fresh=True)`、`source._contacts(db)`、`source.self_user(db)`、`source._render_row(...)`、`source.issued_images`（LRU，`:137-140`）。因此：

- `history_browser.py` 保留为微信专用实现，不改造成双平台；
- 新增 `qq_history_browser.py`，只读本地规范库（[data-schema.md](data-schema.md)），输出同样的容器形态（`{"messages": [...], "hasMoreBefore": bool, ...}`，见 history_browser.py:228/:257 的现有形状）；
- `:602/:1669/:1689` 三个调用点按来源分流；QQ 的返回容器字段与微信版一致（`messages/hasMoreBefore/hasMore` 等），保证 Backend 下游不改。

### 1.3 微信 ID 形态耦合

| 1.2.2 锚点 | 现状 | QQ 适配动作 |
|---|---|---|
| `backend_service.py`（11 处）、`batch_engine.py`（2 处） | `endswith("@chatroom")` 群/友分支 | 改为会话来源显式 `kind` 字段（`conversation.kind='friend'`）；QQ 单聊恒为单聊，不靠后缀推断 |
| `batch_engine.py:24` `SHARD = re.compile(r"message__message_\d+\.db\Z")` | 微信分库文件名校验 | QQ 批次位置第二项用 `qq:<conversationKey>` 显式标识（见 §4.3），微信校验原样保留 |
| `chat_server.py:12` | 文本前缀剥离 `wxid_*`/`*@chatroom:` | QQ 文本清洗规则按实测样本版本化，不套用微信正则 |

### 1.4 `.db` / `.lock` / `_db` 引用（10 个 bridge 文件）

与草案 §1.4 结论一致：逐个区分"微信消息库"与"本软件自身存储"。QQSource 及其数据全部走 `QQVibeData` 独立数据根（[data-schema.md §2](data-schema.md)），不触碰任何微信 `.db` 分库；`model_source.py` 的 `.db` 属模型库校验，与消息无关。

### 1.5 账号清理

> 2026-09-29 第五轮：QQ 装配独立 QQAccountAPI/QQAccountStore，仅登记已确认UIN与规范库路径；账号选择接受 a:hash 后仍只用SHA-256文件名。当前账号停任务/关连接后清除，非当前账号不影响当前库；持久化清理中间态，搬移失败回滚、删除未完显式重试，不触碰微信快照或密钥。同步协调器 stop_account 尚待T06实际注入。已登记离线库可显式activate；不以此动作连接QCE。回归见 test_qq_accounts.py。

`account_store.py:287 delete(identifier, *, guard, forget)` 为唯一删除入口。QQ 顺序：**停同步 worker → 冻结连接 → 删 `QQVibeData/accounts/<account_key[2:]>/` → 通知 forget 回调**（`backend_service.py:154,210`）；删除前做绝对路径规范化与 reparse 检查（R19）。

### 1.6 更新器（必改项）

上游更新器会把 QQ 版覆盖回 WechatVibe（R35，06 §"上游 1.2.2 对迁移的影响"第 5 条）。T03 必须：QQ 模式禁用应用内更新安装通道，版本页显示"尚未配置独立更新源"。

## 2. QQSource 接口契约

QQSource 实现消费者实际调用的**全部方法**（消费者全部按鸭子类型调用），线程模型为"一把
`RLock` 串行化本来源的全部读状态"。

> **RS07 的根因与本轮修法**：旧稿 §2 只写了 10 个方法，而 `backend_service.py` / `real_http.py`
> 实际调用了 18 个。本轮不是"把实现全部公开"，而是**逐个给出签名与返回结构**。
> `scripts/test-migration-contracts.py` 用 AST 从生产代码反查所有 `self.source.X()` /
> `backend.source.X()` / `source.X()` 调用点，要求 X 必须出现在下方 ```python 块里；
> 并要求本文件 ```dto 块声明的键集合与生产代码里真实 `return {…}` 字面量的键集合一致。
> **少一个方法或多一个字段都算测试失败**，不靠自述。

### 2.0 方法全集（AST 覆盖闸门）

```python
def identity(self) -> tuple[str, Path]
def verified_identity(self, *, messages: bool = False) -> tuple[str, Path]
def require_messages_ready(self) -> None
def conversation_coverage(self, conversation_key: str) -> dict
def sessions(self) -> dict
def contact(self, user: str) -> dict
def messages(self, user: str, limit: int, offset: int = 0) -> MessageWindow
def message_windows(self, users: list[str], limit: int = 80,
                    expected_account: str | None = None) -> MessageWindowBatch
def texts_for_refs(self, user: str, refs: list[tuple], with_ids: bool = False) -> list
def media(self, user: str, stable_id: str) -> tuple[bytes, str] | None
def history_highwater(self, user: str) -> tuple[int, str, int] | None
def history_page(self, user: str, highwater, after=None,
                 page_size: int = 256) -> tuple[list[dict], tuple | None]
def quoted_history_page(self, user: str, ceiling, after=None, page_size: int = 64,
                        member: str | None = None) -> tuple[list[dict], tuple | None]
def preceding_text_context(self, user: str, before, limit: int = 3) -> list[dict]
def stats(self, user: str, member: str | None = None) -> tuple[int, int, list[dict]]
def profile_metadata(self, user: str, member: str | None = None) -> dict
def profile_overview(self, user: str, member: str | None = None, highwater=None) -> dict
def invalidate_profile_metadata(self, user: str | None = None) -> None
def request_scope(self) -> AbstractContextManager
def binding_token(self) -> tuple
def read_library(self, expected_account: str) -> AbstractContextManager
def data_scope(self, account: str, user: str) -> dict
def ingest_history(self, account: str, user: str, *, before=None, limit=20, message_cursor=None, kind=None) -> dict
def forget_account(self, account: str) -> None
def close(self) -> None
```

`lock` 不是方法而是属性：`lock: threading.RLock`（`backend_service.py:802,1423` 用
`getattr(self.source, "lock", None)` 取）。

`kind: str` 与 `media_reason: threading.local` 也是属性。QQ 的 `kind='qq'`；每次 `media()` 在调用线程设置
`media_reason.value='qq-media-not-implemented'`，HTTP 消费者从该属性读取原因，不调用 `media_reason()`。

### 2.1 身份与会话

```text
verified_identity / identity
  返回 (account_key, data_root)
    account_key = "a:" + sha256(("qq:uin:" + canonical_uin).encode("utf-8")).hexdigest()[:32]  # data-schema §5
    data_root   = Path(QQVibeData/accounts/<account_key[2:]>/)  # Windows目录只使用hex部分
  异常 AccountUnavailableError  —— 账号未就绪/身份不可得
       AccountChangedError      —— 读取期间身份与打开时不一致
```

- 本人身份比较**只比重算后的 `account_key` 字符串**，不比 `selfInfo.uin` 原值与 hash
  （RS06：旧稿把两者混为一谈）。`messages=True` 且本地库未就绪时抛 `MessagesUnavailableError`。
- 本人账号键固定UIN，UID只作别名。缺UIN时停止新的在线读取，不因UID后来出现而换库；离线账号仍按已选持久身份浏览。

```text
require_messages_ready()                       # 无参数：账号级本地库可读门
  就绪返回 None；未就绪抛 MessagesUnavailableError
  语义 = "本账号的本地规范库已建立且可读"。不要求 QQ 在线（F17 离线浏览），
         不要求任何会话已有消息（合法空会话必须通过）。

conversation_coverage(conversation_key) -> dict # 单会话覆盖状态，与上面**分开**
  {"conversationKey": str,
   "covered": bool,           # 该会话是否至少成功同步过一次（complete/complete-empty）
   "state": str,              # sync_checkpoints.state
   "scannedThroughMs": int,
   "lastTaskStatus": str,     # complete | complete-empty | partial | error
   "partialReason": str|None,
   "attempts": int}
  未登记的会话返回 covered=False，不抛异常。
```

- RS07 的裁决：旧稿用无参的 `require_messages_ready()` 同时表达"账号库就绪"和"该会话同步过"，
  二者必须分开。**消费者仍只见无参版本**（`:327,351,1652` 不改签名）；会话覆盖是新入口。

```dto sessions.top
self,sessions,account,messagesReady
```

```dto sessions.item
username,displayName,name,avatar,avatarCandidates,preview,time,sortTimestamp,unreadCount,lastMsgType,lastMsgSubType,pinned,lastSender,isGroup
```

```dto contact
name,avatar,avatarCandidates
```

- `sessions["self"]` = `{"username": <本人 id>, **contact_display(...)}`；顶层
  `account`/`messagesReady` 是真实存在的字段（旧稿漏写）。
- 联系人显示键是 **`name`/`avatar`/`avatarCandidates`**（`wechat_source.py:82-86` 的
  `contact_display`），不是 `displayName` 单键。QQ 项的 `username` 用 `conversation_key` 填充，
  `displayName` 作为兼容别名等于 `name`；头像来自好友表，缺失时为空串占位。
- `isGroup` 在 QQ 单聊恒为 `False`（不再靠 `@chatroom` 后缀推断，§1.3）。

### 2.2 消息读取

```dto message.row
id,historyCursor,side,text,kind,time,type,senderId,senderName,senderAvatar,senderAvatarCandidates
```

- 这是 `wechat_source._render_row` 真实产出的键（`backend_service.py:1645,1658,1672,1686`
  在序列化时剔除 `_` 前缀的内部键）。旧稿写 `sender`/`isSelf` 是**不存在的字段**。
- `direction='peer'` 投影为 `side="other"`；`'self'` → `"self"`；`'system'` 行在渲染层返回
  `None` 而被丢弃（微信版行为），QQ 必须同样丢弃而不是渲染成空文本。
- `time` 为 epoch **毫秒**（`_render_row` 里对 <1e10 的秒值乘 1000，QQ 侧 `time_ms` 已是毫秒，
  直接赋值，不再二次判断）。
- `historyCursor` 是 `encode_cursor(account, user, position)` 的不透明串；QQ 版的 `position`
  三元组见 §4.3。`kind ∈ text|image|other`（展示用粗粒度），`type` 是映射表给出的细粒度名。
- 内部键 `_sort` = `position` 三元组，只给 Backend/history_browser 用，不出 HTTP。

```text
messages(user, limit, offset=0) -> MessageWindow(list)
  .has_more_before: bool          # 本会话在本次截取之前是否还有更早消息
message_windows(users, limit=80, expected_account=None) -> MessageWindowBatch(dict)
  dict[user] -> list[message.row]
  .has_more_before: dict[user -> bool]     # 逐会话元信息，不是裸 list
```

- **`has_more_before` 必须保留**（RS07）：单会话是 bool、批量是逐会话 dict，两种容器不能混。
  `message_windows` 还必须在 `expected_account` 与实际账号不一致时抛 `AccountChangedError`，
  且整批读取全程处于账号校验括号内（进批校验、出批复校验）。
- `texts_for_refs(user, refs, with_ids=False)`：`refs` 是 `(position_scope2, local, stable_id)`
  三元组列表（微信版即 `(shard, local_id, stable_id)`）；返回文本列表，
  `with_ids=True` 时返回 `(stable_id, text)` 对。缺引用不报错，返回可少项。
- `media(user, stable_id) -> (bytes, mime) | None`；`None` 时 `media_reason.value` 给出脱敏原因码
  （如 `invalid-id`、`local-key-unavailable`）。QQ 首版无媒体时固定返回 `None` +
  `media_reason.value='qq-media-not-implemented'`，**不探测微信**。
- `quoted_history_page(user, ceiling, after=None, page_size=64, member=None) -> (page, next_after)`
  ；`ceiling is None` → `([], None)`。`page` 元素是 `message.row`。
- `preceding_text_context(user, before, limit=3) -> list[{id,side,text}]`（只有这三个键）。
- `history_highwater(user) -> (time_ms, 'qq:'||conversation_key, local_seq) | None`；`None`=空会话。
- `history_page(user, highwater, after=None, page_size=256) -> (page, next_cursor)`；
  `highwater is None` → `([], None)`；`next_cursor` 同三元组形态。

```dto history.page
account,user,messages,results,hasMoreBefore,hasMoreAfter,nextCursor,oldestCursor,newestCursor,focusId
```

```dto search.page
account,user,messages,hasMore,nextCursor
```

- 旧稿只写"等字段"。上表是 `backend_service.py:1664-1674` / `:1683-1691` 实际返回的**全部**键，
  逐键列出并由测试比对。

```text
request_scope(self) -> AbstractContextManager
```

- 语义：一次 HTTP 请求内复用同一读取上下文，并在退出时**实际校验代次**。
  QQ 版不得写成 no-op：上下文进入时记录 `(account_key, db_generation)`，退出时重查，
  变化则抛 `AccountChangedError`（`backend_service.py:927,1480,1582` 的 `nullcontext` 兜底
  是"允许但不推荐"，QQ 必须提供真实现）。

### 2.3 统计与画像

```text
stats(user, member=None) -> (count: int, text_count: int, members: list[member.ref])
  member=None 时 members 对单聊为 []（微信版仅群聊非空）
profile_metadata(user, member=None) -> {"contact": contact, "members": [member.ref],
                                        "count": int, "textCount": int}
profile_overview(user, member=None, highwater=None) -> 同形状，但 textCount 恒为 None
  （overview 不重算文本统计；highwater 用于有界刷新）
invalidate_profile_metadata(user=None) -> None
  公共失效入口：user=None 清全部，否则只清该会话。必须真实存在。
```

```dto member.ref
id,name,avatar,avatarCandidates
```

> 已实现，`backend_service.py` 的清理路径改为无条件调用这个公开入口，私有属性访问已删除。
> 行为断言在 `bridge/test_api_insights.py`（本地来源清理计一次公开失效、API 来源清理不计），
> 闸门项 `test_profile_invalidation_goes_through_the_public_entry` 取代原"已知债务见证"测试。

- **RS07 遗留的第二处缺口**：`backend_service.py:1168` 现在靠
  `hasattr(self.source, "profile_metadata_cache")` 直接 `.clear()`。QQSource 不提供同名私有属性，
  该分支就会**静默不失效**（缓存留着旧统计继续用）。因此 T03/T08 必须把该调用点改为
  "优先调用 `invalidate_profile_metadata()`，缺失时才回退属性清理"。**这是生产代码改动，
  契约本身修不了它**，在此明确列为必做项而不是留一句"提供公共入口"。

### 2.4 生命周期

```text
forget_account(account) -> None   # 只清该账号内存缓存与未提交状态，不删磁盘
close() -> None                   # 释放连接/线程，幂等
```

- `forget_account` 对未知 account 是 no-op（不抛）。磁盘删除只走 `account_store.delete`（§1.5）。

### 2.5 明确不进入 QQSource 的方法

`_db / _contacts / self_user / _render_row / issued_images / profile_metadata_cache` 是
WeChatSource 的私有实现。QQSource **不得**提供同名私有成员给 `history_browser.py` 使用；
`qq_history_browser.py` 直接读规范库，不经由这些内部接口。唯一例外是 §2.3 末条：调用点必须
改为公开入口，而不是让 QQSource 补一个假属性来迁就旧代码。

## 3. 同步状态机

状态（每个 `(accountKey, conversationKey)` 一条，持久化于 `sync_checkpoints`）：

```text
IDLE ──(轮询到期/手动触发)──► FETCHING ──(规范化成功)──► STAGING
STAGING ──(事务提交成功: messages+checkpoint 原子写入)──► COMMITTED ──► IDLE
FETCHING/STAGING ──(任何失败)──► ERROR(lastError, 保留旧 checkpoint) ──(退避后重试)──► FETCHING
任意态 ──(断线检测)──► DISCONNECTED ──(重连+身份核验成功)──► CATCHING_UP(按 checkpoint 带重叠回读)──► COMMITTED
CATCHING_UP ──(达预算仍有缺口)──► PARTIAL(缺口范围可见, 状态明示) ──► IDLE(下轮续补)
```

不变式：

1. **checkpoint 只能与消息同事务推进**（R17/V11）；提交失败回滚两者。
2. `PARTIAL`/`ERROR` 不计入"最近同步成功"时间显示；"API 请求成功"≠"历史完整"（02 §6.2）。
3. 提交前重新核验本人账号与连接代次；代次变化则整批丢弃（V22，R21）。
4. 同一会话同一时刻至多一个同步任务；新任务先取消旧任务并等待退出（R18）。
5. 轮询参数起步值：当前会话 4s、其他已选会话 15–30s，受全局请求预算约束；**禁止**每轮全量 forceRefresh（R05；固定窗口见 §4.1 与 P02 实验结果）。

错误分类（对应探针 P01 的阶段划分）：`auth`(401/403 → 停止并提示配置，不重试) / `http`(超时/5xx → 有界退避) / `business`(success=false → 记录错误码，不当作空结果) / `protocol`(结构缺失 → partial/error，绝不静默吞)。

## 4. 三游标契约（不得共用一个 lastMessageId）

三个游标的**排序语义只有一个来源**：`data-schema.md §3` 的
`canonical_order = (time_ms ASC, local_seq ASC)`。RS08 的裁决是"两者必须映射到同一个严格全序"，
因此本节不再出现第二套排序键。

### 4.1 fetch cursor（QQ 拉取检查点）

- 形态：`{version, platform:"qq", accountKey, conversationKey, windowStartMs, windowEndMs,
  scannedThroughMs, lastTaskStatus, attempts, overlapMs, state, partialReason}`。
  epoch **毫秒**，单位与截止时间显式入字段。
- **没有 `lastCommitSeq`**：QCE 6.3.0 的 `fetch` 响应里没有可持久化的上游提交序号
  （T01 §8 + 本轮源码核对：只有 `messages/totalCount/currentPage/totalPages/hasNext/cacheHit/fetchedAt`）。
  旧稿写该字段属凭空（RS08）。续读状态由窗口 + 已扫描前沿 + 任务终态表达。
- 推进规则（逐条可复算，见 [data-schema.md §4](data-schema.md)）：
  1. `windowStartMs = scannedThroughMs - overlapMs`；任务内 `windowEndMs` 固定不漂移。
  2. **只有探针给出合法终态**（`complete` / `complete-empty`，即缓存读完且 `hasNext=false`）
     才把 `scannedThroughMs` 推进到 `windowEndMs`。
  3. `partial` 时 `scannedThroughMs` 不变、`attempts+=1`、`state='PARTIAL'`，下一轮**同窗口大预算**
     重读；去重靠定位键，重读幂等。达上限（默认 3）后停在 PARTIAL 并暴露缺口。
  4. 因此"提交几页后就把下一窗口推进到 end−2 秒"在本规则下不可能发生：推进只看终态，
     不看请求的窗口 end。
  5. 不持久化 `page`：QCE 已加载缓存范围跨重启不保证存在（CP06 未实测），不得伪造跨重启页游标。
- `overlapMs=2000` 只是**秒截断与时钟差护栏**，不是完整性证明；把数字加大不增加任何保证。
- 放大防护：显式 `batchSize` + `maxPages/maxMessages/maxFetchRequests/maxTotalRequests/maxSeconds`
  预算随任务携带。R2-2 后 `Budget` 对**每一次** HTTP 请求过账；重复页/无新 ID/结构缺失/坏项 →
  `partial`，绝不 completed（P01 探针 R2 行为即此契约的参考实现，由 `probes/test_qce_probe.py` 锁住）。

### 4.2 browse cursor（本地历史浏览游标）

- 形态：`(anchorMessageKey, direction, offset)`，`direction ∈ {'before','after'}`，`offset >= 0`。
  只引用本地库主键；非法值、跨账号/跨会话值抛 `ValueError("invalid cursor scope")`。
- 解析：`anchorMessageKey` → 该行的 `(time_ms, local_seq)`；`before` 取严格 `<` 的前 `offset+limit`
  条中最旧的 `limit` 条，`after` 取严格 `>` 的后 `limit` 条。
- **稳定序 = `canonical_order`**（`data-schema.md §3`）。旧稿的
  `stable_tie_key = sha256(nativeId)[0:16]` 已**删除**：64 位截断的两行碰撞未被排除，
  而 `local_seq` 由 `UNIQUE(account_key, conversation_key, local_seq)` 保证会话内唯一，
  与 `time_ms` 组合后是**可证明**的严格全序。
- 同秒两行（X1）由 `local_seq` 定序，跨页不遗漏；比较器由测试实际运行，不靠叙述。

### 4.3 batch cursor（分析批次位置，兼容现有 batch_state/batch_engine）

- 三元结构 `(seq, scope2, local)` 保留，映射为
  **`seq = time_ms`、`scope2 = "qq:" || conversationKey`、`local = local_seq`**。
  第 1、3 元素正是 `canonical_order` 的两个键，第 2 元素只是作用域标签，不参与排序——
  这就是 RS08 要求的"两者映射"。
- `batch_state.py:23 SHARD` 校验按 `qq:` 前缀分流：`^qq:.+$` 走会话键分支，微信分支原样保留，
  **不伪造 `message__message_0.db`，不删校验**（02 §4.3 红线）。
- `charOffset` 续算保留现有 `cursor[2]-1` 算术。`local_seq`是不可变正整数且可有间隔；(t,n-1)为开区间下界时仍包含(t,n)，整数间无其他取值，**不要求n-1对应真实前一行**。
- `history_page`只在空页返回 `([], None)`；即使最后一页不足page_size，只要有消息就必须返回该页最后一条的游标。现有batch_engine会在next_cursor为None时先break，不能以None表示“本页有数据但已到尾部”。
- 回补不重排local_seq；以完整 `(time_ms,local_seq)` 位置记录失效边界并递增data_revision，旧代次任务与游标拒绝后重建受影响上下文。
- 外部游标携带 `version/platform/accountKey/conversationKey/dataRevision`；当前 QQ 对外游标 version=2，旧版本、跨会话或过期 dataRevision 明确拒绝（stale-history-cursor 提示刷新）。发行前的 version=1 不继续接受。内部分析位置仍为三元组，不改减一算术；本地 Laya 与 API 模型的代次保护、暂存重建和原子替换已在第七、八轮实现并离线验证，不能把这些合成证据当成真实接入和实际模型验收。

## 5. 去重、冲突与 revision 失效

- 定位键：`(platform, accountKey, conversationKey, nativeIdKind, nativeId)`；`nativeIdKind`
  首版只有 `"qce-msgId"`（19 位字符串，T01 CP08）。OneBot 若引入属新 kind，需独立证据才建
  alias（D05/R08）。
- 同 native ID 内容不一致：按 `data-schema.md §6.2` 的唯一撤回规则区分
  `recalled`（`recallTime` 解析为整数且 **>0**）/ `revised` / `conflict`，保留双方记录与来源、
  置分析失效；不静默覆盖（V10）。`0`/缺字段都不是撤回。
- 每会话维护dataRevision及完整二元earliestAffectedPosition。旧记录插入、正文/时间修订和撤回取最早受影响位置；变更与revision/边界同事务提交。不能把整数local_seq单独当成时间先后。
- 旧revision任务提交前失效；从最早受影响批次之前的有效检查点重建到尾部，找不到有效检查点则重建该会话对应模型画像。原结果保留为旧依据，完成后原子替换，不重复累计摘要。
- 缓存复用必须模型来源/版本、正文、方向和所需上下文指纹相同。不重排本地键不意味着回补可以不重算。

2026-09-29 实施状态：本地分支按账号/会话/模型版本/dataRevision隔离暂存结果，保留已发布结果至重建成功；现有批次格式缺少独立可验证的任意位置检查点，因此修订后走整会话重建回退。正常追加继续增量，失败可从该新代次片段恢复，恢复备份也必须提升dataRevision。API标签、画像和盘点的对应保护已在第八轮接入，标签额外比较文本/方向/顺序与画像摘要上下文指纹；历史标签GET/POST共享around锚，过期锚要求刷新。详见第七轮及第八轮。全部为合成验证，真实接入与实际模型闭环尚未完成。

## 6. `qq` 来源枚举扩展（RV08，T03/T08 实施点）

1.2.2 两端枚举与 Backend 写死点（符号锚点见 baseline.md §8）：

| 位置 | 现值 | 改动 |
|---|---|---|
| `bridge/message_input.py:28 SOURCE_KINDS` | `{wechat, ocr, unknown}` | 加 `qq` |
| `shared/message-input.ts:17 MessageSourceKind` / `:64 SOURCE_KINDS` | 同上 | 同步加 `qq`（两端同票，不同步即校验失败） |
| `bridge/backend_service.py:644,1865` | `source_kind="wechat"` 写死 | 由装配/来源接口传入 `source.kind`；QQ 路径传 `qq` |

约束：QQ 消息**不得**伪装成 `wechat/ocr/unknown` 过校验；`build_input_record(..., source_kind="qq")` 在扩展后必须通过现有毫秒时间/身份/引用一致性校验（`message_input.py` 其余校验原样保留，不另建绕开它的输入格式——02 §5）；`inputMeta` 不丢弃。

## 7. 详情 DTO（D10：简洁默认 + 有依据的详情）

> **本轮新发现的 RS07 同类缺陷（08 未列出，见 `G1-resubmission-2.md` §4）**：旧稿写
> 行级是 `{emotion: label|null, intent: label|null}`、详情是**单个** `candidates` 数组。
> 实际生产代码不是这样：
> - `chatui/message-labels.js:12-25` 消费的是 `view.emotions` / `view.intents`（**两个复数数组**）；
> - `chatui/message-insight-adapters.js:20-26,55-65` 产出 `{emotions, intents}`；
> - `electron/local-message-insights.ts:16-18` 的 `FineMessageInsight` 是
>   `{emotion: LabelScore[], intent: LabelScore[], intentBroad: LabelScore[]}`（**三个打分数组**）。
> 即"没区分情绪与意图"在旧稿里被低估了：真实代码本来就分两路，只是**行级视图与底层打分数组
> 的名字单复数不同**。契约必须按真实名字写，否则实现者会去新增一套并行结构。

```dto insight.row
emotions,intents
```

```dto insight.fine
emotion,intent,intentBroad
```

```dto detail.local
emotionCandidates,intentCandidates,kaomoji,modelSource,analysisVersion,scope
```

```dto detail.api
emotionLabels,intentLabels,modelSource,analysisVersion,scope
```

```dto detail.candidate
label,rawProbability
```

```dto detail.modelSource
kind,id,status
```

```dto detail.scope
accountKey,conversationKey,messageKey
```

- 行级视图 `insight.row`：`emotions`/`intents` 各为 `[{label}]`，**≤3 项**，首项为主标签。
  这是 1.2.2 现行前端契约，不改名。
- 底层打分 `insight.fine`：`emotion`/`intent`/`intentBroad` 为 `LabelScore[]`
  （`shared/contracts`）。QQ 版沿用同一结构，不另造。
- 本地详情 `detail.local`：**情绪与意图是两个独立数组**（旧稿单个 `candidates` 作废），
  各自 ≤3 项、按**未重新归一化**的原始概率降序；`rawProbability` 取自已有结果/缓存的原始分布
  （`electron/local-message-insights.ts` 仍返回完整候选分布，`chatui/message-insight-adapters.js`
  只在行级隐藏概率——1.2.2 已具备数据条件）。`kaomoji` 可缺省。
- `detail.scope` 必带 `accountKey/conversationKey/messageKey`，另加 `analysisVersion` 与
  `modelSource.kind`；`kind ∈ {"laya","api"}`。**API 详情不造概率**：无候选分布时
  `intentCandidates/emotionCandidates` 缺省为空数组，绝不补数字（V44）。
- 第九轮实现保持行级DTO不变，详情由独立`localDetails/apiDetails`适配器生成。`scope.messageKey`取现有消息行`id`，与账号/会话组成唯一范围，不改规范库内部哈希键。API未提供分析版本时该字段为null，界面明确说明；本地来源ID取实际配置返回值，不硬写为`local`。
- 行为：展开详情不发模型请求、不改画像/统计；切换会话/账号/模型后详情立即失效清空
  （经 §2.3 `invalidate_profile_metadata()`）；键盘可开合；长内容滚动不挤压聊天行（V44/UI11）。

## 8. 连接与进程契约

- 连接目标仅允许本机回环（`http://127.0.0.1:40653` 默认）；不提供 `0.0.0.0`/非回环配置入口。令牌存现有安全配置机制（`runtime/` 下的受控存储），不进日志/Git/renderer；401 视为配置错误，不换传递位置重试（P01 同口径）。
- QCE/NapCat 是外部进程：QQVibe 只连接，不停止不升级（V42：不按名字/端口杀外部进程）；沿用 1.2.2 进程所有权模式管理**自身** bridge（`before-quit` + `--stop-owned-bridge`）。
- 版本支持表：QCE 6.3.0（实装）+ 参考 `7fcca888`；QQ `9.9.29.47354` 实测。升级 QCE 前必须复跑契约套件（R06）；组件详细构建号 [实机待证]。
- 许可：QCE GPL-3.0 以外部依赖方式使用，不捆绑进 QQVibe 包（D07/R37）；最终分发清单在 G4 复核。

## 9. 合成反例（契约级；T04/T05 落为 V09/V12/V39 fixture）

| # | 输入 | 必须结果 | 违反即 P1 |
|---|---|---|---|
| X1 | 同一秒两条 msgId 不同的"好"（重复摄取两遍） | 库中恰好 2 条；重摄不增不减 | 正文哈希/时间去重把两条并成一条 |
| X2 | 昵称相同、UIN 不同的两个联系人 | 两个 conversationKey，画像/缓存互不可见 | 同昵称合并 |
| X3 | 同一联系人改昵称 | conversationKey 不变，仅显示名更新 | 新建第二份画像 |
| X4 | `msgId = "9007199254740993"`（>2^53） | 全链路（HTTP/存储/重读）逐字符串一致 | 转 Number 丢精度 |
| X5 | 先导入 t=10–20 消息并完成分析，再补 t=1–9 | `dataRevision+1`、earliestAffectedPosition为完整最早位置；保留既有local_seq，旧revision游标必须被拒；干净全量与增量最终态确定性一致 | 只记最新时间导致早期消息永不分析 |
| X6 | 撤回消息（recallTime 非空）与同 ID 原文先后到达 | 状态置 `recalled`，保留记录，分析失效；不当作两条新消息 | 静默覆盖或重复计数 |
| X7 | 群聊记录（3+ 参与者）误投单聊管线 | 结构冲突拒绝，不入库 | 筛成两人后当完整单聊 |
| X8 | 长消息 3 段跨 2 批（charOffset 续算 `cursor[2]-1`） | 尾段不重复累计、不漏 | 续算错位 |
| X9 | 同一上游响应重复页（游标不前进） | 任务 `partial(reason=duplicate)`，循环终止 | 死循环或谎报 completed |
| X10 | `build_input_record(..., source_kind="qq")`（枚举扩展后） | 通过并保留 inputMeta | 伪装 wechat 过校验 |

## 10. 方法覆盖表（消费者 ↔ 契约方法 ↔ 现有回归）

`scripts/test-migration-contracts.py` 从 `bridge/backend_service.py`、`bridge/real_http.py`、
`bridge/batch_engine.py` 的 AST 反查全部来源方法调用点，与本文件 §2.0 的 ```python 块逐一比对；
下表只是人读版本，**通过与否以测试为准**。

| 契约方法 | 直接消费者 | 现有回归锚 |
|---|---|---|
| verified_identity / identity | backend_service :280,323,347,284,330,354 | test_backend_layers.py |
| require_messages_ready | backend_service :327,351,1652 分支 | test_backend_layers.py |
| conversation_coverage（新） | QQ 同步 worker / 版本页（T07） | test_migration_contracts.py（契约级） |
| sessions / contact | backend_service :306,963 | test_real_backend.py |
| messages | backend_service :1643（insights 窗口兜底 :604） | test_api_insights*.py |
| message_windows | backend_service :1656, test_chat_classifier | test_chat_classifier.py |
| texts_for_refs | backend_service :2569,2600 | test_real_backend.py |
| media / media_reason | real_http :178 | test_real_backend.py |
| history_highwater / history_page | backend_service :958,846 | test_backend_layers.py |
| quoted_history_page | backend_service :2071 | test_real_backend.py |
| preceding_text_context | backend_service :2031 | test_real_backend.py |
| stats | backend_service :973,977,2591,2595 | test_real_backend.py |
| profile_metadata | backend_service :969,2587 | test_api_insights.py |
| profile_overview | backend_service :965 | test_api_insights.py |
| invalidate_profile_metadata（新） | backend_service :1168（**当前是私有属性 clear，需改**） | test_migration_contracts.py |
| browse/search/saved_results（微信实现） | backend_service :602,1669,1689 | test_backend_layers.py；QQ 版在 T07 建 test_qq_source.py |
| lock / request_scope | backend_service :802,926,1423,1478 | 同上 |
| forget_account / close | backend_service :154,210,213 | test_account_store.py |
| message_input（qq 扩展后） | backend_service :644,1865 | test_message_input.py（**禁止**先加"qq 必须被拒"断言） |

## 11. 遗留开放项

1. **[实机待证]** CP04 新消息可达与延迟、CP06 跨重启 msgId 稳定（P03 协议就绪，等用户配合）。
2. **[实机待证]** QCE 组件详细构建号/NapCat 实际版本；升级复跑范围。
3. QCE 好友表分页上限（好友 424 人，`/api/friends` 单页 50 的翻页语义）——T07 实现前补一次有界探查。
4. 内联脚本证据补交（07 RV06）：前轮"有界内联脚本"未入库，其 CP03/CP05/CP07/CP08 观察维持"前轮报告"定性。

## 2026-09-30 旧历史补读与近期核对实现补充

同一协调器先处理到期的前向窗口，空闲时再做近期核对与旧历史补读；两者共用分钟请求额度和账号/选择/取消边界。后台单次扫描限3秒，仍遵守总请求、分页与消息预算。当前临时参数为旧历史间隔30秒、近期核对间隔60秒、近期范围10分钟，均需CP04/CP06后的设备校准。

旧历史从首次前向窗口起点向时间0补读，消息与补读状态在同一事务提交；原前向checkpoint不被回退。没有完成的窗口从第一页重放，不持久化跨进程页码。预算不足在三次有界尝试后按秒缩小窗口，优先读较新的部分，保留旧区间栈。重复页、协议/身份异常等不作为成功覆盖；最小1秒仍读不完时明确保留缺口和重试入口。分钟额度耗尽只等待恢复，不消耗失败次数。

近期核对周期重扫最近10分钟，找回前向重叠范围之外的迟到消息；只声明本轮接口区间扫描完成，不保证更早迟到消息或QQ服务器全部历史可达。新消息/回补仍按native ID合并并触发既有分析代次失效。移除会话、切账号、重连和恢复后，任务从消息库重新加载，旧内存进度不可作为恢复依据。

证据见第十二轮回报T06-history-and-reconciliation.md。所有验证使用临时库和假QCE；生产真实开关继续关闭，G1接入/CP04/CP06/G2及真机验收仍待证。

## 2026-09-30 QQ媒体与引用展示补充（第十三轮）

普通文本仍按`message.row`输出；需要媒体/引用占位的QQ行可附加以下`qqDisplay`展示对象，旧的必需键和`_sort`不变。它不进入模型wire，不改作者`text`、消息类型映射或核心DDL。

```dto message.qqDisplay
parts,hasQuote,quoteText,quoteTruncated
```

`parts`只取`image/audio/file/video/face/unknown`，来自已保存的明确结构元素，按第一次出现去重。`hasQuote`说明存在引用，`quoteText`只取规范库独立`quote`列，缺失时为null，不补猜作者/内容；最多4000个Unicode字符，长引用令`quoteTruncated=true`。撤回/冲突行不返回原引用或媒体描述。界面用textContent展示，无媒体URL、文件路径、读取/下载/识别动作。源码依据为固定NapCat `0b4cfe65` 的MessageElement与固定QCE `7fcca888` 的MessageContent/解析器；新增数字msgType仍不擅自映射，未识别值保留unknown计数与占位。

本地人格显示遵守既有后端的100条目标文本、每轴30条支持证据和0.2份额差门槛，未达维度保留未知；API维度保持原提供商估计契约。原好感度/MBTI计算、词库和模型权重未改变。具体源码与反例见第十三轮回报。


## 2026-09-30 只读支持信息扩展（第十七轮）

`data_scope(account,user)`仅用于QQ当前本机单聊，内部持`read_library`的账号代次和库锁，并在同一个只读事务快照中汇总各项；不拉取QCE、不推理、不改变消息DDL/检查点/已有DTO。跨账号拒绝，未知单聊拒绝。返回本机消息/有效文本/对方有效文本数量、起止毫秒时间、dataRevision、规范化版本分组、已保存的前向窗口与history/reconcile工作区间/待扫描stack、最近完成扫描时间。其容器键为account/user/source/dataRevision/counts/range/normalizeVersions/moreNormalizeVersions/forward、可选history/reconcile，以及固定completeness='not-proven'/scanBoundary='interface-window-only'。正文、引用、raw、路径及原始错误串不返回；查询不会创建辅助表。逐条同步/导入来源尚未分别持久化，不反推来源或完整性。

`GET /api/qq/data-scope?account=...&user=...`仅装配QQAccountAPI时提供，继续受正式HTTP回环/Origin/请求代次约束。

`GET /api/qq/diagnostics`返回固定`qq-diagnostics-v1`：软件/运行时版本、已知状态枚举、数值计数及区段可用标记。只调用已有health/model_source/sync.public/import.status，不读取聊天日志/正文，不启动模型或发外部请求。用户名、账号/UIN/UID/会话键、昵称、Base URL、源ID、模型路径/自由名称、Key/token和原始错误一律不复制进导出；未知字符串映射unknown，非法数值置null，状态聚合最多500个会话并明确采样数量。该端点是匿名诊断摘要，不含数据库或日志包。


## 2026-09-30 F23 接入和补导记录（辅助契约v1）

`ingest_history(account,user,before,limit,message_cursor,kind)` 持read_library账号代次/库锁与只读一致快照。limit为1–50，before为上页末runId严格小于值，kind可选forward/history/reconcile/file-import。message_cursor必须为当前账号、会话和dataRevision编码的historyCursor，并能对应实际time_ms/local_seq行。读取不建表、不联网；无辅助记录的旧库返回空，不倒推旧消息来源。

响应含account、user、dataRevision、entries和nextBefore。每批返回runId、kind、format、sourceSnapshot、sourceVersion、state/reason、completedAtMs、interfaceWindow、recordRange、acceptedRows/rejectedRows、counts（inserted/unchanged/recalled/revised/conflicts）、dataRevision。message_cursor查询额外含observationCount与最多5项observations（fingerprint、normalizeVersion、disposition）。按批次分页，单批多内容版本不会跨页丢批次；原始竞争内容仍在原消息冲突表，不在此响应复制。

sourceSnapshot是文件解析快照的SHA256（按行解析值和拒绝码及头元数据/格式的规范JSON构造），不是原文件字节SHA256，不保存原文件路径。sourceVersion对接口保存实际返回且校验的版本，对文件保存语法合法的metadata.version并标为导出声明版本；缺失或不符合版本语法时null不猜。同步失败原因只存固定枚举，不存异常正文、token和地址。interfaceWindow只表示请求区间，complete也不证明远端全部历史。重复来源观察不增加消息或改变分析代次。

`data_scope`追加provenance：recordedMessages/unrecordedMessages、sources（kind/messages，重叠计数不是分区）、runCount、forwardSuccesses、lastForwardSuccessAtMs。只读同一快照。QQSync.public的lastSuccessAtMs来自当前账号已提交forward complete/complete-empty记录，重启保留；补导、history/reconcile、partial/error和切换账号不冒充前向成功。匿名diagnostics不追加这些身份/指纹明细。


## 2026-10-01 QQ R2 型别与成员扩展

原local-core-v1单聊 DTO 和游标保持；QQ群会话/消息额外提供 conversationKind=group，isGroup来自存储kind，不从用户名后缀推断。QQ新增以下只读来源方法：

```python
def conversation_kind(self, user: str) -> str
def members(self, user: str) -> list[dict]
```

members含稳定id/username、群内name、avatar、isSelf、active和已读范围count；uin/uid保留字符串用于身份投影。群profile必须指定当前会话内的member_id，缺目标不生成全群人格。详情见docs/release/R2-DATA-CONTRACT.md。

### R3 单聊本人目标扩展（2026-10-01）

QQ单聊的stats/profile_metadata/profile_overview/quoted_history_page允许member="self"，明确表示当前账号在这个单聊的表达；其他任意member仍拒绝。member=None保留原对方语义。本人计数只来自direction=self且可分析文本，画像进度和发布按目标独立；普通消息浏览仍保留原始方向。本人标签与画像引用不得借用对方参考。此扩展不改原账号/会话/消息键或游标，不应用于WeChat兼容来源。
