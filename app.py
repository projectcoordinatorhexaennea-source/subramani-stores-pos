"""Subramani Stores – department-store POS, inventory, purchases, expenses & reports.
Flask + SQLite. Prices are GST-inclusive (MRP). Run: python app.py"""
import csv, io, os, json, secrets, sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from flask import (Flask, g, request, session, redirect, render_template_string,
                   flash, abort, url_for, get_flashed_messages, Response)
from werkzeug.security import generate_password_hash, check_password_hash

DB   = os.environ.get("ERP_DB", "stores.db")
STORE = os.environ.get("STORE_NAME", "Subramani Stores")
ADDR  = os.environ.get("STORE_ADDRESS", "")
GSTIN = os.environ.get("STORE_GSTIN", "")
PHONE = os.environ.get("STORE_PHONE", "")

app = Flask(__name__)

def _secret():
    if os.environ.get("SECRET_KEY"): return os.environ["SECRET_KEY"]
    if not os.path.exists(".secret"):
        with open(".secret","w") as f: f.write(secrets.token_hex(32))
        try: os.chmod(".secret",0o600)
        except OSError: pass
    return open(".secret").read()

app.secret_key = _secret()
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)

# ── Schema ───────────────────────────────────────────────────────────
SCHEMA = """
create table if not exists users(id integer primary key, username text unique, pw text);
create table if not exists products(id integer primary key, barcode text unique, name text, dept text,
  gst real default 18, mrp real default 0, cost real default 0, stock real default 0, reorder real default 5, active integer default 1);
create table if not exists bills(id integer primary key, number text unique, ts text, phone text,
  gross real, discount real, total real, tax real, mode text);
create table if not exists bill_items(id integer primary key, bill_id integer, product_id integer,
  name text, qty real, mrp real, gst real, amount real);
create table if not exists purchases(id integer primary key, inv_date text, recv_date text,
  supplier text, supplier_gstin text, inv_number text, subtotal real, cgst real, sgst real, igst real,
  total real, notes text);
create table if not exists purchase_items(id integer primary key, purchase_id integer, product_id integer,
  hsn text, qty real, free_qty real default 0, rate real, discount real default 0, gst_pct real, taxable real,
  cgst real, sgst real, igst real, total real);
create table if not exists stock_adj(id integer primary key, product_id integer, date text,
  qty_change real, reason text, note text, new_stock real);
create table if not exists expenses(id integer primary key, date text, category text,
  description text, amount real);
"""

T = {
 "en": dict(pos="Billing",products="Products",purchase="Purchases",report="Reports",
            logout="Log out",inventory="Inventory",expenses="Expenses"),
 "ta": dict(pos="பில்லிங்",products="பொருட்கள்",purchase="கொள்முதல்",report="அறிக்கைகள்",
            logout="வெளியேறு",inventory="சரக்கு",expenses="செலவுகள்")
}

# ── DB helpers ───────────────────────────────────────────────────────
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
        g.db.execute("pragma journal_mode=wal")
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d: d.close()

def init():
    c = sqlite3.connect(DB); c.executescript(SCHEMA)
    # add 'active' column if upgrading from older schema
    cols = [r[1] for r in c.execute("pragma table_info(products)")]
    if "active" not in cols:
        c.execute("alter table products add column active integer default 1")
    if not c.execute("select 1 from users").fetchone():
        pw = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(9)
        c.execute("insert into users(username,pw) values('admin',?)",(generate_password_hash(pw),))
        c.commit(); print(f"\n*** First run. Login: admin / {pw} ***\n")
    c.close()

@app.template_filter("inr")
def inr(x): return "₹{:,.2f}".format(x or 0)

@app.context_processor
def ctx():
    lang = session.get("lang","en")
    session.setdefault("_t", secrets.token_hex(16))
    return dict(t=lambda k: T[lang].get(k,T["en"][k]), lang=lang, store=STORE, token=session["_t"])

@app.before_request
def csrf():
    if request.method=="POST" and request.form.get("_t")!=session.get("_t"): abort(400)

def login_required(f):
    @wraps(f)
    def w(*a,**k):
        if not session.get("uid"): return redirect(url_for("login"))
        return f(*a,**k)
    return w

def num(v, default=0.0):
    try: return float(v)
    except (TypeError,ValueError): return default

# ── Icons (Lucide-style SVG) ─────────────────────────────────────────
_I = lambda d: f'<svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">{d}</svg>'
ICONS = dict(
    pos     = _I('<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8m-4-4v4"/>'),
    products= _I('<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><path d="M3.27 6.96 12 12.01l8.73-5.05M12 22.08V12"/>'),
    purchase= _I('<path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/><path d="M3 6h18"/><path d="M16 10a4 4 0 0 1-8 0"/>'),
    report  = _I('<path d="M18 20V10M12 20V4M6 20v-6"/>'),
    inventory=_I('<path d="M9 5H2v7l6.29 6.29a1 1 0 0 0 1.42 0l3.58-3.58a1 1 0 0 0 0-1.42L9 9Z"/><path d="M6 9.01V9"/><path d="m15 5 6.3 6.3a1 1 0 0 1 0 1.4L17 17"/>'),
    expenses= _I('<line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>'),
    logout  = _I('<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>'),
    search  = '<svg width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>',
    camera  = _I('<path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/>'),
    edit    = _I('<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>'),
    delete  = _I('<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>'),
    download= _I('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>'),
    plus    = _I('<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>'),
)

