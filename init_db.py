# init_db.py
import sqlite3

DB_PATH = "materiel.db"

SCHEMA_SQL = """
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
    contact TEXT,
    telephone TEXT,
    email TEXT,
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
    mode_paiement TEXT,          -- espèces, mobile money, virement, etc.
    acompte REAL DEFAULT 0,
    statut TEXT DEFAULT 'brouillon',   -- brouillon, payé, partiel, annulé
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

-- Index utiles
CREATE INDEX IF NOT EXISTS idx_produits_reference ON produits(reference);
CREATE INDEX IF NOT EXISTS idx_lignes_vente_vente ON lignes_vente(vente_id);
CREATE INDEX IF NOT EXISTS idx_lignes_vente_produit ON lignes_vente(produit_id);
"""

def init_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        print("✅ Base initialisée avec succès.")
    finally:
        conn.close()

if __name__ == "__main__":
    init_db()
