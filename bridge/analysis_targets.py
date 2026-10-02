"""Shared typed conversation/target decisions, independent of display names."""
import hashlib
SELF_SUBJECT = 'self'


def is_group(source, user):
    kind = getattr(source, 'conversation_kind', None)
    if callable(kind):
        value = kind(user)
        if value not in ('friend', 'group'):
            raise ValueError('conversation-kind-invalid')
        return value == 'group'
    # Compatibility belongs to the legacy WeChat adapter contract. QQ always
    # declares its conversation kind and never enters this suffix fallback.
    if getattr(source, 'kind', None) == 'qq':
        raise ValueError('conversation-kind-unavailable')
    return user.endswith('@chatroom')


def subject(source, user, member=None):
    if is_group(source, user):
        return member or ''
    if getattr(source, 'kind', None) == 'qq' and member == SELF_SUBJECT:
        return SELF_SUBJECT
    if member is not None:
        raise ValueError('invalid-analysis-target')
    return user


def message_subject(source, user, item):
    if is_group(source, user):
        return item.get('senderId') or ''
    if getattr(source, 'kind', None) == 'qq' and item.get('side') == 'self':
        return SELF_SUBJECT
    return user


def target(item, subject, *, is_group=False):
    if item.get('side') not in ('self', 'other'):
        return False
    if is_group:
        return bool(item.get('senderId')) and (not subject or item['senderId'] == subject)
    return item['side'] == ('self' if subject == SELF_SUBJECT else 'other')


def labels_eligible(source, item):
    return item.get('side') == 'other' or (getattr(source, 'kind', None) == 'qq' and item.get('side') == 'self')


def portrait_version(base_version, source, user, member=None):
    if getattr(source,'kind',None)!='qq' or member is None:
        return base_version
    target_id=subject(source,user,member)
    digest=hashlib.sha256(target_id.encode('utf-8')).hexdigest()[:16]
    # Peer cache keys remain exactly as before. New targets each have an
    # independent staging generation so a failed self/member rebuild cannot
    # overwrite another person's published portrait.
    return base_version+':qq-target-v1:'+digest