# ── BASE template ────────────────────────────────────────────────────
BASE = r"""<!doctype html><html lang="{{ lang }}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ store }}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --font:'Inter',system-ui,-apple-system,'Noto Sans Tamil',sans-serif;
  --bg:#f1f5f9;--fg:#0f172a;--muted:#64748b;--border:#e2e8f0;
  --card:#fff;--card-hover:#f8fafc;
  --pri:#6366f1;--pri-hover:#4f46e5;--pri-light:#eef2ff;--pri-fg:#fff;
  --ok:#10b981;--ok-light:#ecfdf5;--warn:#f59e0b;--warn-light:#fffbeb;
  --err:#ef4444;--err-light:#fef2f2;
  --sh-xs:0 1px 2px rgba(0,0,0,.05);--sh:0 1px 3px rgba(0,0,0,.1),0 1px 2px -1px rgba(0,0,0,.1);
  --sh-md:0 4px 6px -1px rgba(0,0,0,.1),0 2px 4px -2px rgba(0,0,0,.1);
  --sh-lg:0 10px 15px -3px rgba(0,0,0,.1),0 4px 6px -4px rgba(0,0,0,.1);
  --sh-xl:0 20px 25px -5px rgba(0,0,0,.1),0 8px 10px -6px rgba(0,0,0,.1);
  --r:8px;--r-lg:12px;--r-xl:16px;--tr:150ms cubic-bezier(.4,0,.2,1);
}
@media(prefers-color-scheme:dark){:root{
  --bg:#0f172a;--fg:#f1f5f9;--muted:#94a3b8;--border:#1e293b;
  --card:#1e293b;--card-hover:#334155;
  --pri:#818cf8;--pri-hover:#a5b4fc;--pri-light:rgba(99,102,241,.15);--pri-fg:#0f172a;
  --ok:#34d399;--ok-light:rgba(16,185,129,.15);--warn:#fbbf24;--warn-light:rgba(245,158,11,.15);
  --err:#f87171;--err-light:rgba(239,68,68,.15);
  --sh-xs:0 1px 2px rgba(0,0,0,.3);--sh:0 1px 3px rgba(0,0,0,.4),0 1px 2px -1px rgba(0,0,0,.3);
  --sh-md:0 4px 6px -1px rgba(0,0,0,.4);--sh-lg:0 10px 15px -3px rgba(0,0,0,.4);--sh-xl:0 20px 25px -5px rgba(0,0,0,.4);
}}
body{font:400 14px/1.6 var(--font);background:var(--bg);color:var(--fg);-webkit-font-smoothing:antialiased}
a{color:var(--pri);text-decoration:none;transition:color var(--tr)}a:hover{color:var(--pri-hover)}
::selection{background:var(--pri);color:var(--pri-fg)}
:focus-visible{outline:2px solid var(--pri);outline-offset:2px;border-radius:4px}

/* ── Nav ─── */
nav{display:flex;align-items:center;gap:4px;padding:0 20px;height:56px;background:var(--card);
  border-bottom:1px solid var(--border);box-shadow:var(--sh-xs);position:sticky;top:0;z-index:100}
.nav-brand{display:flex;align-items:center;gap:8px;font-weight:700;font-size:15px;color:var(--fg);margin-right:12px;white-space:nowrap}
.nav-brand img{width:30px;height:30px;border-radius:50%;object-fit:cover;border:2px solid var(--border)}
.nav-links{display:flex;align-items:center;gap:2px;flex:1;overflow-x:auto;-webkit-overflow-scrolling:touch}
.nl{display:inline-flex;align-items:center;gap:5px;padding:7px 10px;border-radius:var(--r);
  color:var(--muted);font-weight:500;font-size:12.5px;transition:all var(--tr);white-space:nowrap}
.nl:hover{color:var(--fg);background:var(--pri-light)}.nl.ac{color:var(--pri);background:var(--pri-light)}
.nav-r{display:flex;align-items:center;gap:6px;margin-left:auto;white-space:nowrap}
.lbtn{display:inline-flex;padding:3px 8px;border-radius:20px;font-size:11px;font-weight:600;
  color:var(--muted);border:1px solid var(--border);transition:all var(--tr)}
.lbtn:hover,.lbtn.al{color:var(--pri);border-color:var(--pri);background:var(--pri-light)}
.lo-btn{display:inline-flex;align-items:center;gap:4px;padding:5px 10px;border-radius:var(--r);
  font-size:12px;font-weight:500;color:var(--err);transition:all var(--tr)}
.lo-btn:hover{background:var(--err-light)}

/* ── Layout ─── */
main{max-width:1100px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:21px;font-weight:700;margin:0 0 14px;letter-spacing:-.02em}
h2{font-size:16px;font-weight:600;margin:20px 0 10px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px;margin:-10px 0 14px}
.page-head{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:16px}
.page-head h1{margin:0}

/* ── Card / Metric ─── */
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--r-lg);padding:20px;box-shadow:var(--sh-xs)}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:20px}
.metric{background:var(--card);border:1px solid var(--border);border-radius:var(--r-lg);padding:18px 20px;
  box-shadow:var(--sh-xs);position:relative;overflow:hidden;transition:all var(--tr)}
.metric:hover{box-shadow:var(--sh-md);transform:translateY(-1px)}
.metric::before{content:'';position:absolute;top:0;left:0;right:0;height:3px}
.metric:nth-child(1)::before{background:var(--pri)}.metric:nth-child(2)::before{background:var(--ok)}
.metric:nth-child(3)::before{background:var(--warn)}.metric:nth-child(4)::before{background:var(--err)}
.ml{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:4px}
.mv{font-size:24px;font-weight:800;letter-spacing:-.03em;font-variant-numeric:tabular-nums}

/* ── Tables ─── */
.tw{overflow-x:auto;border:1px solid var(--border);border-radius:var(--r-lg);background:var(--card);box-shadow:var(--sh-xs)}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:9px 12px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;
  color:var(--muted);border-bottom:2px solid var(--border)}
td{padding:9px 12px;border-bottom:1px solid var(--border);font-size:13px;vertical-align:middle}
tr:last-child td{border-bottom:none}tbody tr{transition:background var(--tr)}tbody tr:hover{background:var(--card-hover)}
.n{text-align:right;font-variant-numeric:tabular-nums;font-weight:500}

/* ── Forms ─── */
input,select,textarea{font:inherit;padding:8px 11px;border:1px solid var(--border);border-radius:var(--r);
  background:var(--card);color:var(--fg);transition:all var(--tr);width:100%}
input:hover,select:hover{border-color:var(--muted)}
input:focus,select:focus{border-color:var(--pri);box-shadow:0 0 0 3px var(--pri-light);outline:none}
input::placeholder{color:var(--muted);opacity:.7}
.fg{display:flex;flex-direction:column;gap:3px}
.fg label{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.fr{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}
.fr>*{flex:1;min-width:110px}

/* ── Buttons ─── */
button,.btn{display:inline-flex;align-items:center;justify-content:center;gap:5px;padding:8px 16px;border:none;
  border-radius:var(--r);font:500 13px var(--font);cursor:pointer;transition:all var(--tr);text-decoration:none;white-space:nowrap;line-height:1.4}
.bp,button[type=submit],button:not([class]){background:var(--pri);color:var(--pri-fg);box-shadow:var(--sh-xs)}
.bp:hover,button[type=submit]:hover,button:not([class]):hover{background:var(--pri-hover);box-shadow:var(--sh)}
.bp:active,button[type=submit]:active,button:not([class]):active{transform:scale(.98)}
.bs{background:var(--card);color:var(--fg);border:1px solid var(--border)}
.bs:hover{background:var(--card-hover);border-color:var(--muted)}
.bd{background:var(--err);color:#fff}.bd:hover{background:#dc2626}
.bg{background:transparent;color:var(--muted);padding:5px 8px}.bg:hover{background:var(--card-hover);color:var(--fg)}
.bok{background:var(--ok);color:#fff}.bok:hover{background:#059669}
.blg{padding:11px 22px;font-size:15px;font-weight:600;border-radius:var(--r-lg)}
.bsm{padding:4px 8px;font-size:11px;border-radius:6px}

/* ── Flash ─── */
.flash{display:flex;align-items:center;gap:10px;padding:11px 14px;border-radius:var(--r-lg);margin-bottom:14px;
  font-size:13px;font-weight:500;border:1px solid var(--border);background:var(--card);box-shadow:var(--sh-xs);animation:slideD .3s ease-out}
.flash::before{content:'ℹ';display:flex;align-items:center;justify-content:center;width:22px;height:22px;
  border-radius:50%;background:var(--pri-light);color:var(--pri);font-size:12px;font-weight:700;flex-shrink:0}
@keyframes slideD{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:translateY(0)}}

/* ── Badges ─── */
.badge{display:inline-flex;align-items:center;padding:2px 7px;border-radius:20px;font-size:11px;font-weight:600}
.b-ok{background:var(--ok-light);color:var(--ok)}.b-warn{background:var(--warn-light);color:var(--warn)}
.b-err{background:var(--err-light);color:var(--err)}.b-pri{background:var(--pri-light);color:var(--pri)}

/* ── POS ─── */
.pos-search{position:relative}
.pos-search svg{position:absolute;left:14px;top:50%;transform:translateY(-50%);color:var(--muted);pointer-events:none}
.pos-search input{padding:14px 14px 14px 44px;font-size:16px;border-radius:var(--r-lg);border:2px solid var(--border);box-shadow:var(--sh)}
.pos-search input:focus{border-color:var(--pri);box-shadow:0 0 0 4px var(--pri-light)}
.search-dd{position:absolute;left:0;right:0;top:100%;margin-top:4px;background:var(--card);border:1px solid var(--border);
  border-radius:var(--r-lg);box-shadow:var(--sh-lg);z-index:50;max-height:320px;overflow-y:auto;display:none}
.search-dd.open{display:block}
.dd-item{display:flex;align-items:center;gap:10px;padding:10px 14px;cursor:pointer;transition:background var(--tr);border-bottom:1px solid var(--border)}
.dd-item:last-child{border-bottom:none}
.dd-item:hover,.dd-item.sel{background:var(--pri-light)}
.dd-name{font-weight:600;font-size:13px;flex:1}.dd-bc{color:var(--muted);font-size:11px;font-family:ui-monospace,monospace}
.dd-price{font-weight:700;font-size:13px;color:var(--pri)}.dd-stock{font-size:11px;color:var(--muted)}
.pos-footer{display:flex;flex-wrap:wrap;align-items:center;gap:14px;padding:18px 20px;background:var(--card);
  border:1px solid var(--border);border-radius:var(--r-lg);box-shadow:var(--sh);margin-top:14px}
.pos-total{font-size:26px;font-weight:800;letter-spacing:-.03em;color:var(--pri)}
.pos-total small{font-size:13px;font-weight:500;color:var(--muted);display:block;letter-spacing:0}
.scan-btn{display:inline-flex;align-items:center;gap:7px;padding:11px 18px;border-radius:var(--r-lg);
  font-size:14px;font-weight:600;background:var(--card);color:var(--fg);border:2px solid var(--border);
  cursor:pointer;transition:all var(--tr)}
.scan-btn:hover{border-color:var(--pri);color:var(--pri);background:var(--pri-light);box-shadow:var(--sh-md)}

/* ── Receipt ─── */
.rc-wrap{display:flex;justify-content:center;padding:20px 0}
.rc{max-width:320px;width:100%;background:var(--card);border:1px solid var(--border);border-radius:var(--r-lg);
  padding:28px 24px;box-shadow:var(--sh-lg);font:13px/1.5 'Courier New',ui-monospace,monospace}
.rc-hd{text-align:center;margin-bottom:14px}.rc-sn{font-size:18px;font-weight:700}
.rc-hr{border:none;border-top:1px dashed var(--border);margin:10px 0}
.rc table{font-size:12px}.rc td,.rc th{border:0;padding:2px 0}
.rc-tot{font-size:16px;font-weight:700}.rc-ft{text-align:center;color:var(--muted);margin-top:14px;font-size:12px}
.rc-acts{display:flex;gap:10px;justify-content:center;margin-top:20px}

/* ── Login ─── */
.login-w{display:flex;align-items:center;justify-content:center;min-height:calc(100vh - 48px)}
.login-c{width:100%;max-width:380px;padding:36px 28px;background:var(--card);border:1px solid var(--border);
  border-radius:var(--r-xl);box-shadow:var(--sh-xl)}
.login-c .logo{width:72px;height:72px;border-radius:50%;object-fit:cover;display:block;margin:0 auto 12px;
  border:3px solid var(--border);box-shadow:var(--sh-md)}
.login-c .sn{font-size:22px;font-weight:800;text-align:center;margin-bottom:2px;letter-spacing:-.03em}
.login-c .ss{text-align:center;color:var(--muted);font-size:13px;margin-bottom:24px}
.login-c .fg{margin-bottom:14px}.login-c .fg input{padding:11px 12px}
.login-c button{width:100%;padding:11px;font-size:14px;font-weight:600;margin-top:6px}

/* ── Modals ─── */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.55);backdrop-filter:blur(4px);display:none;
  align-items:center;justify-content:center;z-index:1000;animation:fadeIn .2s}
.modal-bg.open{display:flex}
.modal{background:var(--card);border-radius:var(--r-xl);padding:24px;width:92%;max-width:440px;
  box-shadow:var(--sh-xl);animation:scaleIn .2s}
.modal h2{margin:0 0 14px;font-size:17px}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
@keyframes scaleIn{from{opacity:0;transform:scale(.95)}to{opacity:1;transform:scale(1)}}

/* ── Filters ─── */
.filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px;align-items:center}
.filters input,.filters select{width:auto;min-width:160px}

/* ── Misc ─── */
.stock-low{color:var(--err);font-weight:700}
.flex{display:flex;align-items:center}.gap-2{gap:8px}.gap-3{gap:12px}.ml-a{margin-left:auto}
.text-m{color:var(--muted)}.text-sm{font-size:13px}
.mt-2{margin-top:16px}.mt-3{margin-top:20px}.mb-2{margin-bottom:14px}.mb-3{margin-bottom:20px}
.fw6{font-weight:600}.fw7{font-weight:700}
.empty-msg{text-align:center;padding:32px;color:var(--muted)}
.action-cell{display:flex;gap:4px}
code{font-size:12px;color:var(--muted)}

@media print{nav,.noprint{display:none!important}body{background:#fff;color:#000}main{padding:0}.rc{border:none;box-shadow:none;padding:0}}
@media(max-width:640px){
  nav{padding:0 10px;height:48px}.nav-brand{font-size:13px;margin-right:6px}.nl{padding:5px 7px;font-size:11px}
  main{padding:14px 10px 40px}.metrics{grid-template-columns:repeat(2,1fr);gap:8px}.metric{padding:12px 14px}
  .mv{font-size:18px}.pos-search input{padding:11px 11px 11px 38px;font-size:14px}
  .pos-footer{padding:12px 14px}.pos-total{font-size:20px}.fr>*{min-width:90px}
}
</style>
<script src="https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js"></script>
</head><body>
{% if session.uid %}
<nav>
  <div class="nav-brand"><img src="/static/logo.jpg" alt=""> {{ store }}</div>
  <div class="nav-links">
    <a class="nl{% if request.path=='/' %} ac{% endif %}" href="/">""" + ICONS['pos'] + """ {{ t('pos') }}</a>
    <a class="nl{% if '/products' in request.path %} ac{% endif %}" href="/products">""" + ICONS['products'] + """ {{ t('products') }}</a>
    <a class="nl{% if '/inventory' in request.path %} ac{% endif %}" href="/inventory">""" + ICONS['inventory'] + """ {{ t('inventory') }}</a>
    <a class="nl{% if '/purchase' in request.path %} ac{% endif %}" href="/purchase">""" + ICONS['purchase'] + """ {{ t('purchase') }}</a>
    <a class="nl{% if '/expenses' in request.path %} ac{% endif %}" href="/expenses">""" + ICONS['expenses'] + """ {{ t('expenses') }}</a>
    <a class="nl{% if '/report' in request.path %} ac{% endif %}" href="/report">""" + ICONS['report'] + """ {{ t('report') }}</a>
  </div>
  <div class="nav-r">
    <a class="lbtn{% if lang=='en' %} al{% endif %}" href="/lang/en">EN</a>
    <a class="lbtn{% if lang=='ta' %} al{% endif %}" href="/lang/ta">தமிழ்</a>
    <a class="lo-btn" href="/logout">""" + ICONS['logout'] + """</a>
  </div>
</nav>
{% endif %}
<main>{% for m in msgs %}<div class="flash">{{ m }}</div>{% endfor %}{{ inner|safe }}</main>
</body></html>"""

