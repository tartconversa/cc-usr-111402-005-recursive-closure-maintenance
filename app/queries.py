"""面向业务的依赖查询与影响范围筛选。

查询只回答正长度可达关系；explain 给出一条真实存在的来源证据路径；
impact 汇总删除后不再可达的闭包行，并可用受限三值谓词筛选。
"""
from .semantics import where as predicate_matches


def reaches(view, source, target):
    """是否存在 source -> target 的正长度路径。"""
    return view.reachable(source, target)


def source_path(view, source, target):
    """返回构成真实来源路径的边列表（证据id+端点），不可达为 None。"""
    return view.explain(source, target)


def dependents(view, target):
    """所有存在正长度路径到达 target 的包，键序输出。"""
    return sorted(view.b.sources_reaching(target))


def impact(store_or_publish, expression=None):
    """读取一次发布的影响范围，可选三值谓词筛选影响行。

    expression 为 None 或 {'op': ...} 结构，作用于 {'from','to','basis'} 行；
    basis 为撤销依据的证据id列表。
    """
    publish = store_or_publish
    if hasattr(store_or_publish, 'read_publish'):
        publish = store_or_publish.read_publish()
    rows = publish['impact']['deleted_rows']
    if expression is not None:
        rows = [row for row in rows if predicate_matches(row, expression)]
    return {
        'version': publish['version'],
        'affected_sources': publish['impact']['affected_sources'],
        'deleted_rows': rows,
        'stats': publish['stats'],
    }
