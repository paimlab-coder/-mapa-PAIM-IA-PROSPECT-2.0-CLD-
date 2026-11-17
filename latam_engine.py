# -*- coding: utf-8 -*-
"""
PAIM IA PROSPECT - motor de prospeccao para paises da America Latina (fora do Brasil).

Fluxo: geocodifica a cidade (Nominatim/OpenStreetMap, com cache em /tmp) ->
le os places do Overture Maps dentro do raio -> classifica pelo segmento
(categorias do Overture + palavras-chave em portugues/espanhol/ingles) ->
calcula o score ICP -> devolve linhas no mesmo formato que o front-end ja usa.

O Brasil continua no paim_engine original; este modulo so e chamado para country_iso != BR.
"""
from __future__ import annotations

import json
import math
import os
import re
import struct
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

SUPPORTED = {
    'AR': 'Argentina', 'BO': 'Bolivia', 'CL': 'Chile', 'CO': 'Colombia', 'EC': 'Ecuador',
    'MX': 'Mexico', 'PY': 'Paraguay', 'PE': 'Peru', 'UY': 'Uruguay',
}

WANTED_COLUMNS = ('id', 'names', 'categories', 'basic_category', 'taxonomy', 'confidence',
                  'websites', 'emails', 'phones', 'addresses', 'geometry', 'bbox')

# Teto de seguranca para o plano gratuito do Render (512 MB).
MAX_SCANNED = int(os.environ.get('LATAM_MAX_SCANNED', '150000'))

# A politica do Nominatim exige um User-Agent que identifique o app e um contato.
NOMINATIM_URL = os.environ.get('NOMINATIM_URL', 'https://nominatim.openstreetmap.org/search')
NOMINATIM_UA = os.environ.get('NOMINATIM_UA', 'PAIM-IA-Prospect/1.0 (paim-prospect-ia.onrender.com)')

RUNTIME = Path(tempfile.gettempdir()) / 'paim_prospect_runtime'
RUNTIME.mkdir(parents=True, exist_ok=True)
GEO_CACHE_FILE = RUNTIME / 'geocode_latam.json'
_geo_lock = threading.Lock()
_last_geo_call = [0.0]


def norm(v) -> str:
    s = unicodedata.normalize('NFD', str(v or ''))
    return ''.join(c for c in s if unicodedata.category(c) != 'Mn').lower().strip()


