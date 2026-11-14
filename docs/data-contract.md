# 资料与接口

## 内容身份

入口app.service.inspect(data)不修改调用方资料，返回input_digest及result。
input_digest是对完整输入JSON按键排序、UTF-8、无空白序列化的SHA-256。数组顺序保留；禁止NaN和无穷，磁盘JSON重复字段拒绝。
app.storage.freeze返回独立副本。输入快照和对象版本由业务字段明确声明，内容摘要不会替调用方创造版本。

## 现有业务约定

edges含evidence_id、from、to，evidence_id唯一，相同端点不同证据独立。nodes明确全集，未知端点拒绝。直接逻辑边在至少一条证据存在时成立。正长度可达不把每个节点自动设为自可达。baseline.execute以全图搜索返回[from,to]，以键序输出，仅作为已有全量查询。snapshot名称由输入版本声明，不由边数组位置生成。

## 受限谓词

app.semantics.evaluate(row, expression)返回True/False/None，None为unknown。
eq/lt/le/gt/ge含field和value，缺字段按null；比较双方必须同类型，null比较结果unknown。
is_null只有field；and/or含args数组，not含arg，按三值逻辑计算。空and为true，空or为false。
where只接受结果严格为true的行。expression为null表示不限制。

## 现有执行链

app.service.inspect → storage.freeze/digest → baseline.execute。sample.json描述的是现有可执行输入，服务输出附资料摘要。
新增能力可以增加入口和计划结构，保留现有调用及输出语义；扩展格式应单独声明版本。

## 增量递归视图（incremental-v1）

app.graphview在不重算整图的前提下维护正长度可达闭包，app.snapshot把直接边与闭包作为同一版本原子发布。

### 变更计划

一批变更为操作数组，按净变化处理，操作间不允许同一证据在同批既新增又撤销：

- `{"op":"add_node","node":...}` / `{"op":"remove_node","node":...}`，有incident证据的节点拒绝移除。
- `{"op":"add_evidence","evidence_id","from","to"}`：未知端点、证据id重复拒绝。
- `{"op":"remove_evidence","evidence_id"}`：证据不存在拒绝。
逻辑边在至少一条证据存在时成立；撤销一条证据若仍有其他证据支撑，逻辑边不消失，仅修复引用该证据的见证。

### 维护语义

删除走标准DRed三阶段：先插入本批新边，再过度删除净消失边可能破坏的闭包行，最后只凭批后幸存边（含新边）重导。
删除集内的闭包行不得充当推导中间行，因此环上两条旧推导不能互相作证；重导幸存判据保证另一条路径仍支持的关系不被抹掉。
只有真正处于环上的节点会出现自可达行，环被打破后对应自可达行进入影响范围。
每个闭包行携带一条由现存evidence_id组成的见证路径，`ReachabilityView.explain`逐边返回；发布前强制校验全部见证真实可达。

### 快照目录与版本

SnapshotStore按版本写独立目录（edges.json + closure.json，或溢写的closure.sqlite3 + publish.json），
新版本先写工作目录再单次原子切换`current`与manifest；直接边和闭包同目录即同一版本。
sqlite后端从父版本复制库文件后做增量维护，未受影响物理页天然复用，父版本目录在发布期间保持只读不变。
publish.json记录parent、edges_digest、change_plan、stats与impact；stats含处理的证据/逻辑边数、
闭包删除/重导/新增/复用行数、级联步数与full_rebuild标记。局部更正full_rebuild为false，仅显式重建为true。

### 后端与溢写

MemoryBackend关系全驻留，适合小图；边或节点超过graphview.choose_backend阈值时用SqliteBackend溢写。
两后端对同一变更序列逐行等价（tests/test_incremental.py含随机差分与后端对等检查）。
app.queries提供reaches、source_path、dependents与impact（impact支持本文件“受限谓词”的三值筛选）。
