# -*- coding: utf-8 -*-
"""PAIM IA PROSPECT - app.py VER51: Brasil (paim_engine) + America Latina (latam_engine), CORS, erros em JSON,
protecao de memoria e LOGIN.

Autenticacao (novo nesta versao):
- O servidor entrega o index.html SOMENTE depois do login (index.html fica ao lado do app.py, no repositorio).
- Todas as rotas /api/* exigem sessao, exceto /api/health e o preflight OPTIONS.
- Usuarios: variavel de ambiente PAIM_USERS (JSON {"usuario": "hash"}). Hash gerado com gerar_hash.py.
- Sessao: cookie assinado (HttpOnly, Secure, SameSite=Lax). Exige SECRET_KEY fixa no Render.
- Revogacao imediata (VER52): a sessao guarda a impressao digital do hash. Trocou a senha ou removeu o
  usuario no PAIM_USERS do Render -> o Render reinicia -> a sessao antiga cai no proximo clique.
- Sem PAIM_USERS valido o sistema fica TRANCADO (ninguem entra). Nunca abre por falta de configuracao.

Protecao de memoria (VER50):
- Uma busca por vez (lock); segunda busca recebe 429.
- Teto de raio/limite por variavel de ambiente (MAX_KM, MAX_LIMIT). Padrao 250 / 1500.
- Log de memoria (RSS) no inicio, a cada 15 s e no fim da busca.
"""
from __future__ import annotations
import os
import json
import time
import html
import hashlib
import logging
import secrets
import tempfile
import threading
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

from flask import (Flask, Response, jsonify, make_response, redirect, request,
                   session, stream_with_context)

import paim_engine as engine
import latam_engine as latam
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

# static_folder=None: nenhum arquivo do repositorio e servido por acaso (so as rotas abaixo).
app = Flask(__name__, static_folder=None)

# Sob gunicorn, usa o mesmo handler do gunicorn para que app.logger.info() apareca nos logs do Render.
if __name__ != '__main__':
    _gunicorn_logger = logging.getLogger('gunicorn.error')
    if _gunicorn_logger.handlers:
        app.logger.handlers = _gunicorn_logger.handlers
        app.logger.setLevel(_gunicorn_logger.level or logging.INFO)


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Tetos de busca. Padrao = comportamento anterior. No Render Free, reduza via variavel de ambiente
# (ex.: MAX_KM=60 e MAX_LIMIT=200) se a instancia continuar reiniciando.
MAX_KM = max(5, _env_int('MAX_KM', 250))
MAX_LIMIT = max(1, _env_int('MAX_LIMIT', 1500))

# ----------------------------------------------------------------------------------------------
# AUTENTICACAO
# ----------------------------------------------------------------------------------------------
SESSION_HOURS = max(1, _env_int('SESSION_HOURS', 12))

_secret = os.environ.get('SECRET_KEY', '').strip()
if not _secret:
    # Sem SECRET_KEY fixa, todo reinicio do Render derruba todas as sessoes.
    _secret = secrets.token_hex(32)
    app.logger.warning('SECRET_KEY nao definida: usando chave aleatoria. Defina SECRET_KEY no Render.')
app.secret_key = _secret
app.config.update(
    SESSION_COOKIE_NAME='paim_session',
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    # Render serve HTTPS. Para teste local em http, defina SESSION_COOKIE_SECURE=0.
    SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', '1') != '0',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
)


def _load_users():
    raw = os.environ.get('PAIM_USERS', '').strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('esperado um objeto JSON {"usuario": "hash"}')
        return {str(k).strip().lower(): str(v) for k, v in data.items() if str(k).strip() and v}
    except Exception as exc:
        app.logger.error('PAIM_USERS invalido: %s', exc)
        return {}


USERS = _load_users()
if not USERS:
    app.logger.error('Nenhum usuario configurado (PAIM_USERS). O sistema ficara trancado ate configurar.')

