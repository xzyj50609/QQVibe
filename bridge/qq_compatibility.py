"""Version evidence and structural capabilities, never a guessed QQ version."""
import re

ADAPTER_VERSION='qce-readonly-http-v1'
VERSION=re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$')


def known_version(value):
    return value if isinstance(value,str) and len(value)<=64 and VERSION.fullmatch(value) else 'unknown'


def identify(info,status):
    version=known_version(info.get('version'))
    if version=='unknown':raise ValueError('unsupported-version')
    napcat=info.get('napcat') or {}
    verified=version.split('-',1)[0].split('+',1)[0]=='6.3.0'
    return {'state':'verified-qce-api' if verified else 'unverified-version',
        'qceVersion':version,'qqVersion':known_version(napcat.get('qqVersion') or info.get('qqVersion')),
        'napcatVersion':known_version(napcat.get('version')),'adapterVersion':ADAPTER_VERSION,
        'requiresConsent':not verified,
        'capabilities':{'identityEnvelope':'checked','loggedInStatus':'checked' if 'loggedIn' in status else 'unknown',
            'stableMessageIdentity':'not-yet-checked','boundedPagination':'not-yet-checked','groupMetadata':'not-yet-checked'},
        'qqCombinationVerified':False}
