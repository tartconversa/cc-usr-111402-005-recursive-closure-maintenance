"""增量递归视图的差分、环处理、证据与快照版本测试。"""
import copy
import json
import random
import tempfile
import unittest
from pathlib import Path

from app.baseline import execute as full_execute
from app.graphview import (
    IntegrityError, MemoryBackend, ReachabilityView, SqliteBackend,
)
from app.snapshot import SnapshotStore
from app import queries


def baseline_pairs(data):
    return {tuple(p) for p in full_execute(copy.deepcopy(data))}


def make_view(backend, data):
    view = ReachabilityView(backend())
    view.load_dataset(copy.deepcopy(data))
    return view


class RandomDifferentialTests(unittest.TestCase):
    """随机图上每批变更后与全量 BFS 闭包逐项对照。"""

    def _run_fuzz(self, backend_factory, seed, nodes_n, rounds):
        rng = random.Random(seed)
        nodes = ['n%d' % i for i in range(nodes_n)]
        data = {'snapshot': 'g', 'nodes': nodes, 'edges': []}
        view = make_view(backend_factory, data)
        eid_seq = 0
        for round_no in range(rounds):
            changes = []
            existing = list(data['edges'])
            rng.shuffle(existing)
            # 先确定撤销（仅针对本轮开始前已存在的证据），再确定新增
            for edge in existing[:rng.randint(0, min(2, len(existing)))]:
                changes.append({'op': 'remove_evidence',
                                'evidence_id': edge['evidence_id']})
                data['edges'].remove(edge)
            for _ in range(rng.randint(0, 3)):
                u, v = rng.choice(nodes), rng.choice(nodes)
                eid_seq += 1
                changes.append({'op': 'add_evidence',
                                'evidence_id': 'f%d' % eid_seq,
                                'from': u, 'to': v})
                data['edges'].append(
                    {'evidence_id': 'f%d' % eid_seq, 'from': u, 'to': v})
            try:
                view.apply(changes)
            except IntegrityError:
                raise
            self.assertEqual(view.closure_pairs(), baseline_pairs(data),
                             msg='seed=%s round=%s changes=%s'
                                 % (seed, round_no, changes))
            self.assertEqual(view.check_witnesses(), [])
            # 每个闭包行的见证必须是真实路径
            for u, v in view.closure_pairs():
                path = view.explain(u, v)
                self.assertIsNotNone(path)
                cursor = u
                for step in path:
                    self.assertEqual(step['from'], cursor)
                    cursor = step['to']
                self.assertEqual(cursor, v)
        view.b.close()

    def test_fuzz_memory(self):
        for seed in range(12):
            self._run_fuzz(MemoryBackend, seed, nodes_n=8, rounds=60)

    def test_fuzz_sqlite(self):
        with tempfile.TemporaryDirectory() as td:
            for seed in range(4):
                db = Path(td) / ('fuzz-%s.sqlite3' % seed)

                def factory():
                    return SqliteBackend(db)

                self._run_fuzz(factory, seed, nodes_n=7, rounds=40)


class CycleAndEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            'snapshot': 'g',
            'nodes': ['a', 'b', 'c', 'd'],
            'edges': [
                {'evidence_id': 'e1', 'from': 'a', 'to': 'b'},
                {'evidence_id': 'e2', 'from': 'b', 'to': 'c'},
                {'evidence_id': 'e3', 'from': 'c', 'to': 'b'},
            ],
        }

    def test_only_nodes_on_cycle_have_self_rows(self):
        view = make_view(MemoryBackend, self.data)
        pairs = view.closure_pairs()
        self.assertIn(('b', 'b'), pairs)
        self.assertIn(('c', 'c'), pairs)
        self.assertNotIn(('a', 'a'), pairs)
        self.assertNotIn(('d', 'd'), pairs)

    def test_breaking_cycle_removes_self_rows_with_impact_basis(self):
        view = make_view(MemoryBackend, self.data)
        result = view.apply(
            [{'op': 'remove_evidence', 'evidence_id': 'e3'}])
        self.assertFalse(result['stats']['full_rebuild'])
        deleted = {(r['from'], r['to']): r['basis']
                   for r in result['impact']['deleted_rows']}
        self.assertEqual(set(deleted), {('b', 'b'), ('c', 'b'), ('c', 'c')})
        self.assertEqual(deleted[('b', 'b')], ['e3'])
        self.assertNotIn(('b', 'b'), view.closure_pairs())
        self.assertNotIn(('c', 'c'), view.closure_pairs())
        # 另一条路径仍支持的关系必须保留
        self.assertIn(('a', 'c'), view.closure_pairs())
        self.assertIn(('b', 'c'), view.closure_pairs())

    def test_parallel_evidence_keeps_edge_and_repairs_witness(self):
        view = make_view(MemoryBackend, self.data)
        view.apply([{'op': 'add_evidence', 'evidence_id': 'px',
                     'from': 'b', 'to': 'c'}])
        result = view.apply(
            [{'op': 'remove_evidence', 'evidence_id': 'e2'}])
        self.assertEqual(result['stats']['edges_removed'], 0)
        self.assertEqual(result['stats']['rows_deleted'], 0)
        self.assertGreater(result['stats']['witness_repairs'], 0)
        self.assertEqual(view.closure_pairs(), baseline_pairs({
            **self.data,
            'edges': [e for e in self.data['edges']
                      if e['evidence_id'] != 'e2']
            + [{'evidence_id': 'px', 'from': 'b', 'to': 'c'}],
        }))
        self.assertEqual(view.check_witnesses(), [])
        # 修复后的见证引用的是仍存在的证据
        path = view.explain('a', 'c')
        self.assertEqual([s['evidence_id'] for s in path], ['e1', 'px'])

    def test_alternate_path_survives_single_edge_removal(self):
        # 菱形：a->b->d 与 a->c->d，删一条边不应抹掉 a->d
        data = {
            'snapshot': 'g', 'nodes': ['a', 'b', 'c', 'd'],
            'edges': [
                {'evidence_id': 'ab', 'from': 'a', 'to': 'b'},
                {'evidence_id': 'ac', 'from': 'a', 'to': 'c'},
                {'evidence_id': 'bd', 'from': 'b', 'to': 'd'},
                {'evidence_id': 'cd', 'from': 'c', 'to': 'd'},
            ],
        }
        view = make_view(MemoryBackend, data)
        result = view.apply(
            [{'op': 'remove_evidence', 'evidence_id': 'bd'}])
        # a->d 仍由 a->c->d 支持；只有直接行 b->d 真正失去
        self.assertIn(('a', 'd'), view.closure_pairs())
        self.assertEqual(
            [(r['from'], r['to']) for r in result['impact']['deleted_rows']],
            [('b', 'd')])
        self.assertEqual(
            [s['evidence_id'] for s in view.explain('a', 'd')],
            ['ac', 'cd'])

    def test_local_change_is_not_full_rebuild(self):
        view = make_view(MemoryBackend, self.data)
        result = view.apply([{'op': 'add_evidence', 'evidence_id': 'z1',
                              'from': 'a', 'to': 'd'}])
        self.assertFalse(result['stats']['full_rebuild'])
        self.assertGreaterEqual(result['stats']['reused_rows'], 1)

    def test_export_dataset_matches_live_graph(self):
        view = make_view(MemoryBackend, self.data)
        view.apply([{'op': 'add_evidence', 'evidence_id': 'px',
                     'from': 'b', 'to': 'c'}])
        exported = view.export_dataset()
        self.assertEqual(exported['nodes'], ['a', 'b', 'c', 'd'])
        self.assertEqual({e['evidence_id'] for e in exported['edges']},
                         {'e1', 'e2', 'e3', 'px'})
        # 导出资料喂给全量 baseline，结果必须与增量闭包一致
        self.assertEqual({tuple(p) for p in full_execute(exported)},
                         view.closure_pairs())

    def test_force_rebuild_flag(self):
        view = make_view(MemoryBackend, self.data)
        result = view.apply(
            [{'op': 'add_evidence', 'evidence_id': 'z1',
              'from': 'a', 'to': 'd'}], force_rebuild=True)
        self.assertTrue(result['stats']['full_rebuild'])
        self.assertEqual(result['stats']['reused_rows'], 0)

    def test_failed_batch_rolls_back_sqlite(self):
        import tempfile
        from pathlib import Path
        from app.graphview import SqliteBackend
        with tempfile.TemporaryDirectory() as td:
            view = ReachabilityView(SqliteBackend(Path(td) / 'r.sqlite3'))
            view.load_dataset(self.data)
            before = view.closure_pairs()
            before_edges = dict(view.b.iter_evidence())

            # 预检在任何变更前拦截：未知端点拒绝且无副作用
            with self.assertRaises(ValueError):
                view.apply([
                    {'op': 'add_evidence', 'evidence_id': 'ok1',
                     'from': 'a', 'to': 'd'},
                    {'op': 'add_evidence', 'evidence_id': 'bad1',
                     'from': 'a', 'to': 'ghost'},
                ])
            self.assertEqual(view.closure_pairs(), before)
            self.assertEqual(dict(view.b.iter_evidence()), before_edges)

            # 维护阶段抛错（见证校验失败）时，未提交事务回滚，连接仍可用
            original = view.check_witnesses
            view.check_witnesses = lambda: [('x', 'y', 'forced')]
            with self.assertRaises(IntegrityError):
                view.apply([{'op': 'add_evidence', 'evidence_id': 'ok2',
                             'from': 'a', 'to': 'd'}])
            view.check_witnesses = original
            self.assertEqual(view.closure_pairs(), before)
            self.assertEqual(dict(view.b.iter_evidence()), before_edges)
            # 回滚后连接仍能正常服务下一次成功发布
            ok = view.apply([{'op': 'add_evidence', 'evidence_id': 'ok3',
                              'from': 'a', 'to': 'd'}])
            self.assertTrue(ok['stats']['rows_inserted'] >= 1)
            view.b.close()

    def test_empty_and_noop_batches(self):
        view = make_view(MemoryBackend, self.data)
        before = view.closure_pairs()
        result = view.apply([])
        self.assertFalse(result['stats']['full_rebuild'])
        self.assertEqual(view.closure_pairs(), before)
        # 撤销一条不存在的证据直接拒绝，状态不变
        with self.assertRaises(ValueError):
            view.apply([{'op': 'remove_evidence', 'evidence_id': 'nope'}])
        self.assertEqual(view.closure_pairs(), before)

    def test_duplicate_and_unknown_endpoint_rejected(self):
        view = make_view(MemoryBackend, self.data)
        with self.assertRaisesRegex(ValueError, '重复'):
            view.apply([{'op': 'add_evidence', 'evidence_id': 'e1',
                         'from': 'a', 'to': 'b'}])
        with self.assertRaisesRegex(ValueError, '不存在'):
            view.apply([{'op': 'remove_evidence', 'evidence_id': 'nope'}])
        with self.assertRaisesRegex(ValueError, '端点'):
            view.apply([{'op': 'add_evidence', 'evidence_id': 'x',
                         'from': 'a', 'to': 'ghost'}])

    def test_node_lifecycle_requires_no_incident_evidence(self):
        # 有 incident 证据的节点拒绝移除；孤立节点可移除后再加回
        view = make_view(MemoryBackend, self.data)
        with self.assertRaisesRegex(ValueError, 'incident'):
            view.apply([{'op': 'remove_node', 'node': 'a'}])
        view.apply([{'op': 'remove_node', 'node': 'd'}])
        self.assertNotIn('d', view.b.nodes)
        view.apply([{'op': 'add_node', 'node': 'e'},
                    {'op': 'add_evidence', 'evidence_id': 'ae',
                     'from': 'a', 'to': 'e'}])
        self.assertTrue(view.reachable('a', 'e'))
        self.assertFalse(view.reachable('e', 'a'))


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            'snapshot': 'graph-1',
            'nodes': ['a', 'b', 'c'],
            'edges': [
                {'evidence_id': 'e1', 'from': 'a', 'to': 'b'},
                {'evidence_id': 'e2', 'from': 'b', 'to': 'c'},
                {'evidence_id': 'e3', 'from': 'c', 'to': 'b'},
            ],
        }

    def test_versioned_publish_and_reuse(self):
        with tempfile.TemporaryDirectory() as td:
            store = SnapshotStore(td)
            p1 = store.initialize(self.data, 'graph-1')
            self.assertTrue(p1['stats']['full_rebuild'])
            self.assertEqual((Path(td) / 'current').read_text(), 'graph-1')
            p2 = store.apply_change(
                self.data, 'graph-2',
                [{'op': 'add_evidence', 'evidence_id': 'e9',
                  'from': 'a', 'to': 'c'}])
            self.assertFalse(p2['stats']['full_rebuild'])
            self.assertEqual(p2['parent'], 'graph-1')
            # 同一版本内边与闭包一致
            view = store.load_view('graph-2')
            try:
                self.assertEqual(view.check_witnesses(), [])
                self.assertEqual(
                    view.closure_pairs(),
                    baseline_pairs({**self.data, 'edges': self.data['edges']
                                    + [{'evidence_id': 'e9', 'from': 'a',
                                        'to': 'c'}]}))
            finally:
                view.b.close()
            # 父版本目录未被改动，仍可回读
            old = store.load_view('graph-1')
            try:
                self.assertEqual(old.closure_pairs(),
                                 baseline_pairs(self.data))
            finally:
                old.b.close()
            manifest = json.loads(
                (Path(td) / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['current'], 'graph-2')
            self.assertEqual([v['version'] for v in manifest['versions']],
                             ['graph-1', 'graph-2'])

    def test_snapshot_name_is_data_identity_not_gate(self):
        # 资料自带的 snapshot 名是输入身份，不门控版本发布；
        # 版本归属由 store 的 parent 链管理
        with tempfile.TemporaryDirectory() as td:
            store = SnapshotStore(td)
            store.initialize(self.data, 'graph-1')
            other = dict(self.data, snapshot='source-system-x')
            publish = store.apply_change(
                other, 'graph-2',
                [{'op': 'add_evidence', 'evidence_id': 'e9',
                  'from': 'a', 'to': 'c'}])
            self.assertEqual(publish['parent'], 'graph-1')
            self.assertEqual(store.current_version(), 'graph-2')
            with self.assertRaisesRegex(ValueError, '未初始化'):
                SnapshotStore(Path(td) / 'fresh').apply_change(
                    other, 'x', [])

    def test_sqlite_store_chain_and_physical_copy(self):
        with tempfile.TemporaryDirectory() as td:
            store = SnapshotStore(Path(td) / 'store')
            p1 = store.initialize(self.data, 'v1')
            self.assertEqual(p1['backend'], 'memory')
            # 直接以 sqlite 后端建库走溢写路径
            db = Path(td) / 'big.sqlite3'
            view = ReachabilityView(SqliteBackend(db))
            view.load_dataset(copy.deepcopy(self.data))
            result = view.apply(
                [{'op': 'remove_evidence', 'evidence_id': 'e3'}])
            self.assertFalse(result['stats']['full_rebuild'])
            self.assertEqual(view.closure_pairs(), baseline_pairs({
                **self.data,
                'edges': [e for e in self.data['edges']
                          if e['evidence_id'] != 'e3'],
            }))
            view.b.close()

    def test_query_helpers_and_impact_filter(self):
        with tempfile.TemporaryDirectory() as td:
            store = SnapshotStore(td)
            store.initialize(self.data, 'graph-1')
            publish = store.apply_change(
                self.data, 'graph-2',
                [{'op': 'remove_evidence', 'evidence_id': 'e3'}])
            view = store.load_view('graph-2')
            try:
                self.assertTrue(queries.reaches(view, 'a', 'c'))
                self.assertFalse(queries.reaches(view, 'b', 'b'))
                self.assertEqual(
                    [s['evidence_id'] for s in
                     queries.source_path(view, 'a', 'c')],
                    ['e1', 'e2'])
                self.assertEqual(queries.dependents(view, 'c'), ['a', 'b'])
            finally:
                view.b.close()
            filtered = queries.impact(
                publish, {'op': 'eq', 'field': 'to', 'value': 'b'})
            self.assertEqual({(r['from'], r['to'])
                              for r in filtered['deleted_rows']},
                             {('b', 'b'), ('c', 'b')})


if __name__ == '__main__':
    unittest.main()