def _user_fp(stored_hash):
    """Impressao digital do hash atual do usuario. Vai na sessao: se a senha mudar no Render
    (ou o usuario for removido), a sessao antiga deixa de valer no proximo clique."""
    return hashlib.sha256(stored_hash.encode('utf-8')).hexdigest()[:16]


def session_still_valid():
    user = session.get('user')
    if not user:
        return False
    stored = USERS.get(user)
    if stored is None or session.get('fp') != _user_fp(stored):
        app.logger.warning('sessao REVOGADA usuario=%r (removido ou senha alterada)', user[:60])
        session.clear()
        return False
    return True


# Hash falso para gastar o mesmo tempo quando o usuario nao existe (nao revela quais usuarios existem).
_DUMMY_HASH = generate_password_hash('senha-falsa-para-equalizar-tempo', method='pbkdf2:sha256:600000')


def verify_login(username, password):
    stored = USERS.get(username.strip().lower())
    if stored is None:
        check_password_hash(_DUMMY_HASH, password)
        return False
    try:
        return check_password_hash(stored, password)
    except Exception:
        return False


# Bloqueio de tentativas por usuario: 5 erros em 10 minutos bloqueiam o login desse usuario por 10 minutos.
MAX_FAILS = 5
FAIL_WINDOW = 600
_fails = {}
_fails_lock = threading.Lock()


def _fail_key(username):
    return username.strip().lower()[:120]


def locked_seconds(username):
    key, now = _fail_key(username), time.time()
    with _fails_lock:
        rec = _fails.get(key)
        if not rec:
            return 0
        count, first = rec
        if now - first > FAIL_WINDOW:
            _fails.pop(key, None)
            return 0
        return int(FAIL_WINDOW - (now - first)) if count >= MAX_FAILS else 0


def register_fail(username):
    key, now = _fail_key(username), time.time()
    with _fails_lock:
        if len(_fails) > 2000:
            for k in [k for k, (_, first) in _fails.items() if now - first > FAIL_WINDOW]:
                _fails.pop(k, None)
            if len(_fails) > 2000:
                _fails.clear()
        count, first = _fails.get(key, (0, now))
        if now - first > FAIL_WINDOW:
            count, first = 0, now
        _fails[key] = (count + 1, first)


def clear_fails(username):
    with _fails_lock:
        _fails.pop(_fail_key(username), None)


