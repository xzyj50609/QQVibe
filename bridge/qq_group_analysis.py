"""Bounded QQ group label ranges and exact per-target input reuse."""
import hashlib
import json

GROUP_FINE_POLICY='qq-group-fine-v2'
NEIGHBOR_MS=10*60*1000


def neighborhood(window,index):
    target=window[index]
    return [item for item in window[max(0,index-3):index] if item.get('kind')=='text' and
            item.get('senderId') and 0<=target.get('time',0)-item.get('time',0)<=NEIGHBOR_MS]


def input_basis(account,user,base_version,window,index):
    def value(item):
        return [item['id'],item.get('side'),item.get('senderId'),item.get('time'),item.get('kind'),item.get('text'),
                item.get('quote'),item.get('mentions')]
    body=[account,user,base_version,GROUP_FINE_POLICY,[value(row) for row in neighborhood(window,index)],value(window[index])]
    return hashlib.sha256(json.dumps(body,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def range_version(base_version,window,limit):
    targets=window[-limit:]
    context=window[:-limit] if len(window)>limit else []
    descriptor=[[row['id'],row.get('senderId'),row.get('time')] for row in [*context,*targets]]
    digest=hashlib.sha256(json.dumps([GROUP_FINE_POLICY,descriptor,[row['id'] for row in targets]],separators=(',',':')).encode()).hexdigest()[:24]
    return base_version+':'+GROUP_FINE_POLICY+':'+digest


def stream_version(base_version):
    # The job identity stays stable when the latest display window moves. Each
    # committed message still carries its own exact, live-checked input basis.
    return base_version+':'+GROUP_FINE_POLICY+':stream'


def current_window(source,account,user,stable_id):
    """The target and its three preceding message positions, never new QCE IO."""
    with source.read_library(account) as (_,library):
        targets=library.connection.execute('SELECT * FROM messages WHERE account_key=? AND conversation_key=? '
            'AND native_id=? LIMIT 2',(account,user,stable_id)).fetchall()
        if len(targets)!=1:return []
        target=targets[0]
        previous=library.connection.execute('SELECT * FROM messages WHERE account_key=? AND conversation_key=? '
            'AND (time_ms,local_seq)<(?,?) ORDER BY time_ms DESC,local_seq DESC LIMIT 3',
            (account,user,target['time_ms'],target['local_seq'])).fetchall()
        revision=library.revision(account,user)[0]
        return [item for row in [*reversed(previous),target] if (item:=source.project_message(row,revision)) is not None]


def current_basis(source,account,user,base_version,stable_id):
    from analysis_targets import labels_eligible
    window=current_window(source,account,user,stable_id)
    if (not window or window[-1]['id']!=stable_id or not labels_eligible(source,window[-1]) or
        window[-1]['kind']!='text' or not window[-1]['text'].strip()):return None
    return input_basis(account,user,base_version,window,len(window)-1)


def reuse(store,account,user,base_version,stable_id,basis):
    prefix=base_version+':'+GROUP_FINE_POLICY+':'
    with store.connect() as db:
        rows=db.execute('SELECT result FROM fine_results_v1 WHERE account=? AND session=? AND id=? AND substr(version,1,?)=? ORDER BY rowid DESC',
            (account,user,stable_id,len(prefix),prefix))
        for (raw,) in rows:
            value=json.loads(raw)
            if value.get('qqInputBasis')==basis:
                return value
    return None


def history_results(store,account,user,base_version,revision,ids,*,source=None):
    """Display committed labels only when their precise current inputs match."""
    if not ids:return {}
    prefix=base_version+':'+GROUP_FINE_POLICY+':'
    with store.connect() as db:
        rows=db.execute('SELECT f.id,f.result FROM fine_results_v1 f '
            'WHERE f.account=? AND f.session=? AND substr(f.version,1,?)=? '
            'AND f.id IN ('+','.join('?' for _ in ids)+') ORDER BY f.rowid DESC',
            (account,user,len(prefix),prefix,*ids)).fetchall()
    results={}
    if source is None:return results
    candidates={stable_id for stable_id,_ in rows}
    bases={stable_id:current_basis(source,account,user,base_version,stable_id) for stable_id in candidates}
    for stable_id,raw in rows:
        value=json.loads(raw)
        if bases.get(stable_id) is not None and value.get('qqInputBasis')==bases[stable_id]:
            results.setdefault(stable_id,value)
    return results
