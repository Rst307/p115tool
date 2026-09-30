"""Static, credential-free management shell. Data stays behind existing API auth."""
from pathlib import Path
from fastapi.responses import Response

ASSETS = {'html': ('index.html', 'text/html'), 'js': ('app.js', 'application/javascript'),
          'css': ('style.css', 'text/css')}


def asset(kind):
    filename, media_type = ASSETS[kind]
    return Response((Path(__file__).parent / 'web' / filename).read_bytes(), media_type=media_type,
        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                 'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
                 'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"})
