"""QQ authored text, quoted authors and mentions remain separate roles."""
import hashlib
import json
from qq_identity import canonical_uin
from qq_entities import UID


def pseudo(member):
    return 'P_'+hashlib.sha256(member.encode('utf-8')).hexdigest()[:12] if isinstance(member,str) and member else None


def projection(row,resolve):
    try:
        raw=json.loads(row['raw']) if isinstance(row['raw'],str) and len(row['raw'])<=2_000_000 else {}
    except (ValueError,RecursionError):
        raw={}
    if not isinstance(raw,dict):raw={}
    quote=None;mentions=[]
    quoted=None
    content=raw.get('content') or {}
    if isinstance(content,dict) and isinstance(content.get('reply'),dict):
        quoted=content['reply']
    elements=raw.get('elements') or []
    if isinstance(elements,list):
        for element in elements[:256]:
            if not isinstance(element,dict):continue
            reply=element.get('replyElement')
            if isinstance(reply,dict):
                reference=reply.get('sourceMsgIdInRecords') or reply.get('replayMsgId')
                records=raw.get('records') or []
                if reference is not None and isinstance(records,list):
                    quoted=next((item for item in records[:256] if isinstance(item,dict) and str(item.get('msgId'))==str(reference)),quoted)
            text=element.get('textElement')
            if isinstance(text,dict) and text.get('atType') in (1,2):
                mentions.append({'kind':'all' if text['atType']==1 else 'user',
                    'senderId':None if text['atType']==1 else resolve(text.get('atUid'),text.get('atNtUid'))})
    if isinstance(content,dict) and isinstance(content.get('mentions'),list):
        for item in content['mentions'][:64]:
            if isinstance(item,dict) and item.get('type') in ('all','user'):
                mentions.append({'kind':item['type'],'senderId':None if item['type']=='all' else resolve(None,item.get('uid'))})
    if isinstance(row['quote'],str):
        sender=(quoted.get('sender') or {}) if isinstance(quoted,dict) else {}
        if not isinstance(sender,dict):sender={}
        q=quoted if isinstance(quoted,dict) else {}
        quote={'id':str(q.get('referencedMessageId') or q.get('msgId')) if q.get('referencedMessageId') or q.get('msgId') else None,
            'senderId':resolve(sender.get('uin') or q.get('senderUin'),sender.get('uid') or q.get('senderUid')),
            'senderName':None,'text':row['quote'][:4000],'sentAtMs':None}
    return quote,mentions[:64]


def group_context(item):
    speaker=pseudo(item.get('senderId'))
    if not speaker:raise ValueError('group-speaker-required')
    context={'speaker':speaker,'time':item.get('time',0),'quote':None,'mentions':[]}
    quote=item.get('quote')
    if isinstance(quote,dict) and isinstance(quote.get('text'),str):
        context['quote']={'speaker':pseudo(quote.get('senderId')),'text':quote['text'][:4000]}
    for mention in (item.get('mentions') or [])[:64]:
        context['mentions'].append({'speaker':pseudo(mention.get('senderId')),'kind':mention['kind']})
    return context
