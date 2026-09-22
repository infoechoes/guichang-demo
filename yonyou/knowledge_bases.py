"""Read-only knowledge-base selection. No order append or index refresh."""
from local_catalog import INDEX, LABEL, search_catalog
from contextlib import contextmanager
from contextvars import ContextVar

DEFAULT_ID = 'default-catalog'
_registry = ContextVar('knowledge_registry', default=None)


@contextmanager
def using(registry):
    token = _registry.set(registry)
    try: yield
    finally: _registry.reset(token)


def resolve(identity=None):
    if _registry.get() is not None: return _registry.get().resolve(identity)
    if identity in (None, ''):
        identity = DEFAULT_ID  # Compatibility with existing profiles and drafts.
    if identity != DEFAULT_ID:
        raise ValueError('所选知识库不存在或未开放，请重新选择知识库')
    return {'id': DEFAULT_ID, 'name': LABEL}


def catalogues():
    if _registry.get() is not None: return _registry.get().catalogues()
    return {'knowledgeBases': [resolve()], 'defaultId': DEFAULT_ID}


def search(identity, query, row, page):
    if _registry.get() is not None: return _registry.get().search(identity, query, row, page)
    base = resolve(identity)
    result = search_catalog(query, row, page, index_path=INDEX)
    return {**result, 'knowledgeBaseId': base['id'], 'candidates': [
        {**item, 'knowledgeBaseId': base['id']} for item in result['candidates']]}
