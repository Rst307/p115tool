"""Deletion intent ledger, not proof of recycle-bin ownership or permanent purge."""
import time
from .models import SafetyError


def delete(service, mid, purpose, file):
    if purpose not in ('SOURCE', 'CACHE') or file.is_dir:
        raise SafetyError('Only verified media file deletion can enter the ledger')
    with service.db.connect() as connection:
        if connection.execute("SELECT id FROM recycle_intents WHERE file_id=? AND state='REQUESTED'", (file.file_id,)).fetchone():
            raise SafetyError('Uncertain deletion intent must be reconciled before another request')
        cursor = connection.execute('''INSERT INTO recycle_intents
            (media_id,purpose,file_id,parent_id,name,sha1,size,state,requested_at)
            VALUES(?,?,?,?,?,?,?,'REQUESTED',?)''',
            (mid,purpose,file.file_id,file.parent_id,file.name,file.sha1.upper(),file.size,time.time()))
        intent_id = cursor.lastrowid
    # Committed before the SDK boundary. Exceptions leave REQUESTED, never replay.
    service.client.delete(file.file_id)
    service.db.execute("UPDATE recycle_intents SET state='ACKNOWLEDGED',observed_at=? WHERE id=?", (time.time(),intent_id))
    return intent_id


def observe(service, mid, purpose, file_id, absent, file=None):
    intent = service.db.one("SELECT * FROM recycle_intents WHERE media_id=? AND purpose=? AND file_id=? ORDER BY id DESC LIMIT 1",
        (mid,purpose,file_id))
    if not intent:
        return  # Pre-migration operation: do not invent historical ownership.
    if intent['state'] not in ('REQUESTED', 'ACKNOWLEDGED'):
        return
    if not absent:
        if file is None or (file.file_id,file.parent_id,file.name,file.sha1.upper(),file.size,file.is_dir) != (
                intent['file_id'],intent['parent_id'],intent['name'],intent['sha1'],intent['size'],False):
            raise SafetyError('Retained file differs from deletion intent identity')
    service.db.execute("UPDATE recycle_intents SET state=?,observed_at=? WHERE id=?",
        ('ABSENT' if absent else 'RETAINED',time.time(),intent['id']))


def history(service, limit=50, offset=0):
    return service.db.all('''SELECT id,media_id,purpose,file_id,parent_id,name,sha1,size,state,requested_at,observed_at
        FROM recycle_intents ORDER BY id DESC LIMIT ? OFFSET ?''', (limit,offset))
