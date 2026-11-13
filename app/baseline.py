"""现有执行路径。"""
def execute(data):
    nodes=set(data['nodes']); graph={n:set() for n in nodes}; seen=set()
    for edge in data['edges']:
        if edge['evidence_id'] in seen: raise ValueError('证据键重复')
        seen.add(edge['evidence_id'])
        if edge['from'] not in nodes or edge['to'] not in nodes: raise ValueError('未知图端点')
        graph[edge['from']].add(edge['to'])
    result=[]
    for source in sorted(nodes):
        reachable=set(); pending=list(graph[source])
        while pending:
            node=pending.pop()
            if node in reachable: continue
            reachable.add(node); pending.extend(graph[node]-reachable)
        result.extend([[source,target] for target in sorted(reachable)])
    return result