# ---------------------------------------------------------------- segmentos
# Cada segmento: termos procurados nas categorias do Overture (ingles) e no nome da empresa.
SEGMENTS = {
    'depositos de mercadorias para terceiros, exceto armazens gerais e guarda-moveis': (
        ['warehouse', 'storage', 'logistic', 'distribution'],
        ['deposito', 'almacen', 'bodega', 'logistica', 'storage', 'warehouse']),
    'operador logistico': (
        ['logistic', 'freight', 'cargo', 'shipping', 'warehouse', 'distribution', 'courier'],
        ['logistica', 'logistic', 'cargo', 'operador', 'almacen', 'bodega', 'deposito', '3pl']),
    'centro de distribuicao': (
        ['distribution', 'warehouse', 'wholesale', 'logistic'],
        ['distribucion', 'distribuidora', 'centro de distribucion', 'cedis', 'almacen', 'bodega']),
    'transportadora': (
        ['freight', 'trucking', 'cargo', 'moving', 'courier', 'transport', 'logistic'],
        ['transporte', 'transportes', 'fletes', 'cargas', 'camiones', 'logistica', 'mudanzas']),
    'alimentos': (
        ['food_production', 'food_manufactur', 'food_processing', 'bakery_wholesale', 'meat_wholesale', 'food_distribut'],
        ['alimentos', 'alimenticia', 'frigorifico', 'lacteos', 'molino', 'conservas', 'embutidos', 'panificadora']),
    'bebidas': (
        ['brewery', 'beverage', 'winery', 'distillery', 'bottl', 'soft_drink'],
        ['bebidas', 'cerveceria', 'cerveza', 'embotelladora', 'vinos', 'bodega de vinos', 'destileria']),
    'eletrodomesticos': (
        ['appliance', 'electronics_manufact'],
        ['electrodomesticos', 'linea blanca', 'appliance']),
    'autopecas': (
        ['auto_parts', 'automotive_parts', 'car_parts', 'tire'],
        ['autopartes', 'repuestos', 'refacciones', 'autopecas', 'neumaticos', 'llantas']),
    'metalurgica': (
        ['metal', 'steel', 'foundry', 'welding', 'machining'],
        ['metalurgica', 'metalmecanica', 'fundicion', 'aceros', 'metales', 'estructuras metalicas']),
    'moveleira': (
        ['furniture_manufact', 'furniture_wholesale', 'cabinet', 'woodwork'],
        ['muebles', 'muebleria', 'carpinteria', 'maderas']),
    'farmaceutica': (
        ['pharmaceutical', 'drugstore_wholesale', 'medical_supply', 'laboratory'],
        ['farmaceutica', 'laboratorio', 'laboratorios', 'drogueria', 'pharma']),
    'atacadista': (
        ['wholesale', 'distribut', 'wholesaler'],
        ['mayorista', 'distribuidora', 'importadora', 'comercializadora', 'al por mayor']),
    'supermercado': (
        ['supermarket', 'grocery', 'hypermarket', 'wholesale_store'],
        ['supermercado', 'hipermercado', 'autoservicio', 'mercado']),
    'e-commerce': (
        ['e_commerce', 'ecommerce', 'online_shop', 'internet_shop', 'fulfillment'],
        ['ecommerce', 'e-commerce', 'tienda online', 'fulfillment']),
    'agronegocio': (
        ['agricultur', 'farm', 'agro', 'grain', 'livestock', 'seed'],
        ['agro', 'agricola', 'agropecuaria', 'granos', 'semillas', 'acopio', 'cooperativa']),
    'fertilizantes': (
        ['fertilizer', 'agrochemical'],
        ['fertilizantes', 'agroquimicos', 'abonos']),
    'papel e celulose': (
        ['paper', 'pulp', 'packaging', 'cardboard'],
        ['papel', 'celulosa', 'carton', 'papelera', 'empaques', 'embalajes']),
    'plasticos': (
        ['plastic', 'packaging', 'polymer'],
        ['plasticos', 'plastico', 'polimeros', 'envases', 'empaques']),
    'quimica': (
        ['chemical', 'chemist_wholesale', 'industrial_gas', 'paint_manufact'],
        ['quimica', 'quimicos', 'pinturas', 'solventes', 'resinas']),
    'textil': (
        ['textile', 'clothing_manufact', 'garment', 'fabric'],
        ['textil', 'textiles', 'confecciones', 'hilanderia', 'telas']),
    'construcao': (
        ['construction', 'building_supplies', 'contractor', 'cement', 'hardware_wholesale'],
        ['construccion', 'constructora', 'materiales de construccion', 'cementos', 'ferreteria industrial']),
    'siderurgia': (
        ['steel', 'iron', 'metal_mill', 'foundry'],
        ['siderurgica', 'acerias', 'aceros', 'hierro', 'laminados']),
}


def segment_terms(segment: str, segment_es: str = ''):
    cats, names = SEGMENTS.get(norm(segment), ([], []))
    extra = [w for w in re.split(r'[\s,;/]+', norm(segment_es)) if len(w) > 3]
    if not cats and not names:
        # Segmento desconhecido: usa as palavras do proprio nome do segmento.
        words = [w for w in re.split(r'[\s,;/]+', norm(segment)) if len(w) > 3]
        return words, words + extra
    return cats, names + extra


