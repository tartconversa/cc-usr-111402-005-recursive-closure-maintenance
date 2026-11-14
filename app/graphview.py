"""递归可达闭包的增量维护。

边表示“包依赖来源包”(from -> to)，闭包只保存正长度路径是否存在。
直接逻辑边可由多条独立证据支撑，删除一条证据不等于删除边。

删除采用 DRed（Delete-Rederive）：
1. 删除阶段把失去全部幸存推导的闭包行级联下入删除集，环上互相作证的行会被过度删除；
2. 重导阶段只依据当前幸存的边重新推导删除集中的行，环中旧推导无法互相作证；
3. 未被重导的行才是真正不再可达的行，连同受影响依据(basis)进入影响范围。
插入采用半朴素前向/反向传播。每个闭包行携带一条由证据id组成的真实见证路径。
"""
import json
import sqlite3
from collections import deque


class IntegrityError(RuntimeError):
    """闭包与当前边集不一致，且增量维护无法自行修复。"""


def _encode_witness(witness):
    return json.dumps(list(witness), ensure_ascii=False, separators=(',', ':'))


class MemoryBackend:
    """小图后端：全部关系驻留内存，按源维护闭包索引。"""

    def __init__(self):
        self.nodes = set()
        self.evidence = {}                 # eid -> (u, v)
        self.pair_count = {}               # (u, v) -> 证据数
        self.pair_eid = {}                 # (u, v) -> 任一证据id
        self.adj = {}                      # u -> {v} 逻辑边
        self.radj = {}                     # v -> {u}
        self.closure = {}                  # (u, v) -> 见证元组
        self.src_index = {}                # u -> {v: 见证}
        self.tgt_index = {}                # v -> {u}

    # 节点 ------------------------------------------------------------------
    def add_node(self, node):
        if node not in self.nodes:
            self.nodes.add(node)
            self.adj.setdefault(node, set())
            self.radj.setdefault(node, set())
            self.src_index.setdefault(node, {})

    def has_node(self, node):
        return node in self.nodes

    def remove_node(self, node):
        self.nodes.remove(node)
        del self.adj[node]
        del self.radj[node]
        del self.src_index[node]

    def iter_nodes(self):
        return iter(self.nodes)

    # 证据与逻辑边 -----------------------------------------------------------
    def add_evidence(self, eid, u, v):
        """返回该端点对是否为新建逻辑边。"""
        if eid in self.evidence:
            raise ValueError('证据键重复')
        self.evidence[eid] = (u, v)
        pair = (u, v)
        new_pair = pair not in self.pair_count
        self.pair_count[pair] = self.pair_count.get(pair, 0) + 1
        self.pair_eid.setdefault(pair, eid)
        if new_pair:
            self.adj[u].add(v)
            self.radj[v].add(u)
        return new_pair

    def remove_evidence(self, eid):
        """返回 (u, v, 逻辑边是否随之消失)。"""
        if eid not in self.evidence:
            raise ValueError('证据不存在')
        u, v = self.evidence.pop(eid)
        pair = (u, v)
        count = self.pair_count[pair] - 1
        gone = count == 0
        if gone:
            del self.pair_count[pair]
            self.pair_eid.pop(pair, None)
            self.adj[u].discard(v)
            self.radj[v].discard(u)
            return u, v, True
        self.pair_count[pair] = count
        if self.pair_eid.get(pair) == eid:
            # 当前索引指向被撤证据时，换一条幸存证据
            self.pair_eid[pair] = next(
                e for e, ep in self.evidence.items() if ep == pair)
        return u, v, False

    def has_evidence(self, eid):
        return eid in self.evidence

    def get_evidence(self, eid):
        return self.evidence[eid]

    def evidence_id_for(self, u, v):
        try:
            return self.pair_eid[(u, v)]
        except KeyError:
            raise KeyError((u, v))

    def iter_evidence(self):
        return iter(self.evidence.items())

    def edge_exists(self, u, v):
        return (u, v) in self.pair_count

    def edge_count(self):
        return len(self.pair_count)

    def successors(self, u):
        return list(self.adj[u])

    def predecessors(self, v):
        return list(self.radj[v])

    def incident(self, node):
        return any(u == node or v == node for u, v in self.pair_count)

    def bulk_load_graph(self, nodes, edges):
        """一次性装载节点与证据，直接构建索引。"""
        for node in nodes:
            self.add_node(node)
        for edge in edges:
            self.add_evidence(
                edge['evidence_id'], edge['from'], edge['to'])

    def bulk_put_rows(self, rows):
        for u, v, witness in rows:
            self.put_row(u, v, witness)

    # 闭包 -------------------------------------------------------------------
    def has_row(self, u, v):
        return (u, v) in self.closure

    def get_witness(self, u, v):
        return self.closure[(u, v)]

    def put_row(self, u, v, witness):
        witness = tuple(witness)
        self.closure[(u, v)] = witness
        self.src_index.setdefault(u, {})[v] = witness
        self.tgt_index.setdefault(v, set()).add(u)

    def delete_row(self, u, v):
        del self.closure[(u, v)]
        del self.src_index[u][v]
        self.tgt_index.get(v, set()).discard(u)

    def sources_reaching(self, v):
        return list(self.tgt_index.get(v, ()))

    def iter_source_rows(self, u):
        return list(self.src_index.get(u, {}).items())

    def iter_all_rows(self):
        return [(u, v, w) for (u, v), w in self.closure.items()]

    def closure_count(self):
        return len(self.closure)

    def clear_closure(self):
        self.closure.clear()
        self.src_index = {n: {} for n in self.nodes}
        self.tgt_index = {}

    # 事务（内存版无需持久化回滚） --------------------------------------------
    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class SqliteBackend:
    """大图溢写后端：证据、逻辑边计数与闭包均落在 sqlite 单文件库。"""

    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.execute('PRAGMA synchronous=NORMAL')
        # 邻接与证据的进程内缓存：大图 BFS 的热点读取不再逐次打 sqlite
        self._adj_cache = None
        self._radj_cache = None
        self._eid_cache = None
        self._pair_eid_cache = None
        # 闭包内存镜像：增量维护的热点判定走内存，落盘走 sqlite
        self._closure = None
        self._src_index = None
        self._tgt_index = None
        self.conn.execute(
            'CREATE TABLE IF NOT EXISTS nodes(n TEXT PRIMARY KEY)')
        self.conn.execute(
            'CREATE TABLE IF NOT EXISTS evidence(eid TEXT PRIMARY KEY,u TEXT NOT NULL,v TEXT NOT NULL)')
        self.conn.execute(
            'CREATE TABLE IF NOT EXISTS epair(u TEXT NOT NULL,v TEXT NOT NULL,cnt INTEGER NOT NULL,PRIMARY KEY(u,v))')
        self.conn.execute('CREATE INDEX IF NOT EXISTS ix_epair_v ON epair(v)')
        self.conn.execute(
            'CREATE TABLE IF NOT EXISTS closure(u TEXT NOT NULL,v TEXT NOT NULL,witness TEXT NOT NULL,PRIMARY KEY(u,v))')
        self.conn.execute('CREATE INDEX IF NOT EXISTS ix_closure_u ON closure(u)')
        self.conn.execute('CREATE INDEX IF NOT EXISTS ix_closure_v ON closure(v)')
        self.conn.commit()

    def invalidate_caches(self):
        # 仅失效图结构缓存；闭包镜像由 put/delete/clear 自行维护，
        # 不能在证据增删时被清空，否则 DRed 会读到空闭包而欠删除
        self._adj_cache = self._radj_cache = None
        self._eid_cache = self._pair_eid_cache = None

    def _ensure_graph_caches(self):
        if self._adj_cache is not None:
            return
        adj, radj, eids, pair_eid = {}, {}, {}, {}
        for n, in self.conn.execute('SELECT n FROM nodes'):
            adj.setdefault(n, set())
            radj.setdefault(n, set())
        for eid, u, v in self.conn.execute('SELECT eid,u,v FROM evidence'):
            adj.setdefault(u, set()).add(v)
            radj.setdefault(v, set()).add(u)
            eids[eid] = (u, v)
            pair_eid.setdefault((u, v), eid)
        self._adj_cache, self._radj_cache = adj, radj
        self._eid_cache, self._pair_eid_cache = eids, pair_eid

    def add_node(self, node):
        self.conn.execute('INSERT OR IGNORE INTO nodes(n) VALUES(?)', (node,))
        self.invalidate_caches()

    def has_node(self, node):
        return self.conn.execute('SELECT 1 FROM nodes WHERE n=?', (node,)).fetchone() is not None

    def remove_node(self, node):
        self.conn.execute('DELETE FROM nodes WHERE n=?', (node,))
        self.invalidate_caches()

    def iter_nodes(self):
        return (row[0] for row in self.conn.execute('SELECT n FROM nodes'))

    def add_evidence(self, eid, u, v):
        if self.conn.execute('SELECT 1 FROM evidence WHERE eid=?', (eid,)).fetchone():
            raise ValueError('证据键重复')
        self.conn.execute('INSERT INTO evidence(eid,u,v) VALUES(?,?,?)', (eid, u, v))
        row = self.conn.execute('SELECT cnt FROM epair WHERE u=? AND v=?', (u, v)).fetchone()
        new_pair = row is None
        if new_pair:
            self.conn.execute('INSERT INTO epair(u,v,cnt) VALUES(?,?,1)', (u, v))
        else:
            self.conn.execute('UPDATE epair SET cnt=cnt+1 WHERE u=? AND v=?', (u, v))
        self.invalidate_caches()
        return new_pair

    def remove_evidence(self, eid):
        row = self.conn.execute('SELECT u,v FROM evidence WHERE eid=?', (eid,)).fetchone()
        if row is None:
            raise ValueError('证据不存在')
        u, v = row
        self.conn.execute('DELETE FROM evidence WHERE eid=?', (eid,))
        count = self.conn.execute(
            'SELECT cnt FROM epair WHERE u=? AND v=?', (u, v)).fetchone()[0] - 1
        gone = count == 0
        if gone:
            self.conn.execute('DELETE FROM epair WHERE u=? AND v=?', (u, v))
        else:
            self.conn.execute('UPDATE epair SET cnt=? WHERE u=? AND v=?', (count, u, v))
        self.invalidate_caches()
        return u, v, gone

    def has_evidence(self, eid):
        self._ensure_graph_caches()
        return eid in self._eid_cache

    def get_evidence(self, eid):
        self._ensure_graph_caches()
        return self._eid_cache[eid]

    def evidence_id_for(self, u, v):
        self._ensure_graph_caches()
        try:
            return self._pair_eid_cache[(u, v)]
        except KeyError:
            raise KeyError((u, v))

    def iter_evidence(self):
        self._ensure_graph_caches()
        return iter(self._eid_cache.items())

    def edge_exists(self, u, v):
        self._ensure_graph_caches()
        return (u, v) in self._pair_eid_cache

    def edge_count(self):
        return self.conn.execute('SELECT count(*) FROM epair').fetchone()[0]

    def successors(self, u):
        self._ensure_graph_caches()
        return list(self._adj_cache.get(u, ()))

    def predecessors(self, v):
        self._ensure_graph_caches()
        return list(self._radj_cache.get(v, ()))

    def incident(self, node):
        self._ensure_graph_caches()
        return bool(self._adj_cache.get(node) or self._radj_cache.get(node))

    def bulk_load_graph(self, nodes, edges):
        """一次性装载节点与证据，单事务批量写入，供初始全量装载使用。"""
        self.conn.execute('BEGIN')
        self.conn.executemany(
            'INSERT OR IGNORE INTO nodes(n) VALUES(?)',
            ((n,) for n in nodes))
        self.conn.executemany(
            'INSERT INTO evidence(eid,u,v) VALUES(?,?,?)',
            ((e['evidence_id'], e['from'], e['to']) for e in edges))
        pair_count = {}
        for e in edges:
            pair = (e['from'], e['to'])
            pair_count[pair] = pair_count.get(pair, 0) + 1
        self.conn.executemany(
            'INSERT INTO epair(u,v,cnt) VALUES(?,?,?)',
            ((u, v, c) for (u, v), c in pair_count.items()))
        self.conn.commit()
        self.invalidate_caches()

    def bulk_put_rows(self, rows):
        """rows 为 (u, v, witness元组) 可迭代对象，批量写闭包并同步内存镜像。"""
        rows = list(rows)
        self.conn.executemany(
            'INSERT INTO closure(u,v,witness) VALUES(?,?,?) '
            'ON CONFLICT(u,v) DO UPDATE SET witness=excluded.witness',
            ((u, v, _encode_witness(w)) for u, v, w in rows))
        self._ensure_closure_cache()
        for u, v, w in rows:
            w = tuple(w)
            self._closure[(u, v)] = w
            self._src_index.setdefault(u, set()).add(v)
            self._tgt_index.setdefault(v, set()).add(u)

    def _ensure_closure_cache(self):
        if self._closure is None:
            self._closure = {
                (u, v): tuple(json.loads(w))
                for u, v, w in self.conn.execute(
                    'SELECT u,v,witness FROM closure')}
            self._src_index = {}
            self._tgt_index = {}
            for (u, v) in self._closure:
                self._src_index.setdefault(u, set()).add(v)
                self._tgt_index.setdefault(v, set()).add(u)

    def has_row(self, u, v):
        self._ensure_closure_cache()
        return (u, v) in self._closure

    def get_witness(self, u, v):
        self._ensure_closure_cache()
        return self._closure[(u, v)]

    def put_row(self, u, v, witness):
        self._ensure_closure_cache()
        witness = tuple(witness)
        existed = (u, v) in self._closure
        self._closure[(u, v)] = witness
        self._src_index.setdefault(u, set()).add(v)
        self._tgt_index.setdefault(v, set()).add(u)
        if existed:
            self.conn.execute(
                'UPDATE closure SET witness=? WHERE u=? AND v=?',
                (_encode_witness(witness), u, v))
        else:
            self.conn.execute(
                'INSERT INTO closure(u,v,witness) VALUES(?,?,?)',
                (u, v, _encode_witness(witness)))

    def delete_row(self, u, v):
        self._ensure_closure_cache()
        del self._closure[(u, v)]
        self._src_index.get(u, set()).discard(v)
        self._tgt_index.get(v, set()).discard(u)
        self.conn.execute('DELETE FROM closure WHERE u=? AND v=?', (u, v))

    def sources_reaching(self, v):
        self._ensure_closure_cache()
        return list(self._tgt_index.get(v, ()))

    def iter_source_rows(self, u):
        self._ensure_closure_cache()
        # 返回快照列表：调用方可能在遍历时增删闭包行（DRed 种子阶段），
        # 直接返回活动集合视图会因迭代中改集合而漏项
        return [(v, self._closure[(u, v)])
                for v in list(self._src_index.get(u, ()))]

    def iter_all_rows(self):
        self._ensure_closure_cache()
        return ((u, v, w) for (u, v), w in self._closure.items())

    def closure_count(self):
        self._ensure_closure_cache()
        return len(self._closure)

    def clear_closure(self):
        self.conn.execute('DELETE FROM closure')
        self._closure = {}
        self._src_index = {}
        self._tgt_index = {}

    def commit(self):
        self.conn.commit()

    def rollback(self):
        # 回滚磁盘事务后，进程内图缓存与闭包镜像都已脏，强制下次从磁盘重建
        self.conn.rollback()
        self.invalidate_caches()
        self._closure = None
        self._src_index = None
        self._tgt_index = None

    def close(self):
        self.conn.commit()
        self.conn.close()


