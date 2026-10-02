# R2 会话与成员契约、schema 3→4 迁移


## 保持不变

账号仍由 canonical QQ UIN 生成 a: 键；好友会话仍为 u:peerUid；message_key 的输入及哈希不变；native_id/msgSeq仍是字符串。local_seq不重排，时间＋local_seq游标、revision与失效边界不变。迁移不改任何原消息、观察记录、收据、同步工作或已成功分析结果。

## 增加的实体

群会话使用 g:canonicalGroupCode，kind显式为group，group_code单独保存；不伪装微信@chatroom，也不把群号放入单聊peer_uid。好友与群的相同数字处于不同命名空间。

账号内人物以独立member_id识别；已知UIN用uin:UIN，只有稳定UID时用uid:UID。后补UIN/UID只补身份字段，不改已有member_id；两条已存在身份发生冲突则拒绝合并，不按昵称猜。群/好友成员关系按账号＋会话＋member_id存储，群名片与人物昵称分开；退群成员留存，并显式记录当前关系状态。

会话资料存群名/好友来源名、受限QQ头像地址与来源版本；人物资料存UIN/UID、昵称及头像。未知发言者保留原消息和未知状态，不纳入人物表达统计。引用、@、撤回、系统信息保留原始raw，显示与分析另有明确投影。

规范化默认仍是friend作用域，group必须由接入器/导入器显式指定。错误类型不得混入另一个作用域。群消息保持具体作者，不以名字或统一other身份合并。

## 升级与回退

消息库当前实际schema是3。schema4扩展conversations的kind及group_code，并增人物、成员关系和会话资料表；messages及其键、顺序、所有旧列不变。旧3库先经完整布局/外键/完整性检查，再用SQLite Backup API保存唯一v3备份。关闭外键约束期间仅在一个事务中重建父会话表，逐列复制所有旧值；其他表保持原表。新结构外键检查通过后提交并重新启用外键。异常回滚，旧备份保留。原2库先完成已存在的2→3观察记录迁移，再升级4；两个恢复点都留存。

先升级私有旧库副本并比较每个旧表的原列、消息键、local_seq、revision和同步状态；旧成功结果数据库仅复制核对，不由此迁移改写。验证前不升级原真实库。R1可用包和数据目录保留。需要回到R1时使用v3备份与原R1程序的独立目录；不把含新群数据的v4库直接交给旧程序，也不覆盖新群库。

下面DDL由当前实现导出，测试同时核对旧冻结DDL与新DDL。没有新依赖或新的QQ接入架构。

```sql
CREATE TABLE conversations (
  conversation_key TEXT PRIMARY KEY,
  account_key      TEXT NOT NULL,
  peer_uin         TEXT,
  peer_uid         TEXT,
  display_name     TEXT,
  kind             TEXT NOT NULL CHECK (kind IN ('friend','group')),
  group_code       TEXT CHECK ((kind='friend' AND group_code IS NULL) OR (kind='group' AND group_code IS NOT NULL)),

  selected         INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0,1)),
  first_time_ms    INTEGER,
  last_time_ms     INTEGER,
  data_revision    INTEGER NOT NULL DEFAULT 1,
  earliest_affected_time_ms INTEGER,
  earliest_affected_local_seq INTEGER,
  UNIQUE (account_key, conversation_key)
);
CREATE TABLE messages (
  message_key      TEXT PRIMARY KEY,
  account_key      TEXT NOT NULL,
  conversation_key TEXT NOT NULL,
  native_id_kind   TEXT NOT NULL,
  native_id        TEXT NOT NULL,
  native_seq       TEXT,
  local_seq        INTEGER NOT NULL CHECK (typeof(local_seq) = 'integer' AND local_seq >= 1),
  sender_uin       TEXT,
  sender_uid       TEXT,
  send_type        TEXT,
  direction        TEXT NOT NULL CHECK (direction IN ('self','peer','system','conflict')),
  time_ms          INTEGER NOT NULL,
  msg_type         INTEGER,
  kind             TEXT NOT NULL,
  text             TEXT,
  quote            TEXT,
  status           TEXT NOT NULL DEFAULT 'normal'
                     CHECK (status IN ('normal','recalled','revised','conflict')),
  recall_time      TEXT,
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
CREATE TABLE message_conflicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_key TEXT NOT NULL, conversation_key TEXT NOT NULL,
  native_id_kind TEXT NOT NULL, native_id TEXT NOT NULL,
  existing_revision INTEGER NOT NULL, incoming_raw TEXT NOT NULL,
  detected_at INTEGER NOT NULL,
  resolved INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0,1))
);
CREATE TABLE message_observations (
  message_key TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  PRIMARY KEY (message_key, fingerprint),
  FOREIGN KEY (message_key) REFERENCES messages (message_key)
);
CREATE TABLE sync_checkpoints (
  account_key TEXT NOT NULL,
  conversation_key TEXT NOT NULL,
  cursor_version INTEGER NOT NULL,
  platform TEXT NOT NULL CHECK (platform = 'qq'),
  window_start_ms INTEGER NOT NULL,
  window_end_ms INTEGER NOT NULL,
  scanned_through_ms INTEGER NOT NULL DEFAULT 0,
  last_commit_time_ms INTEGER NOT NULL DEFAULT 0,
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
CREATE TABLE identity_aliases (
  account_key TEXT NOT NULL,
  uin TEXT NOT NULL, uid TEXT NOT NULL DEFAULT '', peer_uid TEXT NOT NULL DEFAULT '',
  alias_kind TEXT NOT NULL,
  evidence TEXT NOT NULL,
  confidence TEXT NOT NULL CHECK (confidence IN ('confirmed','probable','pending')),
  created_at INTEGER NOT NULL,
  UNIQUE (account_key, uin, uid, peer_uid, alias_kind)
);
CREATE TABLE import_manifest (
  import_id TEXT PRIMARY KEY,
  source_kind TEXT NOT NULL,
  account_key TEXT NOT NULL,
  rows_total INTEGER, rows_ok INTEGER, rows_rejected INTEGER, rows_conflict INTEGER,
  earliest_native_time_ms INTEGER, latest_native_time_ms INTEGER,
  imported_at INTEGER NOT NULL,
  schema_version TEXT NOT NULL
);

CREATE TABLE qq_people_v1 (
 account_key TEXT NOT NULL, member_id TEXT NOT NULL,
 uin TEXT, uid TEXT, nickname TEXT NOT NULL DEFAULT '', avatar_url TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(account_key,member_id), UNIQUE(account_key,uin), UNIQUE(account_key,uid)
);
CREATE TABLE qq_memberships_v1 (
 account_key TEXT NOT NULL, conversation_key TEXT NOT NULL, member_id TEXT NOT NULL,
 card_name TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 source TEXT NOT NULL DEFAULT 'observed',
 PRIMARY KEY(account_key,conversation_key,member_id),
 FOREIGN KEY(account_key,conversation_key) REFERENCES conversations(account_key,conversation_key),
 FOREIGN KEY(account_key,member_id) REFERENCES qq_people_v1(account_key,member_id)
);
CREATE TABLE qq_conversation_profiles_v1 (
 account_key TEXT NOT NULL, conversation_key TEXT NOT NULL,
 name TEXT NOT NULL DEFAULT '', avatar_url TEXT NOT NULL DEFAULT '', source_version TEXT,
 PRIMARY KEY(account_key,conversation_key),
 FOREIGN KEY(account_key,conversation_key) REFERENCES conversations(account_key,conversation_key)
);
```