def page(body, **c):
    return render_template_string(BASE, inner=render_template_string(body, **c), msgs=get_flashed_messages())


# ══════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════

# ── Auth ─────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        u = db().execute("select * from users where username=?",(request.form.get("username",""),)).fetchone()
        if u and check_password_hash(u["pw"], request.form.get("password","")):
            session.clear(); session["uid"]=u["id"]; return redirect("/")
        flash("Wrong username or password.")
    return page("""<div class="login-w"><div class="login-c">
<img src="/static/logo.jpg" alt="{{ store }}" class="logo">
<div class="sn">{{ store }}</div><div class="ss">Point of Sale System</div>
<form method="post"><input type="hidden" name="_t" value="{{ token }}">
<div class="fg"><label>Username</label><input name="username" placeholder="Enter username" autofocus required></div>
<div class="fg"><label>Password</label><input name="password" type="password" placeholder="Enter password" required></div>
<button type="submit">Sign in</button></form></div></div>""")

@app.route("/logout")
def logout(): session.clear(); return redirect("/login")

@app.route("/lang/<code>")
def set_lang(code):
    if code in T: session["lang"]=code
    return redirect(request.referrer or "/")


# ── POS (Billing) ───────────────────────────────────────────────────
@app.route("/", methods=["GET","POST"])
@login_required
def pos():
    d = db()
    if request.method=="POST":
        try: cart = json.loads(request.form.get("cart","[]"))
        except ValueError: cart = []
        lines = {}
        for c in cart if isinstance(cart,list) else []:
            if not isinstance(c,dict): continue
            p = d.execute("select * from products where id=? and active=1",(c.get("id"),)).fetchone()
            q = num(c.get("qty"))
            if p and q>0: lines.setdefault(p["id"],[p,0])[1]+=q
        if not lines: flash("The bill is empty."); return redirect("/")
        for p,q in lines.values():
            if q>p["stock"]: flash(f"Not enough stock for {p['name']}: {p['stock']:g} available."); return redirect("/")
        gross = sum(p["mrp"]*q for p,q in lines.values())
        disc = min(max(num(request.form.get("discount")),0),gross)
        f = (gross-disc)/gross if gross else 0
        tax = sum(p["mrp"]*q*f*p["gst"]/(100+p["gst"]) for p,q in lines.values())
        mode = request.form.get("mode") if request.form.get("mode") in ("cash","upi","card") else "cash"
        cur = d.execute("insert into bills(number,ts,phone,gross,discount,total,tax,mode) values(?,?,?,?,?,?,?,?)",
            ("tmp-"+secrets.token_hex(4), datetime.now().isoformat(timespec="seconds"),
             request.form.get("phone","").strip()[:15], round(gross,2), round(disc,2),
             round(gross-disc,2), round(tax,2), mode))
        bid = cur.lastrowid
        d.execute("update bills set number=? where id=?",(f"BILL-{bid:06d}",bid))
        for p,q in lines.values():
            d.execute("insert into bill_items(bill_id,product_id,name,qty,mrp,gst,amount) values(?,?,?,?,?,?,?)",
                      (bid,p["id"],p["name"],q,p["mrp"],p["gst"],round(p["mrp"]*q,2)))
            d.execute("update products set stock=stock-? where id=?",(q,p["id"]))
        d.commit(); return redirect(url_for("receipt",bid=bid))
    prods = [dict(id=p["id"],barcode=p["barcode"] or "",name=p["name"],mrp=p["mrp"],stock=p["stock"],dept=p["dept"])
             for p in d.execute("select * from products where active=1 order by name")]
    return page("""
<h1>{{ t('pos') }}</h1>
<div style="display:flex;gap:10px;align-items:stretch;margin-bottom:18px">
  <div class="pos-search" style="flex:1;position:relative">
    """ + ICONS['search'] + """
    <input id="q" placeholder="Scan barcode or type product name …" autofocus autocomplete="off">
    <div id="sdd" class="search-dd"></div>
  </div>
  <button type="button" class="scan-btn" onclick="openScanner()">""" + ICONS['camera'] + """ Scan</button>
</div>

<!-- Scanner Modal -->
<div id="scanner-modal" class="modal-bg" onclick="if(event.target===this)closeScanner()">
  <div class="modal"><div class="flex gap-2 mb-2"><h2>Scan Barcode</h2><button type="button" class="bg ml-a" onclick="closeScanner()">✕</button></div>
  <div id="scanner-view" style="border-radius:var(--r-lg);overflow:hidden;background:#000;min-height:200px"></div>
  <p class="text-m text-sm" style="text-align:center;margin-top:10px">Point your camera at the product barcode</p></div>
</div>

<div class="tw mb-3"><table>
<thead><tr><th>#</th><th>Item</th><th style="width:90px">Qty</th><th class="n">Price</th><th class="n">Amount</th><th style="width:44px"></th></tr></thead>
<tbody id="cart"><tr id="empty-row"><td colspan="6" class="empty-msg">Scan or search to add items</td></tr></tbody>
</table></div>

<form method="post" id="f"><input type="hidden" name="_t" value="{{ token }}"><input type="hidden" name="cart" id="cj">
<div class="pos-footer">
  <div class="fr" style="flex:1">
    <div class="fg" style="max-width:130px"><label>Discount ₹</label>
      <input name="discount" id="disc" type="number" step="0.01" min="0" placeholder="0.00"></div>
    <div class="fg" style="max-width:120px"><label>Payment</label>
      <select name="mode"><option value="cash">Cash</option><option value="upi">UPI</option><option value="card">Card</option></select></div>
    <div class="fg" style="max-width:160px"><label>Customer phone</label>
      <input name="phone" placeholder="Optional" inputmode="tel"></div>
  </div>
  <div class="pos-total"><small>Items ₹<span id="g">0.00</span></small>₹<span id="net">0.00</span></div>
  <button type="submit" class="blg">Save & Print</button>
</div></form>

<script>
const P={{ prods|tojson }};let cart=[],selIdx=-1;
const q=document.getElementById('q'),tb=document.getElementById('cart'),dc=document.getElementById('disc'),sdd=document.getElementById('sdd');
function pr(id){return P.find(x=>x.id==id)}
function add(p){const c=cart.find(x=>x.id==p.id);if(c)c.qty++;else cart.push({id:p.id,qty:1});draw();hideDd()}

/* ── Search dropdown ── */
function showDd(items){
  if(!items.length){hideDd();return}
  sdd.innerHTML=items.map((p,i)=>
    '<div class="dd-item'+(i===selIdx?' sel':'')+'" data-i="'+i+'">'+
    '<div><div class="dd-name">'+p.name+'</div><div class="dd-bc">'+p.barcode+' · '+p.dept+'</div></div>'+
    '<div style="text-align:right"><div class="dd-price">₹'+p.mrp.toFixed(2)+'</div><div class="dd-stock">Stock: '+p.stock+'</div></div></div>'
  ).join('');
  sdd.classList.add('open');
  sdd.querySelectorAll('.dd-item').forEach(el=>el.onclick=()=>{add(items[+el.dataset.i]);q.value='';q.focus()});
}
function hideDd(){sdd.classList.remove('open');sdd.innerHTML='';selIdx=-1}
let lastMatches=[];
q.addEventListener('input',()=>{
  const v=q.value.trim().toLowerCase();if(!v){hideDd();return}
  lastMatches=P.filter(p=>(p.barcode&&p.barcode.toLowerCase().includes(v))||p.name.toLowerCase().includes(v)).slice(0,8);
  selIdx=-1;showDd(lastMatches);
});
q.addEventListener('keydown',e=>{
  if(e.key==='ArrowDown'&&lastMatches.length){e.preventDefault();selIdx=Math.min(selIdx+1,lastMatches.length-1);showDd(lastMatches);return}
  if(e.key==='ArrowUp'&&lastMatches.length){e.preventDefault();selIdx=Math.max(selIdx-1,0);showDd(lastMatches);return}
  if(e.key==='Escape'){hideDd();return}
  if(e.key!=='Enter')return;e.preventDefault();
  if(selIdx>=0&&lastMatches[selIdx]){add(lastMatches[selIdx]);q.value='';return}
  const v=q.value.trim().toLowerCase();if(!v)return;
  let p=P.find(x=>x.barcode&&x.barcode.toLowerCase()===v)||P.find(x=>x.name.toLowerCase()===v);
  if(!p){const m=P.filter(x=>x.name.toLowerCase().includes(v));if(m.length===1)p=m[0]}
  if(p){add(p);q.value=''}else alert('No product matches. Try the dropdown suggestions.');
});
document.addEventListener('click',e=>{if(!e.target.closest('.pos-search'))hideDd()});

/* ── Cart ── */
function draw(){
  tb.replaceChildren();
  if(!cart.length){const er=document.createElement('tr');er.id='empty-row';er.innerHTML='<td colspan="6" class="empty-msg">Scan or search to add items</td>';tb.append(er)}
  let g=0;cart.forEach((c,i)=>{const p=pr(c.id);g+=p.mrp*c.qty;const tr=tb.insertRow();
    tr.insertCell().textContent=i+1;tr.insertCell().textContent=p.name;
    const qi=document.createElement('input');qi.type='number';qi.step='any';qi.min='0';qi.value=c.qty;qi.style.width='70px';
    qi.onchange=()=>{c.qty=parseFloat(qi.value)||0;if(c.qty<=0)cart.splice(i,1);draw()};tr.insertCell().append(qi);
    const pc=tr.insertCell();pc.className='n text-m';pc.textContent='₹'+p.mrp.toFixed(2);
    const a=tr.insertCell();a.className='n fw6';a.textContent='₹'+(p.mrp*c.qty).toFixed(2);
    const x=document.createElement('button');x.type='button';x.className='bg bsm';x.innerHTML='✕';
    x.onclick=()=>{cart.splice(i,1);draw()};tr.insertCell().append(x)});
  document.getElementById('g').textContent=g.toFixed(2);
  const d=Math.min(Math.max(parseFloat(dc.value)||0,0),g);
  document.getElementById('net').textContent=(g-d).toFixed(2);q.focus()}
dc.oninput=draw;
document.getElementById('f').onsubmit=e=>{const c=cart.filter(x=>x.qty>0);
  if(!c.length){e.preventDefault();alert('Add items first.');return}
  document.getElementById('cj').value=JSON.stringify(c)};

/* ── Camera scanner ── */
let scanner=null;
function openScanner(){
  document.getElementById('scanner-modal').classList.add('open');
  scanner=new Html5Qrcode('scanner-view',{formatsToSupport:[
    Html5QrcodeSupportedFormats.EAN_13,Html5QrcodeSupportedFormats.EAN_8,
    Html5QrcodeSupportedFormats.UPC_A,Html5QrcodeSupportedFormats.UPC_E,
    Html5QrcodeSupportedFormats.CODE_128,Html5QrcodeSupportedFormats.CODE_39,
    Html5QrcodeSupportedFormats.CODE_93,Html5QrcodeSupportedFormats.ITF,
    Html5QrcodeSupportedFormats.QR_CODE]});
  scanner.start({facingMode:'environment'},{fps:15,qrbox:{width:260,height:150}},
    code=>{closeScanner();handleBarcode(code)},()=>{}
  ).catch(err=>{alert('Camera error: '+err);closeScanner()});}
function closeScanner(){if(scanner){try{scanner.stop().catch(()=>{})}catch(e){}scanner=null}
  document.getElementById('scanner-modal').classList.remove('open')}
function handleBarcode(code){
  const p=P.find(x=>x.barcode&&x.barcode===code)||P.find(x=>x.barcode&&x.barcode.toLowerCase()===code.toLowerCase());
  if(p){add(p);if(navigator.vibrate)navigator.vibrate(100)}
  else{alert('No product for barcode: '+code);q.value=code;q.focus()}}
</script>""", prods=prods)