def choose_backend(node_count, edge_count):
    """超过阈值即溢写 sqlite，阈值内走内存。"""
    if edge_count > 3000 or node_count > 1500:
        return 'sqlite'
    return 'memory'


class ReachabilityView:
    """对具体后端执行增量闭包维护，并产出统计与影响范围。"""

    def __init__(self, backend):
        self.b = backend
        self.last_stats = None
        self.last_impact = None

    # 基础装载 ---------------------------------------------------------------
    def load_dataset(self, data):
        """全量装载 nodes/edges 并做一次显式全量重建（仅用于初始化）。"""
        nodes = set(data['nodes'])
        seen = set()
        for edge in data['edges']:
            eid = edge['evidence_id']
            if eid in seen:
                raise ValueError('证据键重复')
            seen.add(eid)
            if edge['from'] not in nodes or edge['to'] not in nodes:
                raise ValueError('未知图端点')
        if hasattr(self.b, 'bulk_load_graph'):
            self.b.bulk_load_graph(data['nodes'], data['edges'])
        else:
            for node in data['nodes']:
                self.b.add_node(node)
            for edge in data['edges']:
                self.b.add_evidence(
                    edge['evidence_id'], edge['from'], edge['to'])
        self.rebuild(reason='initial')
        return self.last_stats

    def load_frozen(self, nodes, edges, closure_rows):
        """直接装载已发布的冻结状态（图 + 闭包行），不触发全量重建。

        用于从快照目录恢复只读分析视图；closure_rows 为 (from, to, witness)。
        """
        b = self.b
        seen = set()
        node_set = set(nodes)
        for edge in edges:
            eid = edge['evidence_id']
            if eid in seen:
                raise ValueError('证据键重复')
            seen.add(eid)
            if edge['from'] not in node_set or edge['to'] not in node_set:
                raise ValueError('未知图端点')
        if hasattr(b, 'bulk_load_graph'):
            b.bulk_load_graph(nodes, edges)
        else:
            for node in nodes:
                b.add_node(node)
            for edge in edges:
                b.add_evidence(
                    edge['evidence_id'], edge['from'], edge['to'])
        b.clear_closure()
        if hasattr(b, 'bulk_put_rows'):
            b.bulk_put_rows(
                (u, v, tuple(w)) for u, v, w in closure_rows)
        else:
            for u, v, w in closure_rows:
                b.put_row(u, v, tuple(w))
        self.last_stats = None
        self.last_impact = None
        b.commit()
        return self

    def rebuild(self, reason='requested'):
        """显式全量重建：按源 BFS，闭包行见证取 BFS 首达路径。"""
        b = self.b
        b.clear_closure()
        bulk = hasattr(b, 'bulk_put_rows')
        pending = []
        for source in sorted(b.iter_nodes()):
            enqueued = set()
            queue = deque([(source, ())])
            while queue:
                node, witness = queue.popleft()
                for nxt in sorted(b.successors(node)):
                    if nxt in enqueued:
                        continue
                    new_witness = witness + (b.evidence_id_for(node, nxt),)
                    enqueued.add(nxt)
                    if bulk:
                        pending.append((source, nxt, new_witness))
                    else:
                        b.put_row(source, nxt, new_witness)
                    queue.append((nxt, new_witness))
        if bulk:
            b.bulk_put_rows(pending)
        self.last_stats = {
            'full_rebuild': True,
            'rebuild_reason': reason,
            'closure_rows': b.closure_count(),
        }
        self.last_impact = {'deleted_rows': [], 'affected_sources': []}
        b.commit()
        return self.last_stats

    # 变更预检与执行 ----------------------------------------------------------
    def _preflight(self, changes):
        """批次语义：一个证据不能在同一批变更里既新增又撤销。"""
        planned_nodes = set(self.b.iter_nodes())
        planned_eids = set(eid for eid, _ in self.b.iter_evidence())
        staged_adds = set()
        staged_removes = set()
        for op in changes:
            kind = op.get('op')
            if kind == 'add_node':
                node = op['node']
                if node in planned_nodes:
                    raise ValueError('节点已存在')
                planned_nodes.add(node)
            elif kind == 'remove_node':
                if op['node'] not in planned_nodes:
                    raise ValueError('未知图端点')
            elif kind == 'add_evidence':
                eid = op['evidence_id']
                if eid in planned_eids or eid in staged_adds:
                    raise ValueError('证据键重复')
                if op['from'] not in planned_nodes or op['to'] not in planned_nodes:
                    raise ValueError('未知图端点')
                planned_eids.add(eid)
                staged_adds.add(eid)
            elif kind == 'remove_evidence':
                eid = op['evidence_id']
                if eid in staged_adds:
                    raise ValueError('同一证据不能在同一批次中既新增又撤销')
                if eid in staged_removes:
                    raise ValueError('同一证据重复撤销')
                if eid not in planned_eids:
                    raise ValueError('证据不存在')
                staged_removes.add(eid)
            else:
                raise ValueError('未知变更操作')

    def apply(self, changes, force_rebuild=False):
        """对一批变更做事务式增量维护，返回统计与影响范围。

        先完成全部证据增删得到批后边集（同批不允许对同一证据既增又撤）。
        随后按标准 DRed 顺序执行：半朴素插入新边 → 过度删除净消失边影响的行
        → 只凭批后幸存边（含新边）重导 → 修复引用被撤证据的见证。
        被撤证据与新证据落在同一端点对时，逻辑边净存在，只触发见证修复而不产生闭包行删除。
        """
        self._preflight(changes)
        b = self.b
        stats = {
            'full_rebuild': False, 'rebuild_reason': None,
            'evidences_added': 0, 'evidences_removed': 0,
            'edges_added': 0, 'edges_removed': 0,
            'closure_rows_before': b.closure_count(),
            'rows_deleted': 0, 'rows_rederived': 0,
            'rows_inserted': 0, 'witness_repairs': 0,
            'delete_cascade_steps': 0, 'insert_cascade_steps': 0,
        }
        added_edges = []                       # (eid, u, v) 批后新存在的逻辑边
        added_pairs = set()
        removed_pairs = {}                    # 净消失边 pair -> [被撤eid]
        removed_evidence_pairs = {}           # 全部撤证 pair -> {eid}
        removed_eids = set()
        try:
            for op in changes:
                kind = op['op']
                if kind == 'add_node':
                    b.add_node(op['node'])
                elif kind == 'remove_node':
                    if b.incident(op['node']):
                        raise ValueError('节点仍有 incident 证据，不能移除')
                    b.remove_node(op['node'])
                elif kind == 'add_evidence':
                    eid, u, v = op['evidence_id'], op['from'], op['to']
                    if b.add_evidence(eid, u, v):
                        added_edges.append((eid, u, v))
                        added_pairs.add((u, v))
                    stats['evidences_added'] += 1
                else:
                    eid = op['evidence_id']
                    u, v, gone = b.remove_evidence(eid)
                    removed_eids.add(eid)
                    removed_evidence_pairs.setdefault((u, v), set()).add(eid)
                    stats['evidences_removed'] += 1
                    # 批内同对端点有新增则逻辑边净存在；只有净消失才进 DRed
                    if gone and (u, v) not in added_pairs:
                        removed_pairs.setdefault((u, v), []).append(eid)
            # 同对端点“撤旧补新”：净消失集合里剔除批后仍有证据的边
            for pair in list(removed_pairs):
                if b.edge_exists(*pair):
                    del removed_pairs[pair]

            if force_rebuild:
                self.rebuild(reason='requested')
                stats.update(self.last_stats)
                stats['evidences_added'] = sum(
                    1 for op in changes if op['op'] == 'add_evidence')
                stats['evidences_removed'] = len(removed_eids)
                stats['closure_rows_before'] = stats.get(
                    'closure_rows_before', stats['closure_rows'])
                b.commit()
                return self._result(stats)

            impact_rows = []
            affected_sources = set()
            rederived = {}
            # 标准 DRed 三阶段顺序：先插入新边，再过度删除，最后重导。
            # 这样重导的幸存判据能识别新边建立的替代路径，不会误删仍可达的行。

            # 阶段一：半朴素插入净新增逻辑边
            if added_edges:
                stats['edges_added'] = len(added_pairs)
                stats['rows_inserted'] = self._insert_edges(added_edges, stats)

            # 阶段二：过度删除净消失边可能破坏的行
            deleted = set()
            basis = {}
            if removed_pairs:
                stats['edges_removed'] = len(removed_pairs)
                deleted, basis = self._delete_edges(removed_pairs, stats)
                for pair in deleted:
                    if b.has_row(*pair):
                        b.delete_row(*pair)

            # 阶段三：只凭批后幸存边（含本批新边）重导删除集中的行
            if deleted:
                rederived = self._rederive(deleted, stats)
                for pair in deleted:
                    if pair in rederived:
                        b.put_row(pair[0], pair[1], rederived[pair])
                    else:
                        u, v = pair
                        impact_rows.append({
                            'from': u, 'to': v,
                            'basis': sorted(basis[pair]),
                        })
                        affected_sources.add(u)
                stats['rows_rederived'] = len(rederived)
                stats['rows_deleted'] = len(deleted) - len(rederived)

            # 阶段四：修复引用了被撤证据的幸存见证（含撤旧补新情形）
            if removed_eids:
                stats['witness_repairs'] = self._repair_witnesses(
                    removed_evidence_pairs, removed_eids)

            problems = self.check_witnesses()
            if problems:
                raise IntegrityError('闭包见证校验失败: %s' % problems[:3])
        except BaseException:
            # 维护中途失败：磁盘后端回滚未提交事务，避免半成品版本落盘
            b.rollback()
            raise

        self.last_impact = {
            'deleted_rows': sorted(impact_rows, key=lambda r: (r['from'], r['to'])),
            'affected_sources': sorted(affected_sources),
        }
        self.last_stats = stats
        b.commit()
        return self._result(stats)

    def _result(self, stats):
        b = self.b
        stats['closure_rows_after'] = b.closure_count()
        after = stats['closure_rows_after']
        if stats['full_rebuild']:
            stats['reused_rows'] = 0
        else:
            before = stats['closure_rows_before']
            # 未受影响复用：发布前后都存在且见证未变的行
            stats['reused_rows'] = max(
                0, before - stats['rows_deleted'] - stats['witness_repairs']
                - stats['rows_rederived'])
        return {
            'stats': dict(stats),
            'impact': self.last_impact,
            'counts': {
                'nodes': len(list(b.iter_nodes())),
                'evidences': len(list(b.iter_evidence())),
                'edges': b.edge_count(),
                'closure_rows': after,
            },
        }

    # DRed 删除阶段 -----------------------------------------------------------
    def _has_surviving_derivation(self, u, v, deleted):
        """删除集内的闭包行不再能充当推导中间行——环上旧推导不得互相作证。

        此阶段在插入之后运行，批后图已含新边；任何现存边或删除集外的
        中间行都构成幸存推导。
        """
        b = self.b
        if b.edge_exists(u, v):
            return True
        for z in b.predecessors(v):
            if (u, z) not in deleted and b.has_row(u, z):
                return True
        return False

    def _delete_edges(self, removed_pairs, stats):
        b = self.b
        deleted = set()
        basis = {}
        work = deque()

        def seed(pair, basis_eids):
            if pair not in deleted and b.has_row(*pair):
                deleted.add(pair)
                basis[pair] = set(basis_eids)
                work.append(pair)

        # 种子：对每条净消失边 (x,y)，所有可达 x 的源 s（含 x 自身）
        # 经过该边能到达的全部既有闭包行都先过度删除，再交幸存判据收敛。
        for (x, y), eids in removed_pairs.items():
            basis_seed = set(eids)
            for s in set(b.sources_reaching(x)) | {x}:
                upstream = set(basis.get((s, x), ()))
                seed((s, y), upstream | basis_seed)
                for t, witness in b.iter_source_rows(y):
                    seed((s, t), upstream | basis_seed | set(witness))
        while work:
            x, z = work.popleft()
            stats['delete_cascade_steps'] += 1
            for y in b.successors(z):
                pair = (x, y)
                if pair in deleted:
                    continue
                if self._has_surviving_derivation(x, y, deleted):
                    continue
                # 全部推导分支都已断：直接边（若有）或经过删除集内行
                pair_basis = set()
                if pair in removed_pairs:
                    pair_basis.update(removed_pairs[pair])
                for w in b.predecessors(y):
                    if (x, w) in deleted:
                        pair_basis.update(basis.get((x, w), ()))
                if not pair_basis:
                    pair_basis = set(basis.get((x, z), ()))
                deleted.add(pair)
                basis[pair] = pair_basis
                work.append(pair)
        return deleted, basis

    # DRed 重导阶段 -----------------------------------------------------------
    def _rederive(self, deleted, stats):
        """在批后图（含本批新边）上只凭现存边重导删除集中的行。"""
        b = self.b
        red = {}
        work = deque()
        # 幸存直接边
        for x, y in sorted(deleted):
            if b.edge_exists(x, y):
                red[(x, y)] = (b.evidence_id_for(x, y),)
                work.append((x, y))
        # 经由未被删除的可达行的首步推导
        for x, y in sorted(deleted):
            if (x, y) in red:
                continue
            for z in b.predecessors(y):
                if (x, z) not in deleted and b.has_row(x, z):
                    red[(x, y)] = b.get_witness(x, z) + (
                        b.evidence_id_for(z, y),)
                    work.append((x, y))
                    break
        # 半朴素扩散：重导行沿现存边继续延伸
        while work:
            x, z = work.popleft()
            for y in b.successors(z):
                pair = (x, y)
                if pair not in deleted or pair in red:
                    continue
                red[pair] = red[(x, z)] + (b.evidence_id_for(z, y),)
                work.append(pair)
        return red

    # 半朴素插入 --------------------------------------------------------------
    def _insert_edges(self, added_edges, stats):
        """新增逻辑边后的闭包增量，按受影响源做 BFS 固定点。

        受影响源 = 新边起点 u 本身，以及所有（含本批新建立的）可达 u 的源。
        对每个源只 BFS 尚不在闭包中的目标，避免重算未受影响行；
        这样新建立的 (s,u) 行也能回头沿新边 (u,v) 传播，不会漏推导。
        """
        b = self.b
        inserted = 0
        # 受影响源工作集：新边起点自身 + 闭包中全部上游源。
        # 用固定点工作队列沿 sources_reaching 反向闭包链扩张，
        # 多跳上游（如 n4->n8->n1，新边在 n8）不会被漏掉。
        affected = set()
        work_sources = deque()
        for _, u, _ in added_edges:
            if u not in affected:
                affected.add(u)
                work_sources.append(u)
        while work_sources:
            u = work_sources.popleft()
            for s in b.sources_reaching(u):
                if s not in affected:
                    affected.add(s)
                    work_sources.append(s)
        for source in sorted(affected):
            stats['insert_cascade_steps'] += 1
            seen = set()
            queue = deque()
            # 从源的全部现存后继进入：旧行也要继续向下探索，
            # 否则经由旧行才能到达的新目标会被漏掉；仅新目标计数入闭包。
            for z in b.successors(source):
                if z in seen:
                    continue
                seen.add(z)
                if not b.has_row(source, z):
                    witness = (b.evidence_id_for(source, z),)
                    b.put_row(source, z, witness)
                    inserted += 1
                else:
                    witness = b.get_witness(source, z)
                queue.append((z, witness))
            while queue:
                node, node_witness = queue.popleft()
                for nxt in b.successors(node):
                    if nxt in seen:
                        continue
                    seen.add(nxt)
                    new_witness = node_witness + (
                        b.evidence_id_for(node, nxt),)
                    if not b.has_row(source, nxt):
                        b.put_row(source, nxt, new_witness)
                        inserted += 1
                    queue.append((nxt, new_witness))
        return inserted

    # 见证修复 ----------------------------------------------------------------
    def _find_witness(self, u, v):
        """在当前边集中 BFS 找一条 u 到 v 的真实路径，找不到返回 None。"""
        b = self.b
        enqueued = set()
        queue = deque([(u, ())])
        while queue:
            node, witness = queue.popleft()
            for nxt in sorted(b.successors(node)):
                if nxt in enqueued:
                    continue
                new_witness = witness + (b.evidence_id_for(node, nxt),)
                if nxt == v:
                    return new_witness
                enqueued.add(nxt)
                queue.append((nxt, new_witness))
        return None

    def _repair_witnesses(self, removed_evidence_pairs, removed_eids):
        """扫描全部闭包行，凡见证引用了本批被撤证据就重建一条真实路径。

        直接遍历闭包内存镜像按证据id集合判定，避免靠受影响源收集时
        因环或自环漏扫；“撤旧补新”下边仍存在，只换见证不删行。
        """
        b = self.b
        repairs = 0
        for u, v, witness in list(b.iter_all_rows()):
            if not any(eid in removed_eids for eid in witness):
                continue
            new_witness = self._find_witness(u, v)
            if new_witness is None:
                raise IntegrityError(
                    '行 %s->%s 已无真实路径却仍在闭包中' % (u, v))
            b.put_row(u, v, new_witness)
            repairs += 1
        return repairs

    # 查询与校验 --------------------------------------------------------------
    def reachable(self, u, v):
        """正长度可达查询；未知节点按不可达处理。"""
        if not self.b.has_node(u) or not self.b.has_node(v):
            return False
        return self.b.has_row(u, v)

    def explain(self, u, v):
        """返回构成真实来源路径的证据序列，不可达返回 None。"""
        if not self.b.has_row(u, v):
            return None
        witness = self.b.get_witness(u, v)
        return [
            {'evidence_id': eid, 'from': endpoints[0], 'to': endpoints[1]}
            for eid, endpoints in ((e, self.b.get_evidence(e)) for e in witness)
        ]

    def check_witnesses(self):
        """逐行核验见证证据仍存在且首尾链接正确，返回问题列表。"""
        problems = []
        for u, v, witness in self.b.iter_all_rows():
            if not witness:
                problems.append((u, v, '空见证'))
                continue
            cursor = u
            for eid in witness:
                if not self.b.has_evidence(eid):
                    problems.append((u, v, '证据 %s 已不存在' % eid))
                    break
                eu, ev = self.b.get_evidence(eid)
                if eu != cursor:
                    problems.append((u, v, '见证在 %s 处断链' % eid))
                    break
                cursor = ev
            else:
                if cursor != v:
                    problems.append((u, v, '见证终点不匹配'))
        return problems

    def export_dataset(self):
        """导出当前 nodes/edges 全量资料，供与全量算法对照。"""
        return {
            'nodes': sorted(self.b.iter_nodes()),
            'edges': [
                {'evidence_id': eid, 'from': endpoints[0], 'to': endpoints[1]}
                for eid, endpoints in sorted(self.b.iter_evidence())
            ],
        }

    def closure_pairs(self):
        return {(u, v) for u, v, _ in self.b.iter_all_rows()}
