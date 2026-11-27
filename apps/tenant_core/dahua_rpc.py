"""
Shared Dahua RPC2 Client — used by both the cloud push (views_mobile.py)
and the studio daemon (sweat_turnstile_daemon.py).

Credentials come exclusively from environment variables:
  DAHUA_ADMIN_USER  (default: "admin")
  DAHUA_ADMIN_PASS  (must be set — no default in production paths)

All public functions are idempotent (safe to call repeatedly).
All exceptions are propagated; callers decide whether to log-and-continue
or abort.
"""

import hashlib
import os
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_ADMIN_USER: str = os.environ.get('DAHUA_ADMIN_USER', 'admin')
_ADMIN_PASS: str = os.environ.get('DAHUA_ADMIN_PASS', '')  # Must be set in production


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_credentials() -> tuple[str, str]:
    """Returns (username, password) from environment. Raises if password is empty."""
    user = os.environ.get('DAHUA_ADMIN_USER', _ADMIN_USER or 'admin')
    pw = os.environ.get('DAHUA_ADMIN_PASS', _ADMIN_PASS)
    if not pw:
        raise EnvironmentError(
            'DAHUA_ADMIN_PASS environment variable is not set. '
            'Set it before starting the application.'
        )
    return user, pw


def _dahua_login(base_url: str, timeout: int = 5) -> tuple[requests.Session, str]:
    """
    Performs the Dahua two-step MD5 Digest authentication handshake.

    Returns (session, session_id) on success.
    Raises RuntimeError if login fails.
    """
    user, pw = _get_credentials()
    sess = requests.Session()

    # Step 1: empty-password probe → server returns realm + random nonce
    r1 = sess.post(
        f'{base_url}/RPC2_Login',
        json={'method': 'global.login',
              'params': {'userName': user, 'password': '', 'clientType': 'Web3.0'},
              'id': 1},
        timeout=timeout,
    )
    r1.raise_for_status()
    d1 = r1.json()

    rand  = d1['params']['random']
    realm = d1['params']['realm']
    sid0  = d1['session']

    # Step 2: compute two-round MD5 and send real password hash
    p1 = hashlib.md5(f'{user}:{realm}:{pw}'.encode()).hexdigest().upper()
    p2 = hashlib.md5(f'{user}:{rand}:{p1}'.encode()).hexdigest().upper()

    r2 = sess.post(
        f'{base_url}/RPC2_Login',
        json={'method': 'global.login',
              'params': {'userName': user, 'password': p2,
                         'clientType': 'Web3.0', 'authorityType': 'Default'},
              'id': 2,
              'session': sid0},
        timeout=timeout,
    )
    r2.raise_for_status()
    d2 = r2.json()

    if not d2.get('result'):
        raise RuntimeError(f'Dahua login failed on {base_url}: {d2}')

    return sess, d2['session']


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register_card(
    base_url: str,
    user_id: str,
    display_name: str,
    card_no: str,
    timeout: int = 10,
) -> bool:
    """
    Idempotently registers a user + QR card token in a Dahua terminal's
    local access-control database.

    Steps:
      1. Login (two-step MD5 Digest)
      2. AccessUser.insertMulti  — creates the user (safe to call if exists)
      3. AccessCard.insertMulti  — registers the 22-char token (safe to call if exists)

    Returns True on success, False on any error (logs the error).
    """
    try:
        sess, sid = _dahua_login(base_url, timeout=timeout)

        # 1. Upsert user (terminal ignores duplicates silently)
        sess.post(
            f'{base_url}/RPC2',
            json={
                'method': 'AccessUser.insertMulti',
                'params': {'UserList': [{
                    'UserID':       user_id,
                    'UserName':     display_name[:31],  # 32-char terminal limit
                    'UserType':     0,                  # 0 = general user
                    'TimeSections': [255],              # 255 = unrestricted time section
                    'Doors':        [0],
                    'UseTime':      0,                  # 0 = unlimited
                }]},
                'id': 10, 'session': sid,
            },
            timeout=timeout,
        )

        # 2. Upsert card/QR (terminal ignores duplicates silently)
        r_card = sess.post(
            f'{base_url}/RPC2',
            json={
                'method': 'AccessCard.insertMulti',
                'params': {'CardList': [{
                    'CardNo':        card_no,
                    'UserID':        user_id,
                    'CardType':      1,              # 1 = normal access card
                    'UseTime':       0,              # 0 = unlimited
                    'ValidDateStart': '2026-01-01 00:00:00',
                    'ValidDateEnd':   '2099-12-31 23:59:59',
                }]},
                'id': 11, 'session': sid,
            },
            timeout=timeout,
        )
        rj = r_card.json()
        err = rj.get('error') or {}
        err_msg = str(err.get('message', ''))
        fail_codes = (err.get('detail') or {}).get('FailCodes') or []
        # 286064919 = Dahua/TimeWatch "Card already exists" batch error code
        result = bool(rj.get('result', False)) or 'exist' in err_msg.lower() or 286064919 in fail_codes
        if result:
            logger.debug('register_card OK: %s card=%s…', base_url, card_no[:6])
        else:
            logger.warning(
                'register_card returned result=False: %s card=%s… resp=%s',
                base_url, card_no[:6], rj,
            )
        return result

    except Exception as exc:
        logger.error('register_card failed: %s card=%s… err=%s', base_url, card_no[:6], exc)
        return False