# ── Receipt ──────────────────────────────────────────────────────────
@app.route("/bill/<int:bid>")
@login_required
def receipt(bid):
    d = db()
    b = d.execute("select * from bills where id=?",(bid,)).fetchone() or abort(404)
    items = d.execute("select * from bill_items where bill_id=?",(bid,)).fetchall()
    return page("""<div class="rc-wrap"><div class="rc"><div class="rc-hd">
<img src="/static/logo.jpg" alt="" style="width:44px;height:44px;border-radius:50%;object-fit:cover;margin:0 auto 6px;display:block;border:2px solid var(--border)">
<div class="rc-sn">{{ store }}</div>{% if addr %}<div>{{ addr }}</div>{% endif %}
{% if phone %}<div>Ph: {{ phone }}</div>{% endif %}{% if gstin %}<div>GSTIN: {{ gstin }}</div>{% endif %}</div>
<hr class="rc-hr"><div class="flex gap-2" style="justify-content:space-between;font-size:12px;color:var(--muted)">
<span>{{ b.number }}</span><span>{{ b.ts.replace('T',' ') }}</span></div><hr class="rc-hr">
<table>{% for i in items %}<tr><td colspan="2" style="font-weight:600;padding-top:4px">{{ i.name }}</td></tr>
<tr><td style="color:var(--muted)">{{ '%g'|format(i.qty) }} × ₹{{ '%.2f'|format(i.mrp) }}</td><td class="n">₹{{ '%.2f'|format(i.amount) }}</td></tr>{% endfor %}</table><hr class="rc-hr">
<table><tr><td>Items total</td><td class="n">₹{{ '%.2f'|format(b.gross) }}</td></tr>
{% if b.discount %}<tr><td>Discount</td><td class="n" style="color:var(--ok)">−₹{{ '%.2f'|format(b.discount) }}</td></tr>{% endif %}
<tr><td class="rc-tot">TOTAL</td><td class="n rc-tot">₹{{ '%.2f'|format(b.total) }}</td></tr>
<tr><td style="color:var(--muted);font-size:11px">incl. CGST</td><td class="n" style="color:var(--muted);font-size:11px">₹{{ '%.2f'|format(b.tax/2) }}</td></tr>
<tr><td style="color:var(--muted);font-size:11px">incl. SGST</td><td class="n" style="color:var(--muted);font-size:11px">₹{{ '%.2f'|format(b.tax/2) }}</td></tr>
<tr><td>Paid by</td><td class="n"><span class="badge b-pri">{{ b.mode|upper }}</span></td></tr></table><hr class="rc-hr">
<div class="rc-ft">Thank you for shopping!<br>Visit again</div></div></div>
<div class="rc-acts noprint"><button onclick="print()" class="bp blg">🖨️ Print</button><a class="btn bs blg" href="/">New bill</a></div>""",
    b=b,items=items,addr=ADDR,phone=PHONE,gstin=GSTIN)


