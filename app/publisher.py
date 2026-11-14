"""增量发布入口：把边证据、快照目录与查询结果接到同一版本上。

用法::

    python -m app.publisher init   <store目录> <完整资料.json> <版本名>
    python -m app.publisher change <store目录> <资料.json> <版本名> <变更计划.json> [--force-rebuild]

变更计划格式（incremental-v1）::

    {"changes": [
      {"op": "add_node", "node": "e"},
      {"op": "add_evidence", "evidence_id": "e9", "from": "a", "to": "e"},
      {"op": "remove_evidence", "evidence_id": "e2"}
    ]}
"""
import json
import sys

from .storage import read_json
from .snapshot import SnapshotStore


def publish_initial(store_dir, data, version):
    store = SnapshotStore(store_dir)
    return store.initialize(data, version)


def publish_change(store_dir, data, version, changes, force_rebuild=False):
    store = SnapshotStore(store_dir)
    return store.apply_change(data, version, changes,
                              force_rebuild=force_rebuild)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 4:
        raise SystemExit(__doc__)
    command, store_dir, data_path, version = argv[:4]
    data = read_json(data_path)
    if command == 'init':
        publish = publish_initial(store_dir, data, version)
    elif command == 'change':
        if len(argv) < 5:
            raise SystemExit('change 需要变更计划 JSON 路径')
        plan = read_json(argv[4])
        force = len(argv) > 5 and argv[5] == '--force-rebuild'
        publish = publish_change(
            store_dir, data, version, plan['changes'], force_rebuild=force)
    else:
        raise SystemExit('未知命令: %s（init|change）' % command)
    print(json.dumps(publish, ensure_ascii=False, allow_nan=False, indent=2))


if __name__ == '__main__':
    main()
