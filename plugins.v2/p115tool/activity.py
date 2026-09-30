"""Host-visible activity without request data, exception text or credentials."""
import logging


def activity(operation, state, mid=None):
    try:
        from app.log import logger
    except ImportError:
        logger = logging.getLogger('p115tool')
    message = f'115 工具箱：{operation} · {state}'
    if type(mid) is int:
        message += f' · 媒体ID={mid}'
    # Deliberately exclude detail: even SDK exception representations can leak.
    if state.startswith('FAILED') or state == 'NEEDS_ATTENTION':
        logger.warning(message)
    else:
        logger.info(message)
