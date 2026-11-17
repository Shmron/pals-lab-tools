"""Standalone runner for poster judging. When joining ECRI, register the blueprint instead."""
import os
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix
from poster_judging import bp

app = Flask(__name__)
_key = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'secret_key')
if not os.path.exists(_key):
    import secrets
    open(_key, 'w').write(secrets.token_hex(32))
app.secret_key = open(_key).read().strip()
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.register_blueprint(bp, url_prefix=os.environ.get('POSTER_PREFIX', ''))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5020)))
