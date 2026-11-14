"""版本化快照目录：直接边与闭包同一版本原子发布。

目录布局::

    store/
      manifest.json            # 全部版本清单与指针
      current                  # 当前版本标签（文件内为版本名）
      v1/
        edges.json             # 该版本直接边（含证据）与节点全集
        closure.json           # 该版本闭包行（含见证），内存后端使用
        closure.sqlite3        # 该版本闭包库，sqlite 后端使用
        publish.json           # 该版本统计与影响范围
      v2/ ...

发布策略：新版本先在独立目录写完整内容，再以一次原子替换切换 current；
sqlite 后端从父版本复制库文件后做增量维护，未受影响的物理页天然复用，
且发布期间父版本目录保持不变，可随时回读。
"""
import json
import shutil
from pathlib import Path

from .storage import digest
from .graphview import (
    ReachabilityView, MemoryBackend, SqliteBackend, choose_backend,
)

MANIFEST_NAME = 'manifest.json'
CURRENT_NAME = 'current'
FORMAT_VERSION = 'incremental-v1'
SQLITE_FILENAME = 'closure.sqlite3'


def _atomic_write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2,
                   sort_keys=True),
        encoding='utf-8')
    tmp.replace(path)


def _atomic_write_text(path, text):
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    tmp.replace(path)


class SnapshotStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / MANIFEST_NAME
        if self.manifest_path.exists():
            self.manifest = json.loads(self.manifest_path.read_text(encoding='utf-8'))
        else:
            self.manifest = {
                'format_version': FORMAT_VERSION,
                'current': None,
                'versions': [],
            }
            self._save_manifest()

    def _save_manifest(self):
        _atomic_write_json(self.manifest_path, self.manifest)

    def current_version(self):
        return self.manifest['current']

    def _version_dir(self, version):
        return self.root / version

    def _dump_dataset(self, view):
        b = view.b
        return {
            'snapshot_format': FORMAT_VERSION,
            'nodes': sorted(b.iter_nodes()),
            'edges': [
                {'evidence_id': eid, 'from': endpoints[0], 'to': endpoints[1]}
                for eid, endpoints in sorted(b.iter_evidence())
            ],
        }

    def _dump_closure(self, view):
        return [
            {'from': u, 'to': v, 'witness': list(witness)}
            for u, v, witness in sorted(view.b.iter_all_rows())
        ]

    def _backend_name(self, view):
        return 'sqlite' if isinstance(view.b, SqliteBackend) else 'memory'

    def _publish(self, view, version, stats, impact, parent, change_plan):
        version_dir = self._version_dir(version)
        if version_dir.exists():
            raise ValueError('版本目录已存在: %s' % version)
        version_dir.mkdir(parents=True)
        edges = self._dump_dataset(view)
        _atomic_write_json(version_dir / 'edges.json', edges)
        backend_name = self._backend_name(view)
        if backend_name == 'sqlite':
            view.b.commit()
            shutil.copyfile(view.b.path, version_dir / SQLITE_FILENAME)
        else:
            _atomic_write_json(version_dir / 'closure.json',
                               self._dump_closure(view))
        edges_digest = digest(edges)
        publish = {
            'format_version': FORMAT_VERSION,
            'version': version,
            'parent': parent,
            'backend': backend_name,
            'edges_digest': edges_digest,
            'change_plan': change_plan,
            'stats': stats,
            'impact': impact,
        }
        _atomic_write_json(version_dir / 'publish.json', publish)
        entry = {
            'version': version,
            'parent': parent,
            'backend': backend_name,
            'edges_digest': edges_digest,
            'stats': stats,
        }
        self.manifest['versions'].append(entry)
        self.manifest['current'] = version
        self._save_manifest()
        _atomic_write_text(self.root / CURRENT_NAME, version)
        return publish

    def initialize(self, data, version):
        """从完整 nodes/edges 资料建立首个版本（一次显式全量重建）。"""
        if self.manifest['versions']:
            raise ValueError('快照库已初始化')
        backend_name = choose_backend(len(data['nodes']), len(data['edges']))
        work = self.root / ('_%s.work' % version)
        shutil.rmtree(work, ignore_errors=True)
        if backend_name == 'sqlite':
            db_path = work / SQLITE_FILENAME
            work.mkdir(parents=True)
            view = ReachabilityView(SqliteBackend(db_path))
        else:
            work.mkdir(parents=True)
            view = ReachabilityView(MemoryBackend())
        view.load_dataset(data)
        stats = dict(view.last_stats)
        impact = dict(view.last_impact)
        publish = self._publish(view, version, stats, impact, None, None)
        view.b.close()
        shutil.rmtree(work, ignore_errors=True)
        return publish

    def apply_change(self, data, version, changes, force_rebuild=False):
        """基于当前版本增量发布新版本。

        data 仅用于在需要时核对资料身份；snapshot 是输入资料自身声明的
        快照名（见资料契约），版本归属由 store 的 parent 链管理，
        不要求它等于当前已发布版本名。
        """
        parent = self.manifest['current']
        if parent is None:
            raise ValueError('快照库未初始化')
        parent_dir = self._version_dir(parent)
        entry = self.manifest['versions'][-1]
        backend_name = entry['backend']
        work = self.root / ('_%s.work' % version)
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        if backend_name == 'sqlite':
            parent_db = parent_dir / SQLITE_FILENAME
            db_path = work / SQLITE_FILENAME
            # 复制父版本库：未受影响物理页直接复用，改动只发生在工作副本
            shutil.copyfile(parent_db, db_path)
            view = ReachabilityView(SqliteBackend(db_path))
        else:
            view = ReachabilityView(MemoryBackend())
            edges_doc = json.loads(
                (parent_dir / 'edges.json').read_text(encoding='utf-8'))
            closure_doc = json.loads(
                (parent_dir / 'closure.json').read_text(encoding='utf-8'))
            view.load_frozen(
                edges_doc['nodes'], edges_doc['edges'],
                [(r['from'], r['to'], tuple(r['witness']))
                 for r in closure_doc])
        result = view.apply(changes, force_rebuild=force_rebuild)
        plan_doc = {'snapshot': parent, 'format_version': FORMAT_VERSION,
                    'changes': changes}
        publish = self._publish(
            view, version, result['stats'], result['impact'], parent, plan_doc)
        view.b.close()
        shutil.rmtree(work, ignore_errors=True)
        return publish

    def load_view(self, version=None):
        """加载某个只读分析视图（默认当前版本），调用方负责 close。"""
        version = version or self.manifest['current']
        if version is None:
            raise ValueError('快照库未初始化')
        version_dir = self._version_dir(version)
        if not version_dir.exists():
            raise ValueError('未知版本: %s' % version)
        entry = next(v for v in self.manifest['versions']
                     if v['version'] == version)
        if entry['backend'] == 'sqlite':
            return ReachabilityView(SqliteBackend(version_dir / SQLITE_FILENAME))
        view = ReachabilityView(MemoryBackend())
        edges_doc = json.loads(
            (version_dir / 'edges.json').read_text(encoding='utf-8'))
        closure_doc = json.loads(
            (version_dir / 'closure.json').read_text(encoding='utf-8'))
        view.load_frozen(
            edges_doc['nodes'], edges_doc['edges'],
            [(r['from'], r['to'], tuple(r['witness']))
             for r in closure_doc])
        return view

    def read_publish(self, version=None):
        version = version or self.manifest['current']
        return json.loads(
            (self._version_dir(version) / 'publish.json').read_text(
                encoding='utf-8'))
