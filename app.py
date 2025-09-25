# app.py — Partie 1 : Auth, DB, Utils
from flask import (
    Flask, render_template, request, redirect, url_for, flash, abort,
    send_file, Response, session, g
)
import sqlite3, io, csv, os, tempfile
from datetime import datetime
from functools import wraps
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from passlib.hash import pbkdf2_sha256 as pwd_hasher
import pandas as pd
from forms import LoginForm

DB_PATH = "materiel.db"
app = Flask(__name__)
app.secret_key = "dev-change-this"  # à personnaliser

# ---------------- Schéma DB ----------------
SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS produits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT NOT NULL,
    reference TEXT UNIQUE,
    categorie TEXT,
    marque TEXT,
    prix_achat REAL NOT NULL DEFAULT 0,
    prix_vente REAL NOT NULL DEFAULT 0,
    quantite INTEGER NOT NULL DEFAULT 0,
    numero_serie TEXT,
    garantie_mois INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT NOT NULL,
    telephone TEXT,
    email TEXT,
    entreprise TEXT,
    adresse TEXT,
    ville TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ventes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER,
    date TEXT DEFAULT CURRENT_TIMESTAMP,
    total_ht REAL DEFAULT 0,
    tva REAL DEFAULT 0,
    total_ttc REAL DEFAULT 0,
    mode_paiement TEXT,
    acompte REAL DEFAULT 0,
    statut TEXT DEFAULT 'payé',
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS lignes_vente (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vente_id INTEGER NOT NULL,
    produit_id INTEGER NOT NULL,
    quantite INTEGER NOT NULL DEFAULT 1,
    prix_unitaire REAL NOT NULL DEFAULT 0,
    remise_pct REAL DEFAULT 0,
    FOREIGN KEY (vente_id) REFERENCES ventes(id) ON DELETE CASCADE,
    FOREIGN KEY (produit_id) REFERENCES produits(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fournisseurs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT NOT NULL,
    contact TEXT,
    telephone TEXT,
    email TEXT,
    adresse TEXT,
    ville TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS achats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fournisseur_id INTEGER,
    date TEXT DEFAULT CURRENT_TIMESTAMP,
    total_ht REAL DEFAULT 0,
    tva REAL DEFAULT 0,
    total_ttc REAL DEFAULT 0,
    mode_paiement TEXT,
    statut TEXT DEFAULT 'reçu',
    FOREIGN KEY (fournisseur_id) REFERENCES fournisseurs(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS lignes_achat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    achat_id INTEGER NOT NULL,
    produit_id INTEGER NOT NULL,
    quantite INTEGER NOT NULL DEFAULT 1,
    prix_unitaire REAL NOT NULL DEFAULT 0,
    FOREIGN KEY (achat_id) REFERENCES achats(id) ON DELETE CASCADE,
    FOREIGN KEY (produit_id) REFERENCES produits(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    societe TEXT,
    adresse TEXT,
    telephone TEXT,
    email TEXT,
    tva_rate REAL DEFAULT 0.18
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    pwd_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'vendeur' -- 'owner' | 'admin' | 'vendeur'
);

CREATE INDEX IF NOT EXISTS idx_lv_v ON lignes_vente(vente_id);
CREATE INDEX IF NOT EXISTS idx_lv_p ON lignes_vente(produit_id);
CREATE INDEX IF NOT EXISTS idx_la_a ON lignes_achat(achat_id);
CREATE INDEX IF NOT EXISTS idx_la_p ON lignes_achat(produit_id);
"""

# ---------------- DB helpers ----------------
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON;")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def ensure_schema():
    conn = sqlite3.connect(DB_PATH) # Utiliser une connexion directe pour l'initialisation
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO settings (id, societe, tva_rate) VALUES (1, 'LeBoulangerComble', 0.18)")
        # Seed owner
        row = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()
        if not row:
            conn.execute("INSERT INTO users (username, pwd_hash, role) VALUES (?,?,?)",
                         ('admin', pwd_hasher.hash('admin'), 'owner'))
        # ---- MIGRATION douce pour le compte admin ----
        cur = conn.execute("SELECT id, username, pwd_hash FROM users WHERE username='admin'").fetchone()
        if cur and not is_pbkdf2_hash(cur["pwd_hash"] or ""):
            conn.execute("UPDATE users SET pwd_hash=? WHERE id=?", (pwd_hasher.hash("admin"), cur["id"]))
        # ----------------------------------------------
        conn.commit()
    finally:
        conn.close()


# ---------------- Utils ----------------
def like(s): return f"%{(s or '').strip()}%"
def parse_float(val, default=0.0):
    try: return float(val)
    except (TypeError, ValueError): return default
def parse_int(val, default=0):
    try: return int(val)
    except (TypeError, ValueError): return default
def parse_date(s): return (s or "").strip() or None

def get_settings():
    db = get_db()
    row = db.execute("SELECT * FROM settings WHERE id=1").fetchone()
    return row

def get_tva_rate():
    st = get_settings()
    return float(st["tva_rate"] if st and st["tva_rate"] is not None else 0.18)

def calcul_totaux(lignes):
    total_ht = 0.0
    for lg in lignes:
        qte = parse_int(lg.get("qte"), 0)
        pu = parse_float(lg.get("pu"), 0)
        remise = parse_float(lg.get("remise_pct"), 0)
        total_ht += qte * pu * (1 - remise/100.0)
    tva = total_ht * get_tva_rate()
    ttc = total_ht + tva
    return (round(total_ht,2), round(tva,2), round(ttc,2))

# --- à placer près du haut, avec les autres utils si tu veux ---
def is_pbkdf2_hash(h: str) -> bool:
    h = h or ""
    return h.startswith("$pbkdf2-sha256$") or h.startswith("pbkdf2_sha256$")

def verify_and_upgrade_password(user_row, candidate_password):
    """
    Tente de vérifier le mot de passe avec PBKDF2.
    Si le hash stocké n'est pas valide (ancien format / en clair),
    on tente une comparaison "en clair" pour les vieilles données.
    En cas de succès, on ré-hashe et on met à jour la base.
    """
    stored = (user_row["pwd_hash"] or "").strip()
    ok = False
    try:
        ok = pwd_hasher.verify(candidate_password, stored)
    except (ValueError, TypeError):
        # ancien format (ex: en clair 'admin' ou autre)
        ok = (stored == candidate_password)

    if ok and not is_pbkdf2_hash(stored):
        # upgrade du hash
        try:
            new_hash = pwd_hasher.hash(candidate_password)
            db = get_db()
            db.execute("UPDATE users SET pwd_hash=? WHERE id=?", (new_hash, user_row["id"])).commit()
        except Exception:
            pass
    return ok


# ---------------- Auth & Rôles ----------------
def has_role(*roles):
    u = session.get("user")
    return bool(u and u.get("role") in roles)

def role_required(*roles):
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not has_role(*roles):
                flash("Accès refusé.", "danger")
                return redirect(url_for("index"))
            return f(*args, **kwargs)
        return wrapper
    return deco

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login", next=request.full_path))
        return f(*args, **kwargs)
    return wrapper

@app.before_request
def load_settings_into_g():
    try:
        g.settings = get_settings()
    except Exception:
        g.settings = None

# Garde global : tout exige login sauf /login et static
@app.before_request
def require_login_globally():
    public_endpoints = {"login"}
    ep = (request.endpoint or "")
    if ep.startswith("static") or ep in public_endpoints:
        return
    if not session.get("user"):
        return redirect(url_for("login", next=request.full_path))

# ---------------- Pagination ----------------
def get_page_args(default_per_page=20):
    try: page = max(1, int(request.args.get("page", 1)))
    except: page = 1
    try: per_page = max(1, int(request.args.get("per_page", default_per_page)))
    except: per_page = default_per_page
    offset = (page - 1) * per_page
    return page, per_page, offset

# ---------------- Auth routes ----------------
# --- remplace ENTIEREMENT ta route /login par ceci ---
@app.route("/login", methods=["GET","POST"])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        username = form.username.data
        password = form.password.data
        db = get_db()
        u = db.execute("SELECT id, username, pwd_hash, role FROM users WHERE username=?", (username,)).fetchone()

        if not u or not verify_and_upgrade_password(u, password):
            flash("Identifiants invalides.", "danger")
        else:
            session["user"] = {"id": u["id"], "username": u["username"], "role": u["role"]}
            flash(f"Bienvenue {u['username']} !", "success")
            return redirect(request.args.get("next") or url_for("index"))

    return render_template("login.html", form=form)


@app.route("/logout")
def logout():
    session.pop("user", None)
    flash("Déconnecté.", "info")
    return redirect(url_for("login"))

@app.route("/mon_mot_de_passe", methods=["GET","POST"])
@login_required
def change_password():
    if request.method == "GET":
        return render_template("change_password.html")
    cur_pwd = request.form.get("current") or ""
    new_pwd = request.form.get("new") or ""
    confirm = request.form.get("confirm") or ""
    if new_pwd != confirm or len(new_pwd) < 6:
        flash("Nouveau mot de passe invalide (min 6) ou non confirmé.", "danger")
        return redirect(url_for("change_password"))
    db = get_db()
    u = db.execute("SELECT id, pwd_hash FROM users WHERE id=?", (session["user"]["id"],)).fetchone()
    if not u or not pwd_hasher.verify(cur_pwd, u["pwd_hash"]):
        flash("Mot de passe actuel incorrect.", "danger")
        return redirect(url_for("change_password"))
    db.execute("UPDATE users SET pwd_hash=? WHERE id=?", (pwd_hasher.hash(new_pwd), u["id"]))
    db.commit()
    flash("Mot de passe modifié.", "success")
    return redirect(url_for("index"))

# app.py — Partie 2 : Stock & Clients
# -------- STOCK --------
@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    min_qte = request.args.get("min_qte", "").strip()
    conn = get_db()
    sql = "SELECT * FROM produits WHERE 1=1"
    params = []
    if q:
        sql += " AND (nom LIKE ? OR reference LIKE ? OR categorie LIKE ? OR marque LIKE ?)"
        params += [like(q), like(q), like(q), like(q)]
    if min_qte.isdigit():
        sql += " AND quantite >= ?"; params.append(int(min_qte))
    sql += " ORDER BY id DESC"
    page, per_page, offset = get_page_args()
    count = conn.execute("SELECT COUNT(*) AS n FROM ("+sql+")", params).fetchone()["n"]
    produits = conn.execute(sql + " LIMIT ? OFFSET ?", params+[per_page, offset]).fetchall()
    is_vendor = session.get('user',{}).get('role') == 'vendeur'
    return render_template("index.html", produits=produits, q=q, min_qte=min_qte,
                           page=page, per_page=per_page, total=count,
                           hide_prix_achat=is_vendor)

@app.route("/produits/ajouter", methods=["POST"])
@login_required
def produits_ajouter():
    nom = (request.form.get("nom") or "").strip()
    if not nom:
        flash("Le nom du produit est obligatoire.", "danger")
        return redirect(url_for("index"))
    reference = (request.form.get("reference") or "").strip()
    categorie = (request.form.get("categorie") or "").strip()
    marque = (request.form.get("marque") or "").strip()
    if session.get('user',{}).get('role') == 'vendeur':
        prix_achat = 0.0
    else:
        prix_achat = parse_float(request.form.get("prix_achat"), 0)
    prix_vente = parse_float(request.form.get("prix_vente"), 0)
    quantite = parse_int(request.form.get("quantite"), 0)
    numero_serie = (request.form.get("numero_serie") or "").strip()
    garantie_mois = parse_int(request.form.get("garantie_mois"), 0)

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO produits (nom, reference, categorie, marque, prix_achat, prix_vente, quantite, numero_serie, garantie_mois, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (nom, reference, categorie, marque, prix_achat, prix_vente, quantite, numero_serie, garantie_mois, datetime.utcnow().isoformat()))
        conn.commit()
        flash("Produit ajouté.", "success")
    except sqlite3.IntegrityError as e:
        flash(f"Erreur (référence en double ?) : {e}", "danger")
    return redirect(url_for("index"))

@app.route("/produits/<int:pid>/supprimer", methods=["POST"])
@role_required('owner','admin')
def produits_supprimer(pid):
    conn = get_db()
    conn.execute("DELETE FROM produits WHERE id = ?", (pid,))
    conn.commit()
    flash("Produit supprimé.", "success")
    return redirect(url_for("index"))

# -------- CLIENTS --------
@app.route("/clients")
def clients_list():
    q = request.args.get("q", "").strip()
    conn = get_db()
    sql = "SELECT * FROM clients WHERE 1=1"
    params = []
    if q:
        sql += " AND (nom LIKE ? OR telephone LIKE ? OR email LIKE ? OR entreprise LIKE ? OR ville LIKE ?)"
        params += [like(q), like(q), like(q), like(q), like(q)]
    sql += " ORDER BY id DESC"
    page, per_page, offset = get_page_args()
    count = conn.execute("SELECT COUNT(*) AS n FROM ("+sql+")", params).fetchone()["n"]
    clients = conn.execute(sql + " LIMIT ? OFFSET ?", params+[per_page, offset]).fetchall()
    return render_template("clients.html", clients=clients, q=q,
                           page=page, per_page=per_page, total=count)

@app.route("/clients/ajouter", methods=["POST"])
@login_required
def clients_ajouter():
    nom = (request.form.get("nom") or "").strip()
    if not nom:
        flash("Le nom du client est obligatoire.", "danger")
        return redirect(url_for("clients_list"))
    telephone = (request.form.get("telephone") or "").strip()
    email = (request.form.get("email") or "").strip()
    entreprise = (request.form.get("entreprise") or "").strip()
    adresse = (request.form.get("adresse") or "").strip()
    ville = (request.form.get("ville") or "").strip()

    conn = get_db()
    conn.execute("""
        INSERT INTO clients (nom, telephone, email, entreprise, adresse, ville)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (nom, telephone, email, entreprise, adresse, ville))
    conn.commit()
    flash("Client ajouté.", "success")
    return redirect(url_for("clients_list"))

@app.route("/clients/<int:client_id>/modifier", methods=["GET","POST"])
@login_required
def clients_modifier(client_id):
    conn = get_db()
    if request.method == "GET":
        client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        if not client: abort(404)
        return render_template("clients_edit.html", client=client)

    nom = (request.form.get("nom") or "").strip()
    telephone = (request.form.get("telephone") or "").strip()
    email = (request.form.get("email") or "").strip()
    entreprise = (request.form.get("entreprise") or "").strip()
    adresse = (request.form.get("adresse") or "").strip()
    ville = (request.form.get("ville") or "").strip()
    if not nom:
        flash("Le nom du client est obligatoire.", "danger")
        return redirect(url_for("clients_modifier", client_id=client_id))
    conn.execute("""
        UPDATE clients SET nom=?, telephone=?, email=?, entreprise=?, adresse=?, ville=?
        WHERE id=?
    """, (nom, telephone, email, entreprise, adresse, ville, client_id))
    conn.commit()
    flash("Client modifié.", "success")
    return redirect(url_for("clients_list"))

@app.route("/clients/<int:client_id>/supprimer", methods=["POST"])
@role_required('owner','admin')
def clients_supprimer(client_id):
    conn = get_db()
    conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    conn.commit()
    flash("Client supprimé.", "success")
    return redirect(url_for("clients_list"))

# app.py — Partie 3 : Ventes & PDF
@app.route("/ventes")
def ventes_list():
    q = request.args.get("q", "").strip()
    d1 = parse_date(request.args.get("date_from"))
    d2 = parse_date(request.args.get("date_to"))
    conn = get_db()
    clients = conn.execute("SELECT id, nom FROM clients ORDER BY nom").fetchall()
    produits = conn.execute("SELECT id, nom, prix_vente, quantite FROM produits ORDER BY nom").fetchall()

    sql = """
    SELECT v.id, v.date, v.total_ht, v.tva, v.total_ttc, v.mode_paiement, v.statut,
           c.nom AS client_nom
    FROM ventes v
    LEFT JOIN clients c ON c.id = v.client_id
    WHERE 1=1
    """
    params = []
    if q:
        sql += " AND (c.nom LIKE ? OR v.mode_paiement LIKE ?)"
        params += [like(q), like(q)]
    if d1:
        sql += " AND date(v.date) >= date(?)"; params.append(d1)
    if d2:
        sql += " AND date(v.date) <= date(?)"; params.append(d2)
    sql += " ORDER BY v.id DESC"

    page, per_page, offset = get_page_args()
    count = conn.execute("SELECT COUNT(*) AS n FROM ("+sql+")", params).fetchone()["n"]
    ventes = conn.execute(sql + " LIMIT ? OFFSET ?", params+[per_page, offset]).fetchall()
    return render_template("ventes.html", ventes=ventes, clients=clients, produits=produits,
                           q=q, date_from=d1 or "", date_to=d2 or "",
                           page=page, per_page=per_page, total=count)

@app.route("/ventes/ajouter", methods=["POST"])
@login_required
def ventes_ajouter():
    conn = get_db()
    client_id = request.form.get("client_id")
    mode_paiement = (request.form.get("mode_paiement") or "").strip()
    acompte = parse_float(request.form.get("acompte"), 0)

    produit_ids = request.form.getlist("produit_id[]")
    qtes = request.form.getlist("qte[]")
    pus = request.form.getlist("pu[]")
    remises = request.form.getlist("remise_pct[]")

    lignes = []
    for i in range(len(produit_ids)):
        pid = parse_int(produit_ids[i], 0)
        qte = parse_int(qtes[i], 0)
        pu = parse_float(pus[i], 0)
        remise = parse_float(remises[i], 0)
        if pid > 0 and qte > 0 and pu >= 0:
            lignes.append({"produit_id": pid, "qte": qte, "pu": pu, "remise_pct": remise})

    if not lignes:
        flash("Ajoute au moins une ligne de vente.", "danger")
        return redirect(url_for("ventes_list"))

    try:
        conn.isolation_level = None; conn.execute("BEGIN")
        for lg in lignes:
            row = conn.execute("SELECT quantite, nom FROM produits WHERE id = ?", (lg["produit_id"],)).fetchone()
            if not row: raise ValueError("Produit introuvable.")
            if row["quantite"] < lg["qte"]:
                raise ValueError(f"Stock insuffisant pour {row['nom']} (dispo {row['quantite']}, demandé {lg['qte']}).")
        total_ht, tva, total_ttc = calcul_totaux(lignes)
        cur = conn.execute("""
            INSERT INTO ventes (client_id, total_ht, tva, total_ttc, mode_paiement, acompte, statut, date)
            VALUES (?, ?, ?, ?, ?, ?, 'payé', CURRENT_TIMESTAMP)
        """, (client_id if client_id else None, total_ht, tva, total_ttc, mode_paiement, acompte))
        vente_id = cur.lastrowid
        for lg in lignes:
            conn.execute("""
                INSERT INTO lignes_vente (vente_id, produit_id, quantite, prix_unitaire, remise_pct)
                VALUES (?, ?, ?, ?, ?)
            """, (vente_id, lg["produit_id"], lg["qte"], lg["pu"], lg["remise_pct"]))
            conn.execute("UPDATE produits SET quantite = quantite - ? WHERE id = ?", (lg["qte"], lg["produit_id"]))
        conn.execute("COMMIT"); flash(f"Vente #{vente_id} enregistrée (TTC {total_ttc}).", "success")
    except Exception as e:
        conn.execute("ROLLBACK"); flash(f"Erreur lors de l'enregistrement: {e}", "danger")
    return redirect(url_for("ventes_list"))

@app.route("/ventes/<int:vente_id>")
def vente_detail(vente_id):
    conn = get_db()
    vente = conn.execute("""
        SELECT v.*, c.nom AS client_nom, c.telephone, c.email, c.entreprise, c.adresse, c.ville
        FROM ventes v LEFT JOIN clients c ON c.id = v.client_id
        WHERE v.id = ?
    """, (vente_id,)).fetchone()
    if not vente:
        abort(404)
    lignes = conn.execute("""
        SELECT lv.*, p.nom AS produit_nom, p.reference
        FROM lignes_vente lv JOIN produits p ON p.id = lv.produit_id
        WHERE lv.vente_id = ?
    """, (vente_id,)).fetchall()
    return render_template("vente_detail.html", vente=vente, lignes=lignes, tva_rate=int(get_tva_rate()*100))

@app.route("/ventes/<int:vente_id>/annuler", methods=["POST"])
@role_required('owner','admin')
def vente_annuler(vente_id):
    conn = get_db()
    try:
        conn.isolation_level = None; conn.execute("BEGIN")
        v = conn.execute("SELECT id, statut FROM ventes WHERE id = ?", (vente_id,)).fetchone()
        if not v: conn.execute("ROLLBACK"); flash("Vente introuvable.", "danger"); return redirect(url_for("ventes_list"))
        if v["statut"] == "annulé": conn.execute("ROLLBACK"); flash("Cette vente est déjà annulée.", "warning"); return redirect(url_for("ventes_list"))
        lignes = conn.execute("SELECT produit_id, quantite FROM lignes_vente WHERE vente_id = ?", (vente_id,)).fetchall()
        for lg in lignes:
            conn.execute("UPDATE produits SET quantite = quantite + ? WHERE id = ?", (int(lg["quantite"]), int(lg["produit_id"])))
        conn.execute("UPDATE ventes SET statut='annulé', total_ht=0, tva=0, total_ttc=0 WHERE id=?", (vente_id,))
        conn.execute("COMMIT"); flash(f"Vente #{vente_id} annulée et stock rétabli.", "success")
    except Exception as e:
        conn.execute("ROLLBACK"); flash(f"Échec annulation: {e}", "danger")
    return redirect(url_for("ventes_list"))

# -------- Facture PDF --------
@app.route("/ventes/<int:vente_id>/facture.pdf")
def facture_pdf(vente_id):
    st = g.settings or {}
    conn = get_db()
    vente = conn.execute("""
        SELECT v.*, c.nom AS client_nom, c.telephone, c.email, c.entreprise, c.adresse, c.ville
        FROM ventes v LEFT JOIN clients c ON c.id = v.client_id
        WHERE v.id = ?
    """, (vente_id,)).fetchone()
    if not vente: conn.close(); abort(404)
    lignes = conn.execute("""
        SELECT lv.*, p.nom AS produit_nom, p.reference
        FROM lignes_vente lv JOIN produits p ON p.id = lv.produit_id
        WHERE lv.vente_id = ?
    """, (vente_id,)).fetchall()

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    left = 20*mm; y = H - 20*mm

    c.setFont("Helvetica-Bold", 14); c.drawString(left, y, (st["societe"] if st and st["societe"] else "LeBoulangerComble"))
    c.setFont("Helvetica", 9); y -= 5*mm; c.drawString(left, y, "Vente de matériel boulangerie-pâtisserie")
    y -= 5*mm; c.drawString(left, y, f"Adresse: {(st['adresse'] if st else '')}  |  Téléphone: {(st['telephone'] if st else '')}  |  Email: {(st['email'] if st else '')}")
    y -= 10*mm; c.setFont("Helvetica-Bold", 12); c.drawString(left, y, f"FACTURE N° {vente['id']}")
    c.setFont("Helvetica", 9); c.drawRightString(W - left, y, f"Date: {vente['date']}")
    y -= 8*mm; c.setFont("Helvetica-Bold", 10); c.drawString(left, y, "Client")
    y -= 5*mm; c.setFont("Helvetica", 9); c.drawString(left, y, f"{vente['client_nom'] or 'Comptoir'}")
    y -= 5*mm
    if vente['entreprise']: c.drawString(left, y, f"Entreprise: {vente['entreprise']}"); y -= 5*mm
    if vente['telephone']: c.drawString(left, y, f"Téléphone: {vente['telephone']}"); y -= 5*mm
    if vente['email']: c.drawString(left, y, f"Email: {vente['email']}"); y -= 5*mm
    if vente['adresse'] or vente['ville']: c.drawString(left, y, f"Adresse: {vente['adresse'] or ''} {vente['ville'] or ''}"); y -= 7*mm

    y -= 3*mm; c.setFont("Helvetica-Bold", 9)
    c.drawString(left, y, "Désignation"); c.drawRightString(W - 70*mm, y, "PU")
    c.drawRightString(W - 50*mm, y, "Qté"); c.drawRightString(W - 30*mm, y, "Rem. %")
    c.drawRightString(W - left, y, "Montant")
    y -= 4*mm; c.line(left, y, W - left, y); y -= 6*mm; c.setFont("Helvetica", 9)

    total_ht_calc = 0.0
    for lg in lignes:
        designation = f"{lg['produit_nom']} [{lg['reference'] or ''}]"
        pu = float(lg['prix_unitaire']); qte = int(lg['quantite']); remise = float(lg['remise_pct'] or 0)
        montant = qte * pu * (1 - remise/100.0); total_ht_calc += montant
        if y < 40*mm:
            c.showPage(); y = H - 20*mm
            c.setFont("Helvetica-Bold", 9)
            c.drawString(left, y, "Désignation"); c.drawRightString(W - 70*mm, y, "PU")
            c.drawRightString(W - 50*mm, y, "Qté"); c.drawRightString(W - 30*mm, y, "Rem. %")
            c.drawRightString(W - left, y, "Montant")
            y -= 4*mm; c.line(left, y, W - left, y); y -= 6*mm; c.setFont("Helvetica", 9)
        c.drawString(left, y, designation[:70]); c.drawRightString(W - 70*mm, y, f"{pu:,.2f}")
        c.drawRightString(W - 50*mm, y, f"{qte}"); c.drawRightString(W - 30*mm, y, f"{remise:.1f}")
        c.drawRightString(W - left, y, f"{montant:,.2f}"); y -= 6*mm

    tva = total_ht_calc * get_tva_rate(); ttc = total_ht_calc + tva
    y -= 4*mm; c.line(left, y, W - left, y); y -= 14*mm; c.setFont("Helvetica-Bold", 10)
    c.drawRightString(W - 30*mm, y, "Total HT :"); c.drawRightString(W - left, y, f"{total_ht_calc:,.2f}")
    y -= 6*mm; c.drawRightString(W - 30*mm, y, f"TVA {int(get_tva_rate()*100)}% :"); c.drawRightString(W - left, y, f"{tva:,.2f}")
    y -= 6*mm; c.drawRightString(W - 30*mm, y, "Total TTC :"); c.drawRightString(W - left, y, f"{ttc:,.2f}")
    y -= 12*mm; c.setFont("Helvetica", 8); c.drawString(left, y, "Merci pour votre confiance.")
    c.showPage(); c.save()

    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"facture_{vente_id}.pdf", mimetype="application/pdf")

# app.py — Partie 4 : Fournisseurs & Achats
# -------- Fournisseurs --------
@app.route("/fournisseurs")
def fournisseurs_list():
    q = request.args.get("q", "").strip()
    conn = get_db()
    sql = "SELECT * FROM fournisseurs WHERE 1=1"
    params = []
    if q:
        sql += " AND (nom LIKE ? OR contact LIKE ? OR telephone LIKE ? OR email LIKE ? OR ville LIKE ?)"
        params += [like(q), like(q), like(q), like(q), like(q)]
    sql += " ORDER BY id DESC"
    page, per_page, offset = get_page_args()
    count = conn.execute("SELECT COUNT(*) AS n FROM ("+sql+")", params).fetchone()["n"]
    fournisseurs = conn.execute(sql + " LIMIT ? OFFSET ?", params+[per_page, offset]).fetchall()
    return render_template("fournisseurs.html", fournisseurs=fournisseurs, q=q,
                           page=page, per_page=per_page, total=count)

@app.route("/fournisseurs/ajouter", methods=["POST"])
@login_required
def fournisseurs_ajouter():
    nom = (request.form.get("nom") or "").strip()
    if not nom:
        flash("Le nom du fournisseur est obligatoire.", "danger")
        return redirect(url_for("fournisseurs_list"))
    contact = (request.form.get("contact") or "").strip()
    telephone = (request.form.get("telephone") or "").strip()
    email = (request.form.get("email") or "").strip()
    adresse = (request.form.get("adresse") or "").strip()
    ville = (request.form.get("ville") or "").strip()

    conn = get_db()
    conn.execute("""
        INSERT INTO fournisseurs (nom, contact, telephone, email, adresse, ville)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (nom, contact, telephone, email, adresse, ville))
    conn.commit()
    flash("Fournisseur ajouté.", "success")
    return redirect(url_for("fournisseurs_list"))

@app.route("/fournisseurs/<int:fid>/supprimer", methods=["POST"])
@role_required('owner','admin')
def fournisseurs_supprimer(fid):
    conn = get_db()
    conn.execute("DELETE FROM fournisseurs WHERE id = ?", (fid,))
    conn.commit()
    flash("Fournisseur supprimé.", "success")
    return redirect(url_for("fournisseurs_list"))

# -------- Achats (owner/admin uniquement) --------
@app.route("/achats")
@role_required('owner','admin')
def achats_list():
    q = request.args.get("q", "").strip()
    d1 = parse_date(request.args.get("date_from"))
    d2 = parse_date(request.args.get("date_to"))

    conn = get_db()
    fournisseurs = conn.execute("SELECT id, nom FROM fournisseurs ORDER BY nom").fetchall()
    produits = conn.execute("SELECT id, nom, prix_achat, quantite FROM produits ORDER BY nom").fetchall()

    sql = """
    SELECT a.id, a.date, a.total_ht, a.tva, a.total_ttc, a.mode_paiement, a.statut,
           f.nom AS fournisseur_nom
    FROM achats a
    LEFT JOIN fournisseurs f ON f.id = a.fournisseur_id
    WHERE 1=1
    """
    params = []
    if q:
        sql += " AND (f.nom LIKE ? OR a.mode_paiement LIKE ?)"
        params += [like(q), like(q)]
    if d1:
        sql += " AND date(a.date) >= date(?)"; params.append(d1)
    if d2:
        sql += " AND date(a.date) <= date(?)"; params.append(d2)
    sql += " ORDER BY a.id DESC"

    page, per_page, offset = get_page_args()
    count = conn.execute("SELECT COUNT(*) AS n FROM ("+sql+")", params).fetchone()["n"]
    achats = conn.execute(sql + " LIMIT ? OFFSET ?", params+[per_page, offset]).fetchall()
    return render_template("achats.html", achats=achats, fournisseurs=fournisseurs, produits=produits,
                           q=q, date_from=d1 or "", date_to=d2 or "",
                           page=page, per_page=per_page, total=count)

@app.route("/achats/ajouter", methods=["POST"])
@role_required('owner','admin')
def achats_ajouter():
    conn = get_db()
    fournisseur_id = request.form.get("fournisseur_id")
    mode_paiement = (request.form.get("mode_paiement") or "").strip()

    produit_ids = request.form.getlist("produit_id[]")
    qtes = request.form.getlist("qte[]")
    pus = request.form.getlist("pu[]")

    lignes = []
    for i in range(len(produit_ids)):
        pid = parse_int(produit_ids[i], 0)
        qte = parse_int(qtes[i], 0)
        pu = parse_float(pus[i], 0)
        if pid > 0 and qte > 0 and pu >= 0:
            lignes.append({"produit_id": pid, "qte": qte, "pu": pu})

    if not lignes:
        flash("Ajoute au moins une ligne d'achat.", "danger")
        return redirect(url_for("achats_list"))

    total_ht = round(sum(lg["qte"] * lg["pu"] for lg in lignes), 2)
    tva = round(total_ht * get_tva_rate(), 2)
    total_ttc = round(total_ht + tva, 2)

    try:
        conn.isolation_level = None; conn.execute("BEGIN")
        cur = conn.execute("""
            INSERT INTO achats (fournisseur_id, total_ht, tva, total_ttc, mode_paiement, statut, date)
            VALUES (?, ?, ?, ?, ?, 'reçu', CURRENT_TIMESTAMP)
        """, (fournisseur_id if fournisseur_id else None, total_ht, tva, total_ttc, mode_paiement))
        achat_id = cur.lastrowid

        for lg in lignes:
            conn.execute("""
                INSERT INTO lignes_achat (achat_id, produit_id, quantite, prix_unitaire)
                VALUES (?, ?, ?, ?)
            """, (achat_id, lg["produit_id"], lg["qte"], lg["pu"]))
            conn.execute("UPDATE produits SET quantite = quantite + ? WHERE id = ?", (lg["qte"], lg["produit_id"]))
        conn.execute("COMMIT"); flash(f"Achat #{achat_id} enregistré (TTC {total_ttc}).", "success")
    except Exception as e:
        conn.execute("ROLLBACK"); flash(f"Erreur lors de l'enregistrement de l'achat: {e}", "danger")
    return redirect(url_for("achats_list"))

# app.py — Partie 5 : Paramètres, Dashboard, Exports, Admin, Utilisateurs
# -------- Paramètres société --------
@app.route("/parametres", methods=["GET","POST"])
@role_required('owner','admin')
def parametres():
    if request.method == "GET":
        return render_template("parametres.html", st=g.settings)
    societe = (request.form.get("societe") or "").strip()
    adresse = (request.form.get("adresse") or "").strip()
    telephone = (request.form.get("telephone") or "").strip()
    email = (request.form.get("email") or "").strip()
    try:
        tva_rate = float(request.form.get("tva_rate"))/100.0
    except:
        tva_rate = get_tva_rate()
    conn = get_db()
    conn.execute("""UPDATE settings SET societe=?, adresse=?, telephone=?, email=?, tva_rate=? WHERE id=1""",
                 (societe, adresse, telephone, email, tva_rate))
    conn.commit()
    flash("Paramètres enregistrés.", "success")
    return redirect(url_for("parametres"))

# -------- Dashboard (owner/admin) --------
@app.route("/dashboard")
@role_required('owner','admin')
def dashboard():
    conn = get_db()
    rows_ca = conn.execute("""
      SELECT strftime('%Y-%m', date) AS ym, SUM(total_ttc) AS ca
      FROM ventes
      WHERE statut!='annulé' AND date >= date('now','-7 months')
      GROUP BY ym ORDER BY ym
    """).fetchall()
    rows_top = conn.execute("""
      SELECT p.nom AS produit, SUM(lv.quantite) AS q
      FROM lignes_vente lv JOIN produits p ON p.id=lv.produit_id
      JOIN ventes v ON v.id=lv.vente_id
      WHERE v.statut!='annulé' AND v.date >= date('now','-6 months')
      GROUP BY p.id ORDER BY q DESC LIMIT 5
    """).fetchall()
    rows_low = conn.execute("""
      SELECT id, nom, reference, quantite FROM produits
      WHERE quantite <= 2 ORDER BY quantite ASC, nom LIMIT 10
    """).fetchall()
    labels = [r["ym"] for r in rows_ca]
    data_ca = [float(r["ca"] or 0) for r in rows_ca]
    top_labels = [r["produit"] for r in rows_top]
    top_values = [int(r["q"] or 0) for r in rows_top]
    return render_template("dashboard.html",
                           labels=labels, data_ca=data_ca,
                           top_labels=top_labels, top_values=top_values,
                           low_rows=rows_low)

# -------- Exports CSV (avec restrictions) --------
@app.route("/export/produits.csv")
def export_produits_csv():
    role = session.get('user',{}).get('role')
    conn = get_db()
    if role == 'vendeur':
        rows = conn.execute("""SELECT id, nom, reference, categorie, marque, prix_vente, quantite, garantie_mois
                               FROM produits ORDER BY nom""").fetchall()
    else:
        rows = conn.execute("""SELECT id, nom, reference, categorie, marque, prix_achat, prix_vente, quantite, garantie_mois
                               FROM produits ORDER BY nom""").fetchall()
    def generate():
        s = io.StringIO(); w = csv.writer(s)
        if role == 'vendeur':
            w.writerow(["id","nom","reference","categorie","marque","prix_vente","quantite","garantie_mois"])
        else:
            w.writerow(["id","nom","reference","categorie","marque","prix_achat","prix_vente","quantite","garantie_mois"])
        yield s.getvalue(); s.seek(0); s.truncate(0)
        for r in rows:
            if role == 'vendeur':
                w.writerow([r["id"], r["nom"], r["reference"], r["categorie"], r["marque"],
                            f"{r['prix_vente']:.2f}", r["quantite"], r["garantie_mois"]])
            else:
                w.writerow([r["id"], r["nom"], r["reference"], r["categorie"], r["marque"],
                            f"{r['prix_achat']:.2f}", f"{r['prix_vente']:.2f}", r["quantite"], r["garantie_mois"]])
            yield s.getvalue(); s.seek(0); s.truncate(0)
    return Response(generate(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=produits.csv"})

@app.route("/export/clients.csv")
def export_clients_csv():
    conn = get_db()
    rows = conn.execute("""SELECT id, nom, telephone, email, entreprise, adresse, ville
                           FROM clients ORDER BY nom""").fetchall()
    def generate():
        s = io.StringIO(); w = csv.writer(s)
        w.writerow(["id","nom","telephone","email","entreprise","adresse","ville"])
        yield s.getvalue(); s.seek(0); s.truncate(0)
        for r in rows:
            w.writerow([r["id"], r["nom"], r["telephone"], r["email"], r["entreprise"], r["adresse"], r["ville"]])
            yield s.getvalue(); s.seek(0); s.truncate(0)
    return Response(generate(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=clients.csv"})

@app.route("/export/ventes.csv")
def export_ventes_csv():
    q = (request.args.get("q") or "").strip()
    d1 = parse_date(request.args.get("date_from"))
    d2 = parse_date(request.args.get("date_to"))
    conn = get_db()
    sql = """
      SELECT v.id, v.date, IFNULL(c.nom,'') AS client, v.total_ht, v.tva, v.total_ttc, v.mode_paiement, v.statut
      FROM ventes v LEFT JOIN clients c ON c.id = v.client_id
      WHERE 1=1
    """
    params = []
    if q:
        sql += " AND (c.nom LIKE ? OR v.mode_paiement LIKE ?)"; params += [like(q), like(q)]
    if d1:
        sql += " AND date(v.date) >= date(?)"; params.append(d1)
    if d2:
        sql += " AND date(v.date) <= date(?)"; params.append(d2)
    sql += " ORDER BY v.id DESC"
    rows = conn.execute(sql, params).fetchall()
    def generate():
        s = io.StringIO(); w = csv.writer(s)
        w.writerow(["id","date","client","total_ht","tva","total_ttc","mode_paiement","statut"])
        yield s.getvalue(); s.seek(0); s.truncate(0)
        for r in rows:
            w.writerow([r["id"], r["date"], r["client"],
                        f"{r['total_ht']:.2f}", f"{r['tva']:.2f}", f"{r['total_ttc']:.2f}",
                        r["mode_paiement"] or "", r["statut"]])
            yield s.getvalue(); s.seek(0); s.truncate(0)
    return Response(generate(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=ventes.csv"})

@app.route("/export/achats.csv")
@role_required('owner','admin')
def export_achats_csv():
    q = (request.args.get("q") or "").strip()
    d1 = parse_date(request.args.get("date_from"))
    d2 = parse_date(request.args.get("date_to"))
    conn = get_db()
    sql = """
      SELECT a.id, a.date, IFNULL(f.nom,'') AS fournisseur, a.total_ht, a.tva, a.total_ttc, a.mode_paiement, a.statut
      FROM achats a LEFT JOIN fournisseurs f ON f.id = a.fournisseur_id
      WHERE 1=1
    """
    params = []
    if q:
        sql += " AND (f.nom LIKE ? OR a.mode_paiement LIKE ?)"; params += [like(q), like(q)]
    if d1:
        sql += " AND date(a.date) >= date(?)"; params.append(d1)
    if d2:
        sql += " AND date(a.date) <= date(?)"; params.append(d2)
    sql += " ORDER BY a.id DESC"
    rows = conn.execute(sql, params).fetchall()
    def generate():
        s = io.StringIO(); w = csv.writer(s)
        w.writerow(["id","date","fournisseur","total_ht","tva","total_ttc","mode_paiement","statut"])
        yield s.getvalue(); s.seek(0); s.truncate(0)
        for r in rows:
            w.writerow([r["id"], r["date"], r["fournisseur"],
                        f"{r['total_ht']:.2f}", f"{r['tva']:.2f}", f"{r['total_ttc']:.2f}",
                        r["mode_paiement"] or "", r["statut"]])
            yield s.getvalue(); s.seek(0); s.truncate(0)
    return Response(generate(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=achats.csv"})

# -------- Outils admin --------
@app.route("/admin/tools")
@role_required('owner','admin')
def admin_tools():
    return render_template("admin_tools.html")

@app.route("/admin/backup")
@role_required('owner','admin')
def admin_backup():
    if not os.path.exists(DB_PATH):
        abort(404)
    return send_file(DB_PATH, as_attachment=True, download_name="materiel.backup.db")

@app.route("/admin/restore", methods=["POST"])
@role_required('owner','admin')
def admin_restore():
    f = request.files.get("dbfile")
    if not f or f.filename == "":
        flash("Aucun fichier sélectionné.", "danger")
        return redirect(url_for("admin_tools"))
    tmp = tempfile.NamedTemporaryFile(delete=False)
    f.save(tmp.name)
    with open(tmp.name, "rb") as fh:
        head = fh.read(16)
    if not head.startswith(b"SQLite format 3"):
        os.unlink(tmp.name)
        flash("Fichier invalide (pas une base SQLite).", "danger")
        return redirect(url_for("admin_tools"))
    if os.path.exists(DB_PATH):
        os.replace(DB_PATH, DB_PATH + ".old")
    os.replace(tmp.name, DB_PATH)
    flash("Base restaurée. (Copie .old conservée)", "success")
    return redirect(url_for("index"))

@app.route("/admin/import_produits", methods=["POST"])
@role_required('owner','admin')
def admin_import_produits():
    f = request.files.get("file")
    mode = request.form.get("mode") or "upsert"
    if not f or f.filename == "":
        flash("Aucun fichier sélectionné.", "danger")
        return redirect(url_for("admin_tools"))
    try:
        if f.filename.lower().endswith(".xlsx"):
            df = pd.read_excel(f)
        else:
            df = pd.read_csv(f)
    except Exception as e:
        flash(f"Erreur lecture fichier: {e}", "danger")
        return redirect(url_for("admin_tools"))

    df.columns = [c.strip().lower() for c in df.columns]
    if "nom" not in df.columns:
        flash("Colonne obligatoire manquante: nom", "danger")
        return redirect(url_for("admin_tools"))
    df = df.fillna("")
    conn = get_db()
    inserted = updated = 0
    for _, row in df.iterrows():
        nom = str(row.get("nom","")).strip()
        reference = str(row.get("reference","")).strip() or None
        categorie = str(row.get("categorie","")).strip() or None
        marque = str(row.get("marque","")).strip() or None
        prix_achat = parse_float(row.get("prix_achat"), 0)
        prix_vente = parse_float(row.get("prix_vente"), 0)
        quantite = parse_int(row.get("quantite"), 0)
        numero_serie = str(row.get("numero_serie","")).strip() or None
        garantie_mois = parse_int(row.get("garantie_mois"), 0)

        if mode == "insert" or not reference:
            try:
                conn.execute("""INSERT INTO produits(nom,reference,categorie,marque,prix_achat,prix_vente,quantite,numero_serie,garantie_mois,updated_at)
                                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                             (nom, reference, categorie, marque, prix_achat, prix_vente, quantite, numero_serie, garantie_mois, datetime.utcnow().isoformat()))
                inserted += 1
            except sqlite3.IntegrityError:
                continue
        else:
            cur = conn.execute("SELECT id FROM produits WHERE reference = ?", (reference,))
            ex = cur.fetchone()
            if ex:
                conn.execute("""UPDATE produits
                                SET nom=?, categorie=?, marque=?, prix_achat=?, prix_vente=?, quantite=?, numero_serie=?, garantie_mois=?, updated_at=?
                                WHERE reference=?""",
                             (nom, categorie, marque, prix_achat, prix_vente, quantite, numero_serie, garantie_mois, datetime.utcnow().isoformat(), reference))
                updated += 1
            else:
                conn.execute("""INSERT INTO produits(nom,reference,categorie,marque,prix_achat,prix_vente,quantite,numero_serie,garantie_mois,updated_at)
                                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                             (nom, reference, categorie, marque, prix_achat, prix_vente, quantite, numero_serie, garantie_mois, datetime.utcnow().isoformat()))
                inserted += 1
    conn.commit()
    flash(f"Import terminé. {inserted} ajout(s), {updated} mise(s) à jour.", "success")
    return redirect(url_for("index"))

# -------- Gestion utilisateurs --------
@app.route("/users")
@role_required('owner','admin')
def users_list():
    conn = get_db()
    users = conn.execute("SELECT id, username, role FROM users ORDER BY role DESC, username").fetchall()
    return render_template("users.html", users=users)

@app.route("/users/new", methods=["GET","POST"])
@role_required('owner','admin')
def users_new():
    if request.method == "GET":
        return render_template("users_edit.html", user=None)
    username = (request.form.get("username") or "").strip()
    role = (request.form.get("role") or "vendeur").strip()
    pwd1 = request.form.get("password") or ""
    pwd2 = request.form.get("password2") or ""
    if not username or len(pwd1) < 6 or pwd1 != pwd2:
        flash("Utilisateur + mot de passe (min 6) + confirmation identique requis.", "danger")
        return redirect(url_for("users_new"))
    if role not in ("owner","admin","vendeur"):
        role = "vendeur"
    conn = get_db()
    try:
        conn.execute("INSERT INTO users(username, pwd_hash, role) VALUES (?,?,?)",
                     (username, pwd_hasher.hash(pwd1), role))
        conn.commit()
        flash("Utilisateur créé.", "success")
    except sqlite3.IntegrityError:
        flash("Nom d’utilisateur déjà pris.", "danger")
    return redirect(url_for("users_list"))

@app.route("/users/<int:uid>/edit", methods=["GET","POST"])
@role_required('owner','admin')
def users_edit(uid):
    conn = get_db()
    if request.method == "GET":
        u = conn.execute("SELECT id, username, role FROM users WHERE id=?", (uid,)).fetchone()
        if not u: abort(404)
        return render_template("users_edit.html", user=u)

    role = (request.form.get("role") or "vendeur").strip()
    if role not in ("owner","admin","vendeur"): role = "vendeur"
    current = conn.execute("SELECT id, username, role FROM users WHERE id=?", (uid,)).fetchone()
    if not current:
        abort(404)
    if current["role"] == "owner" and not has_role('owner'):
        flash("Seul le propriétaire peut modifier un compte owner.", "danger")
        return redirect(url_for("users_list"))

    conn.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    conn.commit()
    flash("Utilisateur mis à jour.", "success")
    return redirect(url_for("users_list"))

@app.route("/users/<int:uid>/reset_pw", methods=["POST"])
@role_required('owner','admin')
def users_reset_pw(uid):
    conn = get_db()
    cur = conn.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
    if not cur: abort(404)
    if cur["role"] == "owner" and not has_role('owner'):
        flash("Seul le propriétaire peut réinitialiser ce mot de passe.", "danger")
        return redirect(url_for("users_list"))
    new_pw = "changeme123"
    conn.execute("UPDATE users SET pwd_hash=? WHERE id=?", (pwd_hasher.hash(new_pw), uid))
    conn.commit()
    flash("Mot de passe réinitialisé à: changeme123 (à changer après connexion).", "warning")
    return redirect(url_for("users_list"))

@app.route("/users/<int:uid>/delete", methods=["POST"])
@role_required('owner','admin')
def users_delete(uid):
    me = session.get('user',{}).get('id')
    if uid == me:
        flash("Impossible de supprimer ton propre compte.", "danger")
        return redirect(url_for("users_list"))
    conn = get_db()
    cur = conn.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
    if not cur: abort(404)
    if cur["role"] == "owner" and not has_role('owner'):
        flash("Seul le propriétaire peut supprimer un owner.", "danger")
        return redirect(url_for("users_list"))
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()
    flash("Utilisateur supprimé.", "success")
    return redirect(url_for("users_list"))

# -------- Run --------
if __name__ == "__main__":
    ensure_schema()
    app.run(debug=True)
