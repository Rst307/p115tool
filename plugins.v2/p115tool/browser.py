"""Read-only virtual browsing over unified media identity, never remote listings."""
from .models import MediaObject
from .strm import virtual_path


class VirtualBrowser:
    def __init__(self, db):
        self.db = db

    def filters(self, prefix, query, storage, status):
        directory = '/' if prefix == '/' else virtual_path(prefix.rstrip('/')) + '/'
        if len(directory) > 4096 or len(query) > 256 or any(ord(c) < 32 for c in query):
            raise ValueError('Invalid browse criteria')
        terms, args = ['substr(virtual_path,1,?)=?'], [len(directory), directory]
        if query:
            terms.append('(instr(lower(title),lower(?))>0 OR instr(lower(file_name),lower(?))>0 OR instr(lower(virtual_path),lower(?))>0)')
            args.extend([query] * 3)
        if storage:
            terms.append('storage_type=?')
            args.append(storage)
        if status:
            terms.append('status=?')
            args.append(status)
        return directory, ' AND '.join(terms), args

    def media(self, prefix='/', query='', storage=None, status=None, offset=0, limit=50):
        directory, where, args = self.filters(prefix, query, storage, status)
        with self.db.connect() as connection:
            total = connection.execute('SELECT COUNT(*) FROM media WHERE ' + where, args).fetchone()[0]
            rows = connection.execute('SELECT * FROM media WHERE ' + where + ' ORDER BY virtual_path,id LIMIT ? OFFSET ?',
                (*args, limit, offset)).fetchall()
            return {'items': [MediaObject(**dict(row)).public() for row in rows], 'total': total,
                'offset': offset, 'limit': limit, 'prefix': directory, 'query': query}

    def tree(self, prefix='/', query='', storage=None, status=None, offset=0, limit=50):
        directory, where, args = self.filters(prefix, query, storage, status)
        sql = '''WITH filtered AS (
            SELECT id,size,storage_type,status,substr(virtual_path,?) remainder FROM media WHERE ''' + where + '''),
            segments AS (SELECT *, instr(remainder,'/')>0 is_directory,
                CASE WHEN instr(remainder,'/')>0 THEN substr(remainder,1,instr(remainder,'/')-1) ELSE remainder END name
                FROM filtered),
            entries AS (SELECT name,is_directory,CASE WHEN is_directory THEN NULL ELSE MIN(id) END media_id,
                COUNT(*) media_count, SUM(size) bytes,
                SUM(storage_type='NORMAL') normal_count,SUM(storage_type='SHARE') share_count,
                SUM(storage_type='CACHE') cache_count,
                SUM(status='BROKEN' OR status LIKE 'FAILED_%') abnormal_count
                FROM segments GROUP BY name,is_directory)
            '''
        with self.db.connect() as connection:
            params = (len(directory) + 1, *args)
            total = connection.execute(sql + 'SELECT COUNT(*) FROM entries', params).fetchone()[0]
            rows = connection.execute(sql + 'SELECT * FROM entries ORDER BY is_directory DESC,name,media_id LIMIT ? OFFSET ?',
                (*params, limit, offset)).fetchall()
            entries = []
            for row in rows:
                item = dict(row)
                item['is_directory'] = bool(item['is_directory'])
                item['path'] = directory + item['name'] + ('/' if item['is_directory'] else '')
                item['key'] = 'directory:' + item['path'] if item['is_directory'] else 'media:' + str(item['media_id'])
                entries.append(item)
            return {'items': entries, 'total': total, 'offset': offset, 'limit': limit,
                'prefix': directory, 'parent': None if directory == '/' else directory.rstrip('/').rsplit('/',1)[0] + '/', 'query': query}
