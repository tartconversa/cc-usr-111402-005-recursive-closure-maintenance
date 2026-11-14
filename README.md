# 碳清单引用图的递归可达视图与循环撤销

本项目提供碳预算资料的现有本地执行路径。当前业务行为见app/baseline.py，资料契约见[资料与接口](docs/data-contract.md)。
app/storage.py校验输入并产生确定内容摘要；app/semantics.py提供受限三值谓词；app/service.py绑定资料身份并运行现有代码。
fixtures/sample.json全部为合成资料，不对应真实机构的排放记录。Python 3.14，当前模块仅依赖标准库。

运行已有入口：

    python -m app.service fixtures/sample.json

执行现有行为检查：

    python -m unittest discover -s tests -v

编译检查：

    python -m compileall -q app tests

## 增量递归可达视图

`app/baseline.py` 保留全量 BFS 闭包，作为差分对照基准。增量能力在三个新模块：

- `app/graphview.py`：递归可达闭包的增量维护。删除采用标准 DRed（插入→过度删除→重导），
  环上旧推导不得互相作证，另有幸存路径的关系不被误删；每个闭包行携带一条真实证据见证路径。
  提供 `MemoryBackend`（小图驻留）与 `SqliteBackend`（大图溢写，闭包内存镜像 + 磁盘落盘）。
- `app/snapshot.py`：版本化快照目录。直接边与闭包在同一版本目录原子发布，`current` 指针
  单次切换；sqlite 后端复制父版本库做增量，未受影响部分物理复用，父版本只读不变。
- `app/queries.py`：依赖查询 `reaches`、来源路径 `source_path`、下游 `dependents`、
  影响范围 `impact`（支持 `app/semantics.py` 的三值谓词筛选）。

发布入口（变更计划格式见 [资料与接口](docs/data-contract.md)）：

    python -m app.publisher init   <store目录> <完整资料.json> <版本名>
    python -m app.publisher change <store目录> <资料.json> <版本名> <变更计划.json> [--force-rebuild]

每次发布返回 `stats`（处理的证据与逻辑边数、闭包删除/重导/新增/复用行数、级联步数、
`full_rebuild`）与 `impact`（删除后不再可达的行及其撤销依据 basis、受影响源）。
普通局部更正 `full_rebuild=false`，只有显式 `--force-rebuild` 或首次装载才全量重建。

`tests/test_incremental.py` 含随机图差分（每批变更后与全量 BFS 逐项对照、逐行见证校验）、
环与并行证据专项、内存/sqlite 后端对等、快照版本化与查询测试。