# ── Products ─────────────────────────────────────────────────────────
@app.route("/products", methods=["GET","POST"])
@login_required
def products():
    d = db()
    if request.method=="POST":
        act = request.form.get("action","save")
        if act=="delete":
            pid = request.form.get("pid")
            d.execute("update products set active=0 where id=?",(pid,))
            d.commit(); flash("Product removed."); return redirect("/products")
        f = request.form
        bc,name = f.get("barcode","").strip(), f.get("name","").strip()
        if not bc or not name: flash("Barcode and name are required."); return redirect("/products")
        vals = (name, f.get("dept","").strip() or "General", num(f.get("gst"),18),
                num(f.get("mrp")), num(f.get("cost")), num(f.get("reorder"),5))
        if d.execute("select 1 from products where barcode=?",(bc,)).fetchone():
            d.execute("update products set name=?,dept=?,gst=?,mrp=?,cost=?,reorder=?,active=1 where barcode=?",vals+(bc,))
            if f.get("stock","").strip()!="":
                d.execute("update products set stock=? where barcode=?",(num(f["stock"]),bc))
            flash("Product updated.")
        else:
            d.execute("insert into products(name,dept,gst,mrp,cost,reorder,barcode,stock,active) values(?,?,?,?,?,?,?,?,1)",
                      vals+(bc,num(f.get("stock"))))
            flash("Product added.")
        d.commit(); return redirect("/products")
    rows = d.execute("select * from products where active=1 order by dept,name").fetchall()
    return page("""
<div class="page-head"><h1>""" + ICONS['products'] + """ {{ t('products') }}</h1></div>
<p class="sub">Enter an existing barcode to update. Leave stock blank to keep current value.</p>
<div class="card mb-3"><form method="post"><input type="hidden" name="_t" value="{{ token }}">
<div class="fr">
  <div class="fg" style="max-width:150px"><label>Barcode</label><input name="barcode" id="p-bc" placeholder="Scan / type" required></div>
  <div class="fg" style="flex:2"><label>Product name</label><input name="name" id="p-name" placeholder="Product name" required></div>
  <div class="fg" style="max-width:130px"><label>Department</label><input name="dept" id="p-dept" placeholder="General" list="dl">
    <datalist id="dl">{% for x in depts %}<option value="{{ x }}">{% endfor %}</datalist></div>
  <div class="fg" style="max-width:120px"><label>MRP (incl GST)</label><input name="mrp" id="p-mrp" type="number" step="0.01" placeholder="0.00" required></div>
  <div class="fg" style="max-width:100px"><label>Cost</label><input name="cost" id="p-cost" type="number" step="0.01" placeholder="0.00"></div>
  <div class="fg" style="max-width:80px"><label>GST %</label><input name="gst" id="p-gst" type="number" step="any" value="18"></div>
  <div class="fg" style="max-width:90px"><label>Stock</label><input name="stock" id="p-stock" type="number" step="any" placeholder="—"></div>
  <div class="fg" style="max-width:85px"><label>Reorder</label><input name="reorder" id="p-reorder" type="number" step="any" value="5"></div>
  <div style="display:flex;align-items:flex-end"><button type="submit">Save</button></div>
</div></form></div>

<div class="filters"><input id="pf" placeholder="🔍 Search products…" style="max-width:260px" oninput="filterP()">
<select id="pdf" onchange="filterP()" style="max-width:160px"><option value="">All departments</option>
{% for x in depts %}<option value="{{ x }}">{{ x }}</option>{% endfor %}</select>
<span class="text-m text-sm ml-a">{{ rows|length }} products</span></div>

<div class="tw"><table><thead><tr><th>Barcode</th><th>Name</th><th>Dept</th><th class="n">MRP</th><th class="n">Cost</th>
<th class="n">GST%</th><th class="n">Stock</th><th>Actions</th></tr></thead><tbody id="ptb">
{% for p in rows %}<tr data-name="{{ p.name|lower }}" data-dept="{{ p.dept }}">
<td><code>{{ p.barcode }}</code></td><td class="fw6">{{ p.name }}</td><td><span class="badge b-pri">{{ p.dept }}</span></td>
<td class="n">{{ p.mrp|inr }}</td><td class="n">{{ p.cost|inr }}</td><td class="n">{{ '%g'|format(p.gst) }}%</td>
<td class="n">{% if p.stock<=p.reorder %}<span class="stock-low">{{ '%g'|format(p.stock) }}</span>{% else %}{{ '%g'|format(p.stock) }}{% endif %}</td>
<td class="action-cell">
  <button type="button" class="bg bsm" title="Edit" onclick='fillEdit({{ p|tojson }})'>""" + ICONS['edit'] + """</button>
  <form method="post" style="display:inline" onsubmit="return confirm('Delete {{ p.name }}?')">
    <input type="hidden" name="_t" value="{{ token }}"><input type="hidden" name="action" value="delete">
    <input type="hidden" name="pid" value="{{ p.id }}"><button class="bg bsm" title="Delete" style="color:var(--err)">""" + ICONS['delete'] + """</button></form>
</td></tr>{% endfor %}</tbody></table></div>

<script>
function filterP(){const v=document.getElementById('pf').value.toLowerCase(),dept=document.getElementById('pdf').value;
  document.querySelectorAll('#ptb tr').forEach(r=>{
    const n=r.dataset.name||'',d=r.dataset.dept||'';
    r.style.display=(!v||n.includes(v))&&(!dept||d===dept)?'':'none'})}
function fillEdit(p){document.getElementById('p-bc').value=p.barcode;document.getElementById('p-name').value=p.name;
  document.getElementById('p-dept').value=p.dept;document.getElementById('p-mrp').value=p.mrp;
  document.getElementById('p-cost').value=p.cost;document.getElementById('p-gst').value=p.gst;
  document.getElementById('p-stock').value='';document.getElementById('p-reorder').value=p.reorder;
  document.getElementById('p-bc').focus();window.scrollTo({top:0,behavior:'smooth'})}
</script>""", rows=rows, depts=sorted({r["dept"] for r in rows}))


# ── Inventory Sheet ──────────────────────────────────────────────────
@app.route("/inventory", methods=["GET","POST"])
@login_required
def inventory():
    d = db()
    if request.method=="POST":
        pid = request.form.get("product_id")
        change = num(request.form.get("qty_change"))
        reason = request.form.get("reason","").strip() or "Manual adjustment"
        note = request.form.get("note","").strip()
        p = d.execute("select * from products where id=? and active=1",(pid,)).fetchone()
        if not p: flash("Product not found."); return redirect("/inventory")
        if change==0: flash("Enter a non-zero quantity."); return redirect("/inventory")
        new_stock = p["stock"]+change
        if new_stock<0: flash(f"Cannot reduce below zero. Current stock: {p['stock']:g}"); return redirect("/inventory")
        d.execute("update products set stock=? where id=?",(new_stock,pid))
        d.execute("insert into stock_adj(product_id,date,qty_change,reason,note,new_stock) values(?,?,?,?,?,?)",
                  (pid,datetime.now().isoformat(timespec="seconds"),change,reason,note,new_stock))
        d.commit(); flash(f"Stock adjusted: {p['name']} → {new_stock:g}"); return redirect("/inventory")
    rows = d.execute("select * from products where active=1 order by dept,name").fetchall()
    total_val = sum(r["stock"]*r["cost"] for r in rows)
    total_mrp_val = sum(r["stock"]*r["mrp"] for r in rows)
    low_count = sum(1 for r in rows if r["stock"]<=r["reorder"])
    recent_adj = d.execute("""select sa.*,p.name pname from stock_adj sa
        join products p on p.id=sa.product_id order by sa.id desc limit 20""").fetchall()
    return page("""
<div class="page-head"><h1>""" + ICONS['inventory'] + """ {{ t('inventory') }}</h1>
<a class="btn bs ml-a" href="/inventory/csv">""" + ICONS['download'] + """ Export CSV</a></div>

<div class="metrics">
  <div class="metric"><div class="ml">Total Products</div><div class="mv">{{ rows|length }}</div></div>
  <div class="metric"><div class="ml">Stock Value (cost)</div><div class="mv">{{ total_val|inr }}</div></div>
  <div class="metric"><div class="ml">Stock Value (MRP)</div><div class="mv">{{ total_mrp_val|inr }}</div></div>
  <div class="metric"><div class="ml">Low Stock Items</div><div class="mv" style="color:var(--err)">{{ low_count }}</div></div>
</div>

<div class="filters">
  <input id="sf" placeholder="🔍 Search inventory…" style="max-width:260px" oninput="filterI()">
  <select id="sdf" onchange="filterI()" style="max-width:160px"><option value="">All departments</option>
  {% for x in depts %}<option value="{{ x }}">{{ x }}</option>{% endfor %}</select>
  <label class="flex gap-2 text-sm" style="cursor:pointer"><input type="checkbox" id="slow" onchange="filterI()" style="width:auto"> Low stock only</label>
</div>

<div class="tw mb-3"><table><thead><tr><th>Barcode</th><th>Product</th><th>Dept</th><th class="n">Cost</th>
<th class="n">MRP</th><th class="n">Stock</th><th class="n">Reorder</th><th class="n">Value</th><th>Adjust</th></tr></thead>
<tbody id="itb">{% for p in rows %}<tr data-name="{{ p.name|lower }}" data-dept="{{ p.dept }}" data-low="{{ '1' if p.stock<=p.reorder else '0' }}">
<td><code>{{ p.barcode }}</code></td><td class="fw6">{{ p.name }}</td><td><span class="badge b-pri">{{ p.dept }}</span></td>
<td class="n">{{ p.cost|inr }}</td><td class="n">{{ p.mrp|inr }}</td>
<td class="n">{% if p.stock<=p.reorder %}<span class="badge b-err">{{ '%g'|format(p.stock) }}</span>{% else %}<span class="badge b-ok">{{ '%g'|format(p.stock) }}</span>{% endif %}</td>
<td class="n">{{ '%g'|format(p.reorder) }}</td><td class="n">{{ (p.stock*p.cost)|inr }}</td>
<td><button type="button" class="bs bsm" onclick='openAdj({{ p.id }},"{{ p.name }}",{{ p.stock }})'>± Adjust</button></td>
</tr>{% endfor %}</tbody></table></div>

<!-- Stock adjustment modal -->
<div id="adj-modal" class="modal-bg" onclick="if(event.target===this)closeAdj()">
<div class="modal"><h2>Adjust Stock</h2><p id="adj-info" class="text-m text-sm mb-2"></p>
<form method="post"><input type="hidden" name="_t" value="{{ token }}"><input type="hidden" name="product_id" id="adj-pid">
<div class="fr mb-2">
  <div class="fg"><label>Change (+/-)</label><input name="qty_change" id="adj-qty" type="number" step="any" placeholder="+10 or -5" required autofocus></div>
  <div class="fg"><label>Reason</label><select name="reason">
    <option>Count correction</option><option>Damage / waste</option><option>Theft / loss</option>
    <option>Return from customer</option><option>Opening stock</option><option>Other</option></select></div>
</div>
<div class="fg mb-2"><label>Note (optional)</label><input name="note" placeholder="Any extra details"></div>
<div class="flex gap-2"><button type="submit" class="bok">Save Adjustment</button><button type="button" class="bs" onclick="closeAdj()">Cancel</button></div>
</form></div></div>

<h2 class="mt-3">Recent Adjustments</h2>
<div class="tw"><table><thead><tr><th>Date</th><th>Product</th><th class="n">Change</th><th class="n">New Stock</th><th>Reason</th><th>Note</th></tr></thead>
<tbody>{% for a in adj %}<tr><td>{{ a.date[:16].replace('T',' ') }}</td><td class="fw6">{{ a.pname }}</td>
<td class="n {% if a.qty_change>0 %}b-ok{% else %}b-err{% endif %}" style="border-radius:4px">{{ '%+g'|format(a.qty_change) }}</td>
<td class="n">{{ '%g'|format(a.new_stock) }}</td><td>{{ a.reason }}</td><td class="text-m">{{ a.note or '—' }}</td></tr>
{% else %}<tr><td colspan="6" class="empty-msg">No adjustments yet</td></tr>{% endfor %}</tbody></table></div>

<script>
function filterI(){const v=document.getElementById('sf').value.toLowerCase(),dept=document.getElementById('sdf').value,lo=document.getElementById('slow').checked;
  document.querySelectorAll('#itb tr').forEach(r=>{
    const n=r.dataset.name||'',d=r.dataset.dept||'',l=r.dataset.low;
    r.style.display=(!v||n.includes(v))&&(!dept||d===dept)&&(!lo||l==='1')?'':'none'})}
function openAdj(id,name,stock){document.getElementById('adj-pid').value=id;
  document.getElementById('adj-info').textContent=name+' — current stock: '+stock;
  document.getElementById('adj-qty').value='';document.getElementById('adj-modal').classList.add('open');
  setTimeout(()=>document.getElementById('adj-qty').focus(),100)}
function closeAdj(){document.getElementById('adj-modal').classList.remove('open')}
</script>""", rows=rows, total_val=total_val, total_mrp_val=total_mrp_val, low_count=low_count,
    adj=recent_adj, depts=sorted({r["dept"] for r in rows}))