LOGIN_HTML = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PAIM IA Prospect · Acesso</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#eef1f5;
font:14px/1.5 "IBM Plex Sans","Segoe UI",system-ui,Arial,sans-serif;color:#14203a}
.card{width:100%;max-width:380px;margin:20px;background:#fff;border-radius:14px;padding:32px 28px;
box-shadow:0 10px 30px rgba(8,37,74,.15);border-top:5px solid #f58220}
h1{margin:0 0 2px;font-size:20px;color:#08254a;letter-spacing:.3px}
p.sub{margin:0 0 22px;color:#5b6b85;font-size:13px}
label{display:block;margin:14px 0 5px;font-weight:600;font-size:12px;color:#08254a;text-transform:uppercase;letter-spacing:.4px}
input{width:100%;padding:11px 12px;border:1px solid #c5cfdf;border-radius:8px;font:inherit}
input:focus{outline:2px solid #0a3d7a;border-color:#0a3d7a}
button{width:100%;margin-top:22px;padding:12px;border:0;border-radius:8px;background:#0a3d7a;color:#fff;
font:inherit;font-weight:700;cursor:pointer}
button:hover{background:#08254a}
.erro{margin-top:16px;padding:10px 12px;border-radius:8px;background:#fdeceb;color:#9b1c1c;font-size:13px}
.foot{margin-top:20px;text-align:center;font-size:11px;color:#8a97ad}
</style></head><body>
<form class="card" method="post" action="/login" autocomplete="on">
<h1>PAIM IA PROSPECT</h1>
<p class="sub">Bertolini Intralogística · acesso restrito</p>
<label for="usuario">Usuário</label>
<input id="usuario" name="usuario" type="text" autocomplete="username" autofocus required>
<label for="senha">Senha</label>
<input id="senha" name="senha" type="password" autocomplete="current-password" required>
<button type="submit">Entrar</button>
{{ERRO}}
<div class="foot">Paim Business Intelligence</div>
</form></body></html>"""

LOGOUT_SNIPPET = """
<form method="post" action="/logout" style="position:fixed;right:14px;bottom:14px;z-index:99999;margin:0">
<button type="submit" title="Encerrar sessão" style="background:#08254a;color:#fff;border:0;border-radius:20px;
padding:7px 14px;font:600 12px 'IBM Plex Sans','Segoe UI',Arial,sans-serif;cursor:pointer;opacity:.9;
box-shadow:0 2px 8px rgba(0,0,0,.3)">Sair · {{USUARIO}}</button></form>
"""

INDEX_PATH = Path(__file__).resolve().with_name('index.html')

# Unicas rotas acessiveis sem login.
PUBLIC_PATHS = {'/login', '/api/health'}


def login_page(message='', status=200):
    erro = f'<div class="erro">{html.escape(message)}</div>' if message else ''
    resp = make_response(LOGIN_HTML.replace('{{ERRO}}', erro), status)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.before_request
def require_login():
    if request.method == 'OPTIONS':  # preflight CORS nao leva cookie
        return None
    if request.path in PUBLIC_PATHS or session_still_valid():
        return None
    if request.path.startswith('/api/'):
        return jsonify({
            'ok': False,
            'auth': False,
            'error': 'Sessão expirada ou não autenticada. Entre em /login.'
        }), 401
    return redirect('/login')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        if session_still_valid():
            return redirect('/')
        return login_page()

    username = (request.form.get('usuario') or '').strip()
    password = request.form.get('senha') or ''
    if not USERS:
        return login_page('Acesso ainda não configurado no servidor. Fale com o administrador.', 503)
    wait_s = locked_seconds(username)
    if wait_s:
        app.logger.warning('login BLOQUEADO usuario=%r', username[:60])
        return login_page(f'Muitas tentativas. Aguarde {max(1, wait_s // 60)} min e tente de novo.', 429)
    if verify_login(username, password):
        clear_fails(username)
        session.clear()
        session['user'] = username.strip().lower()
        session['fp'] = _user_fp(USERS[session['user']])
        session.permanent = True
        app.logger.info('login OK usuario=%r', username[:60])
        return redirect('/')
    register_fail(username)
    app.logger.warning('login FALHOU usuario=%r', username[:60])
    return login_page('Usuário ou senha inválidos.', 401)


@app.post('/logout')
def logout():
    session.clear()
    return redirect('/login')


@app.get('/')
def index():
    if not INDEX_PATH.exists():
        return jsonify({'ok': False, 'error': 'index.html não encontrado ao lado do app.py no servidor.'}), 500
    page = INDEX_PATH.read_text(encoding='utf-8')
    snippet = LOGOUT_SNIPPET.replace('{{USUARIO}}', html.escape(session.get('user', '')))
    pos = page.lower().rfind('</body>')
    page = page + snippet if pos == -1 else page[:pos] + snippet + page[pos:]
    resp = make_response(page)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


# ----------------------------------------------------------------------------------------------
# MOTOR
# ----------------------------------------------------------------------------------------------
# Origem exata do GitHub Pages. Pode ser sobrescrita no Render por CORS_ORIGINS.
ALLOWED_ORIGINS = {
    origin.strip().rstrip('/')
    for origin in os.environ.get(
        'CORS_ORIGINS',
        # 'null' = HTML aberto direto do computador (file://), usado nos testes locais.
        'https://paimlab-coder.github.io,null'
    ).split(',')
    if origin.strip()
}

# Render Free usa filesystem efemero. Arquivos temporarios/cache ficam em /tmp.
runtime = Path(tempfile.gettempdir()) / 'paim_prospect_runtime'
runtime.mkdir(parents=True, exist_ok=True)
engine.TARGET = runtime
engine.CACHE = runtime / 'cache_overture'
engine.MUNICIPIOS_CSV = runtime / 'municipios_brasil.csv'
engine.CITY.clear()

_startup_error = None
try:
    engine.load_municipalities()
except Exception as exc:
    _startup_error = f'{type(exc).__name__}: {exc}'

# Uma busca por vez. O lock e liberado pela propria thread da busca, ao terminar.
_search_lock = threading.Lock()
_search_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='paim-search')


def rss_mb():
    """Memoria residente atual do processo, em MB (Linux). None se indisponivel."""
    try:
        with open('/proc/self/status', encoding='ascii', errors='ignore') as fh:
            for line in fh:
                if line.startswith('VmRSS:'):
                    return round(int(line.split()[1]) / 1024)
    except Exception:
        pass
    return None


def _run_search(fn, args, label):
    started = time.time()
    app.logger.info('prospect INICIO %s | rss=%s MB', label, rss_mb())
    try:
        return fn(*args)
    finally:
        app.logger.info('prospect FIM %s | %.1fs | rss=%s MB', label, time.time() - started, rss_mb())
        _search_lock.release()


def cors_origin():
    origin = (request.headers.get('Origin') or '').rstrip('/')
    return origin if origin in ALLOWED_ORIGINS else None


@app.after_request
def add_cors_headers(response):
    origin = cors_origin()
    if origin:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Vary'] = 'Origin'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Accept'
        response.headers['Access-Control-Max-Age'] = '86400'
    return response


# Qualquer erro vira JSON (com CORS pelo after_request) em vez de "Failed to fetch" no navegador.
@app.errorhandler(Exception)
def handle_any_error(exc):
    code = exc.code if isinstance(exc, HTTPException) else 500
    if code >= 500:
        app.logger.exception('Erro nao tratado em %s', request.path)
    return jsonify({'ok': False, 'error': f'{type(exc).__name__}: {exc}'}), code


# CNAE escolhido sem segmento: usa o segmento correspondente (o motor trabalha por segmento).
CNAE_TO_SEGMENT = {
    '5211-7/99': 'Depósitos de mercadorias para terceiros, exceto armazéns gerais e guarda-móveis',
    '5210-9/99': 'Depósitos de mercadorias para terceiros, exceto armazéns gerais e guarda-móveis',
    '5211-7/01': 'Operador logístico', '5210-9/01': 'Operador logístico',
    '5250-8/04': 'Operador logístico',
    '5211-7/02': 'Centro de distribuição', '5210-9/02': 'Centro de distribuição',
    '4930-2/02': 'Transportadora',
    '1099-6/99': 'Alimentos',
    '1113-5/02': 'Bebidas',
    '3101-2/00': 'Moveleira',
    '2121-1/01': 'Farmacêutica',
    '4790-3/02': 'E-commerce',
}


# Responde o preflight sem passar pela rotina pesada de prospeccao.
@app.route('/api/<path:any_path>', methods=['OPTIONS'])
def api_options(any_path):
    origin = cors_origin()
    if not origin:
        return jsonify({'ok': False, 'error': 'Origem CORS não autorizada.'}), 403
    return make_response('', 204)


# Informacoes do servico (antes ficavam em "/"; agora "/" entrega o sistema). Exige login.
@app.get('/api/info')
def info():
    return jsonify({
        'ok': True,
        'service': 'PAIM IA PROSPECT API',
        'version': '50-latam',
        'source': 'Overture Maps',
        'municipalities': len(engine.CITY),
        'latam_countries': sorted(latam.SUPPORTED),
        'startup_error': _startup_error,
        'allowed_origins': sorted(ALLOWED_ORIGINS),
        'max_km': MAX_KM,
        'max_limit': MAX_LIMIT,
        'auth_users': len(USERS),
        'session_hours': SESSION_HOURS,
    })


@app.get('/api/health')
def health():
    status = 200 if not _startup_error and len(engine.CITY) >= 5500 else 503
    return jsonify({
        'ok': status == 200,
        'version': '50-latam',
        'source': 'Overture Maps',
        'municipalities': len(engine.CITY),
        'latam_countries': sorted(latam.SUPPORTED),
        'error': _startup_error,
        'busy': _search_lock.locked(),
        'rss_mb': rss_mb(),
    }), status


@app.route('/api/prospect', methods=['GET','POST'])
def prospect_api():
    payload = request.args.to_dict() if request.method == 'GET' else (request.get_json(silent=True) or {})
    iso = str(payload.get('country_iso', 'BR') or 'BR').strip().upper()
    if iso == 'BR' and _startup_error:
        return jsonify({'ok': False, 'error': _startup_error}), 503
    try:
        city = str(payload.get('city', '')).strip()
        region = str(payload.get('uf', '')).strip()
        uf = region.upper()
        segment = str(payload.get('segment', '')).strip()
        if not segment:
            segment = CNAE_TO_SEGMENT.get(str(payload.get('cnae', '')).strip(), '')
        segment_es = str(payload.get('segment_es', '')).strip()
        km = max(5, min(MAX_KM, int(payload.get('km', 20))))
        limit = max(1, min(MAX_LIMIT, int(payload.get('limit', 50))))
        min_score = max(0, min(100, int(payload.get('min_score', 0))))

        if not city or not uf or not segment:
            return jsonify({
                'ok': False,
                'error': 'Estado/província, cidade e segmento (ou CNAE) são obrigatórios.'
            }), 400

        if iso == 'BR':
            task = (engine.prospect, (city, uf, segment, km, limit, min_score))
        elif iso in latam.SUPPORTED:
            task = (latam.prospect, (iso, region, city, segment, segment_es, km, limit, min_score))
        else:
            return jsonify({'ok': False, 'error': f'País não suportado: {iso}.'}), 400

        # Uma busca por vez: evita varias buscas pesadas ao mesmo tempo na mesma instancia (RAM do plano Free).
        if not _search_lock.acquire(blocking=False):
            return jsonify({
                'ok': False,
                'busy': True,
                'error': 'Já existe uma busca em andamento no servidor. '
                         'Aguarde ela terminar (pode levar alguns minutos) e tente de novo.'
            }), 429

        label = f'{iso}/{uf}/{city}/{segment[:40]} km={km} limit={limit} user={session.get("user")}'
        try:
            future = _search_pool.submit(_run_search, task[0], task[1], label)
        except Exception:
            _search_lock.release()
            raise

        def generate():
            # Mantém a conexão HTTP ativa durante a busca por blocos.
            # Espaços antes do JSON são válidos e evitam inatividade prolongada.
            try:
                yield '\n'
                while True:
                    done, _ = wait([future], timeout=15)
                    if not done:
                        app.logger.info('prospect em andamento | rss=%s MB', rss_mb())
                        yield ' '
                        continue
                    try:
                        data = future.result()
                        data['requested_km'] = km
                        data['applied_limit'] = limit
                        yield json.dumps(data, ensure_ascii=False)
                    except Exception as exc:
                        yield json.dumps({
                            'ok': False,
                            'error': f'{type(exc).__name__}: {exc}'
                        }, ensure_ascii=False)
                    break
            except GeneratorExit:
                app.logger.warning('prospect: cliente desconectou; a busca segue ate terminar (%s)', label)
                raise

        return Response(
            stream_with_context(generate()),
            mimetype='application/json; charset=utf-8',
            headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'}
        )
    except Exception as exc:
        return jsonify({
            'ok': False,
            'error': f'{type(exc).__name__}: {exc}'
        }), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '10000'))
    app.run(host='0.0.0.0', port=port)