def delete_cards(base_url: str, card_nos: list[str], timeout: int = 10, batch_size: int = 50) -> bool:
    """
    Batch-removes QR card tokens from the terminal's local database.
    Uses a single login session across chunks of `batch_size`.
    """
    if not card_nos:
        return True
    try:
        sess, sid = _dahua_login(base_url, timeout=timeout)
        all_ok = True
        for i in range(0, len(card_nos), batch_size):
            chunk = card_nos[i:i + batch_size]
            ok = False
            for offset, method in enumerate(('AccessCard.deleteMulti', 'AccessCard.removeMulti')):
                r = sess.post(
                    f'{base_url}/RPC2',
                    json={
                        'method': method,
                        'params': {'CardNoList': chunk},
                        'id': 20 + offset + i, 'session': sid,
                    },
                    timeout=timeout,
                )
                ok = bool(r.json().get('result', False))
                if ok:
                    break
                logger.warning('delete_cards %s failed on %s: %s', method, base_url, r.text[:200])
            all_ok = all_ok and ok
        return all_ok
    except Exception as exc:
        logger.error('delete_cards failed: %s count=%d err=%s', base_url, len(card_nos), exc)
        return False


def delete_card(base_url: str, card_no: str, timeout: int = 10) -> bool:
    """
    Removes a QR card token from the terminal's local database.
    Used by the stale-card cleanup job.

    Returns True on success, False on error.
    """
    return delete_cards(base_url, [card_no], timeout=timeout)


def open_door(base_url: str, label: str = '', timeout: int = 5) -> bool:
    """
    Sends the physical relay-open command to the terminal.
    Uses HTTP Digest auth (urllib) so credentials never travel in plaintext.

    Returns True if the terminal acknowledged, False on error.
    """
    import urllib.request

    user, pw = _get_credentials()
    url = f'{base_url}/cgi-bin/accessControl.cgi?action=openDoor&channel=1'
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, url, user, pw)
    opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))

    try:
        resp   = opener.open(url, timeout=timeout)
        result = resp.read().decode('utf-8', errors='replace').strip()
        logger.info('open_door OK: %s label=%r result=%r', base_url, label, result)
        return True
    except Exception as exc:
        logger.error('open_door FAILED: %s label=%r err=%s', base_url, label, exc)
        return False


def subscribe_events(base_url: str, timeout: int = 300):
    """
    Generator: connects to the terminal's HTTP event stream and yields
    raw CardNo strings as they arrive.

    Usage:
        for card_no in subscribe_events(device.base_url):
            handle(card_no)

    The generator reconnects automatically on network errors (callers
    should wrap in a while True loop with a small sleep on exception).
    Raises StopIteration only when the caller breaks out.
    """
    import re
    import urllib.request as _ur

    user, pw = _get_credentials()
    url = f'{base_url}/cgi-bin/eventManager.cgi?action=attach&codes=[All]'
    mgr = _ur.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, url, user, pw)
    opener = _ur.build_opener(_ur.HTTPDigestAuthHandler(mgr))

    with opener.open(url, timeout=timeout) as resp:
        buffer = ''
        while True:
            chunk = resp.read(512)
            if not chunk:
                break
            buffer += chunk.decode('utf-8', errors='ignore')
            while '--myboundary' in buffer:
                part, buffer = buffer.split('--myboundary', 1)
                if 'Code=AccessControl' not in part and 'CardNo' not in part:
                    continue
                m = re.search(r'["\']CardNo["\']\s*:\s*["\']([^"\']+)["\']', part)
                if m:
                    card_no = m.group(1).strip()
                    if card_no:
                        yield card_no