@app.route("/inventory/csv")
@login_required
def inventory_csv():
    rows = db().execute("select * from products where active=1 order by dept,name").fetchall()
    si = io.StringIO()
    w = csv.writer(si)
    w.writerow(["Barcode","Name","Department","MRP","Cost","GST%","Stock","Reorder Level","Stock Value (Cost)","Stock Value (MRP)"])
    for r in rows:
        w.writerow([r["barcode"],r["name"],r["dept"],r["mrp"],r["cost"],r["gst"],r["stock"],r["reorder"],
                     round(r["stock"]*r["cost"],2), round(r["stock"]*r["mrp"],2)])
    return Response(si.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":f"attachment; filename=inventory_{date.today()}.csv"})


# ── Purchases (Tax Invoice Entry) ────────────────────────────────────
@app.route("/purchase", methods=["GET","POST"])
@login_required
def purchase():
    d = db()
    if request.method=="POST":
        f = request.form
        supplier = f.get("supplier","").strip() or "Unknown"
        supplier_gstin = f.get("supplier_gstin","").strip()
        inv_number = f.get("inv_number","").strip()
        inv_date = f.get("inv_date","") or date.today().isoformat()
        notes = f.get("notes","").strip()
        items = []
        pids = f.getlist("product_id"); qtys = f.getlist("qty"); frees = f.getlist("free_qty")
        rates = f.getlist("rate"); discs = f.getlist("item_disc"); hsns = f.getlist("hsn")
        gsts = f.getlist("gst_pct")
        for i in range(len(pids)):
            p = d.execute("select * from products where id=?",(pids[i],)).fetchone()
            q = num(qtys[i] if i<len(qtys) else 0)
            fq = num(frees[i] if i<len(frees) else 0)
            rate = num(rates[i] if i<len(rates) else 0)
            disc = num(discs[i] if i<len(discs) else 0)
            hsn = hsns[i].strip() if i<len(hsns) else ""
            gst_pct = num(gsts[i] if i<len(gsts) else 18)
            if p and q>0 and rate>=0:
                taxable = round(q * rate - disc, 2)
                is_igst = len(supplier_gstin)>=2 and len(GSTIN)>=2 and supplier_gstin[:2]!=GSTIN[:2]
                cgst_amt = 0 if is_igst else round(taxable * gst_pct / 200, 2)
                sgst_amt = 0 if is_igst else round(taxable * gst_pct / 200, 2)
                igst_amt = round(taxable * gst_pct / 100, 2) if is_igst else 0
                line_total = round(taxable + cgst_amt + sgst_amt + igst_amt, 2)
                items.append(dict(p=p, hsn=hsn, qty=q, free_qty=fq, rate=rate, disc=disc,
                    gst_pct=gst_pct, taxable=taxable, cgst=cgst_amt, sgst=sgst_amt, igst=igst_amt, total=line_total))
        if not items:
            flash("Add at least one valid item."); return redirect("/purchase")
        subtotal = sum(it["taxable"] for it in items)
        t_cgst = sum(it["cgst"] for it in items)
        t_sgst = sum(it["sgst"] for it in items)
        t_igst = sum(it["igst"] for it in items)
        grand = round(subtotal + t_cgst + t_sgst + t_igst, 2)
        cur = d.execute("""insert into purchases(inv_date,recv_date,supplier,supplier_gstin,inv_number,
            subtotal,cgst,sgst,igst,total,notes) values(?,?,?,?,?,?,?,?,?,?,?)""",
            (inv_date, date.today().isoformat(), supplier, supplier_gstin, inv_number,
             round(subtotal,2), round(t_cgst,2), round(t_sgst,2), round(t_igst,2), grand, notes))
        pur_id = cur.lastrowid
        for it in items:
            d.execute("""insert into purchase_items(purchase_id,product_id,hsn,qty,free_qty,rate,
                discount,gst_pct,taxable,cgst,sgst,igst,total) values(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pur_id,it["p"]["id"],it["hsn"],it["qty"],it["free_qty"],it["rate"],
                 it["disc"],it["gst_pct"],it["taxable"],it["cgst"],it["sgst"],it["igst"],it["total"]))
            total_qty = it["qty"] + it["free_qty"]
            cost_per = it["total"] / total_qty if total_qty else 0
            d.execute("update products set stock=stock+?, cost=? where id=?",(total_qty, round(cost_per,2), it["p"]["id"]))
        d.commit(); flash(f"Purchase invoice {inv_number} saved. {len(items)} items received.")
        return redirect(url_for("purchase_view", pid=pur_id))
    prods = [dict(id=p["id"],name=p["name"],barcode=p["barcode"],cost=p["cost"],gst=p["gst"])
             for p in d.execute("select * from products where active=1 order by name")]
    recent = d.execute("select * from purchases order by id desc limit 20").fetchall()
    return page("""
<h1>""" + ICONS['purchase'] + """ {{ t('purchase') }}: Enter Tax Invoice</h1>
{% if not prods %}<div class="card" style="text-align:center;padding:40px"><p class="text-m">Add products first.</p>
<a class="btn bp" href="/products">Go to Products</a></div>{% else %}
<div class="card mb-3"><form method="post" id="pform"><input type="hidden" name="_t" value="{{ token }}">

<div class="fr mb-3">
  <div class="fg" style="flex:2"><label>Supplier / Distributor</label><input name="supplier" placeholder="Distributor name" required></div>
  <div class="fg" style="max-width:200px"><label>Supplier GSTIN</label><input name="supplier_gstin" placeholder="29ABCDE1234F1Z5" maxlength="15" style="text-transform:uppercase"></div>
  <div class="fg" style="max-width:160px"><label>Invoice Number</label><input name="inv_number" placeholder="INV-001" required></div>
  <div class="fg" style="max-width:150px"><label>Invoice Date</label><input name="inv_date" type="date" value="{{ today }}"></div>
</div>

<h2 style="margin-top:0">Invoice Line Items</h2>
<div class="tw mb-2" style="overflow-x:auto"><table id="inv-tbl">
<thead><tr><th>Product</th><th style="width:90px">HSN</th><th style="width:70px">Qty</th><th style="width:60px">Free</th>
<th style="width:90px">Rate ₹</th><th style="width:80px">Disc ₹</th><th style="width:70px">GST%</th>
<th class="n" style="width:90px">Taxable</th><th class="n" style="width:70px">CGST</th><th class="n" style="width:70px">SGST</th>
<th class="n" style="width:90px">Total</th><th style="width:36px"></th></tr></thead>
<tbody id="b"></tbody>
<tfoot><tr style="font-weight:700;background:var(--card-hover)"><td colspan="7" class="n">Totals</td>
<td class="n" id="ft-tax">0.00</td><td class="n" id="ft-cgst">0.00</td><td class="n" id="ft-sgst">0.00</td>
<td class="n" id="ft-total">0.00</td><td></td></tr></tfoot>
</table></div>

<div class="fr mb-2"><div class="fg" style="flex:2"><label>Notes (optional)</label>
<input name="notes" placeholder="Any remarks about this invoice"></div></div>
<div class="flex gap-2"><button type="button" class="bs" onclick="addRow()">""" + ICONS['plus'] + """ Add Line</button>
<button type="submit" class="blg">Save Invoice & Receive Stock</button></div>
</form></div>

<script>
const P={{ prods|tojson }};const tb=document.getElementById('b');let rowId=0;
function fill(tr){const s=tr.querySelector('select');const p=P.find(x=>x.id==+s.value);
  if(p){tr.querySelector('[name=rate]').value=p.cost;tr.querySelector('[name=gst_pct]').value=p.gst}calc()}
function calc(){
  let tTax=0,tCgst=0,tSgst=0,tTotal=0;
  tb.querySelectorAll('tr').forEach(tr=>{
    const q=parseFloat(tr.querySelector('[name=qty]')?.value)||0;
    const rate=parseFloat(tr.querySelector('[name=rate]')?.value)||0;
    const disc=parseFloat(tr.querySelector('[name=item_disc]')?.value)||0;
    const gst=parseFloat(tr.querySelector('[name=gst_pct]')?.value)||0;
    const taxable=Math.round((q*rate-disc)*100)/100;
    const cgst=Math.round(taxable*gst/200*100)/100;
    const sgst=Math.round(taxable*gst/200*100)/100;
    const total=Math.round((taxable+cgst+sgst)*100)/100;
    const tc=tr.querySelectorAll('.calc');
    if(tc[0])tc[0].textContent=taxable.toFixed(2);
    if(tc[1])tc[1].textContent=cgst.toFixed(2);
    if(tc[2])tc[2].textContent=sgst.toFixed(2);
    if(tc[3])tc[3].textContent=total.toFixed(2);
    tTax+=taxable;tCgst+=cgst;tSgst+=sgst;tTotal+=total});
  document.getElementById('ft-tax').textContent=tTax.toFixed(2);
  document.getElementById('ft-cgst').textContent=tCgst.toFixed(2);
  document.getElementById('ft-sgst').textContent=tSgst.toFixed(2);
  document.getElementById('ft-total').textContent=tTotal.toFixed(2)}
function rmRow(btn){btn.closest('tr').remove();calc()}
function addRow(){rowId++;const tr=document.createElement('tr');
  const s=document.createElement('select');s.name='product_id';
  P.forEach(p=>s.add(new Option(p.name+' ('+p.barcode+')',p.id)));
  s.onchange=()=>fill(tr);
  tr.innerHTML='<td></td>'+
    '<td><input name="hsn" placeholder="HSN" style="width:80px"></td>'+
    '<td><input name="qty" type="number" step="any" min="0.01" value="1" oninput="calc()" style="width:60px"></td>'+
    '<td><input name="free_qty" type="number" step="any" min="0" value="0" style="width:50px"></td>'+
    '<td><input name="rate" type="number" step="0.01" min="0" oninput="calc()" style="width:80px"></td>'+
    '<td><input name="item_disc" type="number" step="0.01" min="0" value="0" oninput="calc()" style="width:70px"></td>'+
    '<td><input name="gst_pct" type="number" step="any" value="18" oninput="calc()" style="width:60px"></td>'+
    '<td class="n calc">0.00</td><td class="n calc">0.00</td><td class="n calc">0.00</td><td class="n calc fw6">0.00</td>'+
    '<td><button type="button" class="bg bsm" onclick="rmRow(this)">✕</button></td>';
  tr.cells[0].append(s);tb.append(tr);fill(tr)}
addRow();
</script>{% endif %}

<h2 class="mt-3">Recent Invoices</h2>
<div class="tw"><table><thead><tr><th>Inv Date</th><th>Invoice #</th><th>Supplier</th><th>GSTIN</th>
<th class="n">Subtotal</th><th class="n">CGST</th><th class="n">SGST</th><th class="n">Total</th><th></th></tr></thead>
<tbody>{% for r in recent %}<tr>
<td>{{ r.inv_date }}</td><td class="fw6">{{ r.inv_number }}</td><td>{{ r.supplier }}</td>
<td><code>{{ r.supplier_gstin or '—' }}</code></td>
<td class="n">{{ r.subtotal|inr }}</td><td class="n">{{ r.cgst|inr }}</td><td class="n">{{ r.sgst|inr }}</td>
<td class="n fw6">{{ r.total|inr }}</td>
<td><a href="/purchase/{{ r.id }}" class="bs bsm">View</a></td>
</tr>{% else %}<tr><td colspan="9" class="empty-msg">No invoices yet</td></tr>{% endfor %}</tbody></table></div>""",
    prods=prods, recent=recent, today=date.today().isoformat())


@app.route("/purchase/<int:pid>")
@login_required
def purchase_view(pid):
    d = db()
    p = d.execute("select * from purchases where id=?",(pid,)).fetchone() or abort(404)
    items = d.execute("""select pi.*,pr.name pname,pr.barcode pbarcode from purchase_items pi
        join products pr on pr.id=pi.product_id where pi.purchase_id=?""",(pid,)).fetchall()
    return page("""
<div class="page-head"><h1>Purchase Invoice</h1><a href="/purchase" class="btn bs">← Back</a></div>
<div class="card mb-3">
<div class="fr mb-3">
  <div><span class="text-m text-sm">Supplier</span><div class="fw7">{{ p.supplier }}</div></div>
  <div><span class="text-m text-sm">GSTIN</span><div class="fw6"><code>{{ p.supplier_gstin or '—' }}</code></div></div>
  <div><span class="text-m text-sm">Invoice #</span><div class="fw7">{{ p.inv_number }}</div></div>
  <div><span class="text-m text-sm">Invoice Date</span><div>{{ p.inv_date }}</div></div>
  <div><span class="text-m text-sm">Received On</span><div>{{ p.recv_date }}</div></div>
</div>
{% if p.notes %}<p class="text-m text-sm mb-2">Notes: {{ p.notes }}</p>{% endif %}
</div>

<div class="tw mb-3"><table><thead><tr><th>Product</th><th>HSN</th><th class="n">Qty</th><th class="n">Free</th>
<th class="n">Rate</th><th class="n">Disc</th><th class="n">GST%</th><th class="n">Taxable</th>
<th class="n">CGST</th><th class="n">SGST</th><th class="n">IGST</th><th class="n">Total</th></tr></thead>
<tbody>{% for i in items %}<tr>
<td class="fw6">{{ i.pname }} <code>({{ i.pbarcode }})</code></td><td>{{ i.hsn or '—' }}</td>
<td class="n">{{ '%g'|format(i.qty) }}</td><td class="n">{{ '%g'|format(i.free_qty) }}</td>
<td class="n">{{ i.rate|inr }}</td><td class="n">{{ i.discount|inr }}</td><td class="n">{{ '%g'|format(i.gst_pct) }}%</td>
<td class="n">{{ i.taxable|inr }}</td><td class="n">{{ i.cgst|inr }}</td><td class="n">{{ i.sgst|inr }}</td>
<td class="n">{{ i.igst|inr }}</td><td class="n fw7">{{ i.total|inr }}</td></tr>{% endfor %}</tbody>
<tfoot><tr style="font-weight:700;background:var(--card-hover)">
<td colspan="7" class="n">Grand Total</td><td class="n">{{ p.subtotal|inr }}</td>
<td class="n">{{ p.cgst|inr }}</td><td class="n">{{ p.sgst|inr }}</td><td class="n">{{ p.igst|inr }}</td>
<td class="n">{{ p.total|inr }}</td></tr></tfoot></table></div>
<button onclick="print()" class="bs noprint">🖨️ Print Invoice</button>""", p=p, items=items)


# ── Expenses ─────────────────────────────────────────────────────────
EXPENSE_CATS = ["Rent","Electricity","Water","Salary","Transport","Supplies","Maintenance","Marketing","Other"]

@app.route("/expenses", methods=["GET","POST"])
@login_required
def expenses():
    d = db()
    if request.method=="POST":
        act = request.form.get("action","add")
        if act=="delete":
            d.execute("delete from expenses where id=?",(request.form.get("eid"),)); d.commit()
            flash("Expense deleted."); return redirect("/expenses")
        amt = num(request.form.get("amount"))
        if amt<=0: flash("Enter a valid amount."); return redirect("/expenses")
        dt = request.form.get("date") or date.today().isoformat()
        d.execute("insert into expenses(date,category,description,amount) values(?,?,?,?)",
                  (dt, request.form.get("category","Other"), request.form.get("description","").strip(), round(amt,2)))
        d.commit(); flash("Expense recorded."); return redirect("/expenses")
    month = request.args.get("m") or date.today().strftime("%Y-%m")
    rows = d.execute("select * from expenses where date like ? order by date desc, id desc",(month+"%",)).fetchall()
    total = sum(r["amount"] for r in rows)
    by_cat = {}
    for r in rows: by_cat[r["category"]] = by_cat.get(r["category"],0) + r["amount"]
    return page("""
<h1>""" + ICONS['expenses'] + """ {{ t('expenses') }}</h1>
<div class="card mb-3"><form method="post"><input type="hidden" name="_t" value="{{ token }}">
<div class="fr">
  <div class="fg" style="max-width:140px"><label>Date</label><input name="date" type="date" value="{{ today }}"></div>
  <div class="fg" style="max-width:150px"><label>Category</label><select name="category">
    {% for c in cats %}<option>{{ c }}</option>{% endfor %}</select></div>
  <div class="fg" style="flex:2"><label>Description</label><input name="description" placeholder="What was the expense for?"></div>
  <div class="fg" style="max-width:130px"><label>Amount ₹</label><input name="amount" type="number" step="0.01" min="0.01" placeholder="0.00" required></div>
  <div style="display:flex;align-items:flex-end"><button type="submit">Add Expense</button></div>
</div></form></div>

<div class="filters"><form class="flex gap-2"><input type="month" name="m" value="{{ month }}" style="width:auto">
<button type="submit" class="bs">Show Month</button></form>
<span class="text-m text-sm ml-a">Total: <b style="color:var(--err)">{{ total|inr }}</b></span></div>

{% if by_cat %}<div class="metrics mb-3">{% for cat,amt in by_cat.items() %}
<div class="metric"><div class="ml">{{ cat }}</div><div class="mv" style="font-size:18px">{{ amt|inr }}</div></div>
{% endfor %}</div>{% endif %}

<div class="tw"><table><thead><tr><th>Date</th><th>Category</th><th>Description</th><th class="n">Amount</th><th></th></tr></thead>
<tbody>{% for e in rows %}<tr><td>{{ e.date }}</td><td><span class="badge b-warn">{{ e.category }}</span></td>
<td>{{ e.description or '—' }}</td><td class="n fw6">{{ e.amount|inr }}</td>
<td><form method="post" style="display:inline" onsubmit="return confirm('Delete this expense?')">
<input type="hidden" name="_t" value="{{ token }}"><input type="hidden" name="action" value="delete">
<input type="hidden" name="eid" value="{{ e.id }}"><button class="bg bsm" style="color:var(--err)">✕</button></form></td>
</tr>{% else %}<tr><td colspan="5" class="empty-msg">No expenses this month</td></tr>{% endfor %}</tbody></table></div>""",
    rows=rows, total=total, by_cat=by_cat, cats=EXPENSE_CATS,
    today=date.today().isoformat(), month=month)


# ── Reports ──────────────────────────────────────────────────────────
@app.route("/report")
@login_required
def report():
    d = db()
    day = request.args.get("d") or date.today().isoformat()
    like = day+"%"
    one = lambda s,*a: d.execute(s,a).fetchone()
    tot = one("select count(*) n, coalesce(sum(total),0) s, coalesce(sum(discount),0) disc, coalesce(sum(tax),0) tax from bills where ts like ?",like)
    cogs_r = one("select coalesce(sum(bi.qty*p.cost),0) from bill_items bi join bills b on b.id=bi.bill_id join products p on p.id=bi.product_id where b.ts like ?",like)
    cogs = cogs_r[0] if cogs_r else 0
    exp_r = one("select coalesce(sum(amount),0) from expenses where date=?",day)
    day_expenses = exp_r[0] if exp_r else 0
    modes = d.execute("select mode, sum(total) s from bills where ts like ? group by mode",(like,)).fetchall()
    depts = d.execute("select p.dept, sum(bi.amount) s from bill_items bi join bills b on b.id=bi.bill_id "
        "join products p on p.id=bi.product_id where b.ts like ? group by p.dept order by s desc",(like,)).fetchall()
    top_items = d.execute("select bi.name, sum(bi.qty) tq, sum(bi.amount) ta from bill_items bi "
        "join bills b on b.id=bi.bill_id where b.ts like ? group by bi.product_id order by tq desc limit 10",(like,)).fetchall()
    bills = d.execute("select * from bills where ts like ? order by id desc limit 100",(like,)).fetchall()
    low = d.execute("select * from products where active=1 and stock<=reorder order by stock").fetchall()
    sv = one("select coalesce(sum(stock*cost),0) from products where active=1")[0]
    gross_profit = tot["s"] - cogs
    net_profit = gross_profit - day_expenses
    return page("""
<div class="page-head"><h1>""" + ICONS['report'] + """ {{ t('report') }}</h1>
<form class="flex gap-2 ml-a"><input type="date" name="d" value="{{ day }}" style="width:auto"><button type="submit" class="bs">Show Day</button></form></div>

<div class="metrics">
  <div class="metric"><div class="ml">Sales</div><div class="mv">{{ tot.s|inr }}</div></div>
  <div class="metric"><div class="ml">Bills</div><div class="mv">{{ tot.n }}</div></div>
  <div class="metric"><div class="ml">Gross Profit</div><div class="mv" style="color:var(--ok)">{{ gp|inr }}</div></div>
  <div class="metric"><div class="ml">Net Profit</div><div class="mv" style="color:{% if np>=0 %}var(--ok){% else %}var(--err){% endif %}">{{ np|inr }}</div></div>
</div>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px" class="mb-3">
  <div class="card"><h2 style="margin-top:0">By Payment Mode</h2><table>
  {% for m in modes %}<tr><td><span class="badge b-pri">{{ m.mode|upper }}</span></td><td class="n fw6">{{ m.s|inr }}</td></tr>
  {% else %}<tr><td class="text-m">No sales</td></tr>{% endfor %}</table></div>
  <div class="card"><h2 style="margin-top:0">Summary</h2><table>
  <tr><td>Discounts</td><td class="n">{{ tot.disc|inr }}</td></tr>
  <tr><td>Tax collected</td><td class="n">{{ tot.tax|inr }}</td></tr>
  <tr><td>Cost of goods</td><td class="n">{{ cogs|inr }}</td></tr>
  <tr><td>Expenses</td><td class="n" style="color:var(--err)">{{ day_exp|inr }}</td></tr>
  <tr><td>Stock value</td><td class="n">{{ sv|inr }}</td></tr></table></div>
</div>

{% if depts %}<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px" class="mb-3">
<div class="card"><h2 style="margin-top:0">By Department</h2><table>
{% for x in depts %}<tr><td><span class="badge b-pri">{{ x.dept }}</span></td><td class="n fw6">{{ x.s|inr }}</td></tr>{% endfor %}</table></div>
<div class="card"><h2 style="margin-top:0">Top Selling</h2><table>
{% for t in top %}<tr><td>{{ t.name }}</td><td class="n text-m">{{ '%g'|format(t.tq) }} sold</td><td class="n fw6">{{ t.ta|inr }}</td></tr>{% endfor %}</table></div>
</div>{% endif %}

<h2>Bills</h2><div class="tw mb-3"><table><thead><tr><th>Bill No.</th><th>Time</th><th>Payment</th><th class="n">Amount</th></tr></thead>
<tbody>{% for b in bills %}<tr><td><a href="/bill/{{ b.id }}" class="fw6">{{ b.number }}</a></td><td>{{ b.ts[11:16] }}</td>
<td><span class="badge b-pri">{{ b.mode|upper }}</span></td><td class="n fw6">{{ b.total|inr }}</td></tr>
{% else %}<tr><td colspan="4" class="empty-msg">No bills</td></tr>{% endfor %}</tbody></table></div>

<h2>Reorder List</h2><div class="tw"><table><thead><tr><th>Product</th><th>Dept</th><th class="n">Stock</th><th class="n">Reorder At</th></tr></thead>
<tbody>{% for p in low %}<tr><td class="fw6">{{ p.name }}</td><td><span class="badge b-pri">{{ p.dept }}</span></td>
<td class="n"><span class="badge b-err">{{ '%g'|format(p.stock) }}</span></td><td class="n">{{ '%g'|format(p.reorder) }}</td></tr>
{% else %}<tr><td colspan="4" class="empty-msg">✓ All stocked up</td></tr>{% endfor %}</tbody></table></div>""",
    day=day,tot=tot,modes=modes,depts=depts,bills=bills,low=low,sv=sv,
    top=top_items,gp=gross_profit,np=net_profit,cogs=cogs,day_exp=day_expenses)


# ── JSON API for product search ──────────────────────────────────────
@app.route("/api/products")
@login_required
def api_products():
    rows = db().execute("select id,barcode,name,mrp,stock,dept from products where active=1 order by name").fetchall()
    return [dict(r) for r in rows]


init()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