# ---------------------------------------------------------------- geocodificacao
def _load_geo_cache() -> dict:
    try:
        return json.loads(GEO_CACHE_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _get_json(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': NOMINATIM_UA, 'Accept-Language': 'es,pt,en', **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


COUNTRY_QID = {'AR': 'Q414', 'BO': 'Q750', 'CL': 'Q298', 'CO': 'Q739', 'EC': 'Q736',
               'MX': 'Q96', 'PY': 'Q733', 'PE': 'Q419', 'UY': 'Q77'}
CITY_TYPES = {'city', 'town', 'village', 'municipality', 'locality', 'district', 'county'}


def _photon(city, region, iso):
    """Photon (komoot, dados OpenStreetMap): gratuito, sem chave, considera a provincia/estado."""
    q = urllib.parse.urlencode({'q': f'{city}, {region}, {SUPPORTED.get(iso, iso)}', 'limit': 10})
    data = _get_json('https://photon.komoot.io/api/?' + q)
    best = None
    for f in data.get('features') or []:
        pr = f.get('properties') or {}
        if (pr.get('countrycode') or '').upper() != iso:
            continue
        coords = (f.get('geometry') or {}).get('coordinates') or []
        if len(coords) < 2:
            continue
        rank = 0
        if norm(pr.get('name')) == norm(city):
            rank += 2
        st = norm(pr.get('state'))
        if region and st and (norm(region) in st or st in norm(region)):
            rank += 2
        if (pr.get('type') or pr.get('osm_value') or '') in CITY_TYPES or pr.get('osm_value') in CITY_TYPES:
            rank += 1
        if best is None or rank > best[0]:
            best = (rank, float(coords[1]), float(coords[0]), ', '.join(x for x in [pr.get('name'), pr.get('state'), pr.get('country')] if x))
    return best[1:] if best else None


def _wikidata(city, iso):
    """Wikidata (CC0): cidade com o pais certo (P17) e coordenadas (P625)."""
    W = 'https://www.wikidata.org/w/api.php?'
    s = _get_json(W + urllib.parse.urlencode({'action': 'wbsearchentities', 'search': city, 'language': 'es',
                                               'type': 'item', 'limit': 8, 'format': 'json'}))
    ids = [x['id'] for x in s.get('search') or []]
    if not ids:
        return None
    e = _get_json(W + urllib.parse.urlencode({'action': 'wbgetentities', 'ids': '|'.join(ids), 'props': 'claims|labels',
                                               'languages': 'es', 'format': 'json'}))
    for i in ids:
        c = ((e.get('entities') or {}).get(i) or {}).get('claims') or {}
        pais = [x.get('mainsnak', {}).get('datavalue', {}).get('value', {}).get('id') for x in c.get('P17', [])]
        coord = [x.get('mainsnak', {}).get('datavalue', {}).get('value') for x in c.get('P625', [])]
        coord = [v for v in coord if isinstance(v, dict) and 'latitude' in v]
        if COUNTRY_QID.get(iso) in pais and coord:
            return float(coord[0]['latitude']), float(coord[0]['longitude']), f'{city} (Wikidata {i})'
    return None


def _nominatim(city, region, iso):
    with _geo_lock:  # no maximo 1 requisicao por segundo (politica do Nominatim)
        wait = 1.1 - (time.time() - _last_geo_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            params = {'city': city, 'state': region, 'countrycodes': iso.lower(), 'format': 'jsonv2', 'limit': 1}
            data = _get_json(NOMINATIM_URL + '?' + urllib.parse.urlencode(params))
        finally:
            _last_geo_call[0] = time.time()
    if data:
        return float(data[0]['lat']), float(data[0]['lon']), data[0].get('display_name', '')
    return None


def geocode(city: str, region: str, iso: str):
    key = f'{iso}|{norm(region)}|{norm(city)}'
    cache = _load_geo_cache()
    if key in cache:
        return tuple(cache[key])
    errors = []
    # Ordem: Photon (considera a provincia) -> Wikidata -> Nominatim. Se um servico limitar (HTTP 429), tenta o proximo.
    for name, fn in (('Photon', lambda: _photon(city, region, iso)),
                     ('Wikidata', lambda: _wikidata(city, iso)),
                     ('Nominatim', lambda: _nominatim(city, region, iso))):
        try:
            hit = fn()
        except Exception as exc:
            errors.append(f'{name}: {type(exc).__name__}: {exc}')
            continue
        if hit:
            cache[key] = list(hit)
            try:
                GEO_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
            except Exception:
                pass
            return hit
        errors.append(f'{name}: sem resultado')
    raise LookupError(f'Não foi possível localizar {city} / {region} ({iso}). Detalhes: ' + ' | '.join(errors))


# ---------------------------------------------------------------- utilidades
def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bbox_around(lat, lon, km):
    dlat = km / 111.32
    dlon = km / (111.32 * max(0.2, math.cos(math.radians(lat))))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def point_of(rec):
    """Coordenadas do place: usa a geometria WKB (ponto) ou o centro do bbox."""
    g = rec.get('geometry')
    if isinstance(g, (bytes, bytearray)) and len(g) >= 21:
        try:
            order = '<' if g[0] == 1 else '>'
            gtype = struct.unpack(order + 'I', g[1:5])[0]
            if gtype % 1000 == 1:
                x, y = struct.unpack(order + 'dd', g[5:21])
                return y, x
        except Exception:
            pass
    b = rec.get('bbox') or {}
    try:
        return (b['ymin'] + b['ymax']) / 2, (b['xmin'] + b['xmax']) / 2
    except Exception:
        return None


def first(lst):
    if isinstance(lst, (list, tuple)) and lst:
        return lst[0]
    return None


def text_of_categories(rec):
    parts = []
    cat = rec.get('categories') or {}
    if isinstance(cat, dict):
        parts.append(cat.get('primary') or '')
        parts.extend(cat.get('alternate') or [])
    for k in ('basic_category',):
        if rec.get(k):
            parts.append(str(rec[k]))
    tax = rec.get('taxonomy')
    if isinstance(tax, dict):
        parts.append(str(tax.get('primary') or ''))
        parts.extend(str(x) for x in (tax.get('hierarchy') or []))
    return ' '.join(norm(p) for p in parts if p)


def name_of(rec):
    n = rec.get('names') or {}
    return (n.get('primary') if isinstance(n, dict) else None) or ''


# ---------------------------------------------------------------- prospeccao
def prospect(iso, region, city, segment, segment_es='', km=60, limit=50, min_score=0, reader_factory=None):
    iso = (iso or '').upper()
    if iso not in SUPPORTED:
        raise ValueError(f'País não suportado: {iso}. Disponíveis: {", ".join(sorted(SUPPORTED))}.')
    lat, lon, display = geocode(city, region, iso)
    bbox = bbox_around(lat, lon, km)

    if reader_factory is None:
        from overturemaps import core

        def reader_factory(b):
            try:
                return core.record_batch_reader('place', bbox=b, stac=True)
            except Exception:
                return core.record_batch_reader('place', bbox=b)

    reader = reader_factory(bbox)
    cat_terms, name_terms = segment_terms(segment, segment_es)
    scanned = matched = rejected = 0
    truncated = False
    rows = []

    for batch in reader:
        # So as colunas usadas: reduz bastante a memoria no plano gratuito (512 MB).
        cols = [c for c in WANTED_COLUMNS if c in batch.schema.names]
        if cols:
            batch = batch.select(cols)
        for rec in batch.to_pylist():
            if scanned >= MAX_SCANNED:
                truncated = True
                break
            scanned += 1
            pt = point_of(rec)
            if not pt:
                rejected += 1
                continue
            dist = haversine_km(lat, lon, pt[0], pt[1])
            if dist > km:
                rejected += 1
                continue
            addr = first(rec.get('addresses')) or {}
            country = (addr.get('country') or '').upper() if isinstance(addr, dict) else ''
            if country and country != iso:
                rejected += 1
                continue
            empresa = name_of(rec)
            cats = text_of_categories(rec)
            nm = norm(empresa)
            hit_cat = any(t in cats for t in cat_terms)
            hit_name = any(t in nm for t in name_terms)
            if not empresa or not (hit_cat or hit_name):
                rejected += 1
                continue
            matched += 1
            phone = first(rec.get('phones')) or ''
            email = first(rec.get('emails')) or ''
            site = first(rec.get('websites')) or ''
            score = (30 if phone else 0) + (25 if email else 0) + (20 if site else 0) + 25
            if score < min_score:
                continue
            cat = rec.get('categories') or {}
            rows.append({
                'id': rec.get('id') or f'{iso}-{scanned}',
                'empresa': empresa,
                'categoria': (cat.get('primary') if isinstance(cat, dict) else '') or '',
                'segmento': segment,
                'endereco': (addr.get('freeform') if isinstance(addr, dict) else '') or '',
                'cidade': (addr.get('locality') if isinstance(addr, dict) else '') or city,
                'uf': (addr.get('region') if isinstance(addr, dict) else '') or region,
                'cep': (addr.get('postcode') if isinstance(addr, dict) else '') or '',
                'pais': SUPPORTED[iso],
                'telefone': phone,
                'email': email,
                'website': site,
                'latitude': round(pt[0], 6),
                'longitude': round(pt[1], 6),
                'distancia_km': round(dist, 1),
                'score': score,
                'confianca': rec.get('confidence'),
                'origem': 'Overture Maps',
            })
        if truncated:
            break

    rows.sort(key=lambda r: (-r['score'], r['distancia_km']))
    return {
        'ok': True,
        'country_iso': iso,
        'country': SUPPORTED[iso],
        'center': {'lat': lat, 'lon': lon, 'label': display},
        'rows': rows[:limit],
        'scanned': scanned,
        'matched': matched,
        'rejected': rejected,
        'truncated': truncated,
        'source': 'Overture Maps',
    }
