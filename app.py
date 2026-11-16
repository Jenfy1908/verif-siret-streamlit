





import streamlit as st
import pandas as pd
import requests
import time
from io import BytesIO
from openpyxl import load_workbook
from openpyxl.styles import PatternFill

# ----------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------
API_KEY = st.secrets["API_KEY"]
HEADERS = {"X-INSEE-Api-Key-Integration": API_KEY}
API_URL = "https://api.insee.fr/api-sirene/3.11/siret/"

st.set_page_config(page_title="Outils SIRET – INSEE", page_icon="🏢")
st.title("🏢 Outils SIRET – API INSEE")

# ----------------------------------------------------------
# HELPERS
# ----------------------------------------------------------
def normalize_siret(s):
    return "".join(c for c in str(s) if c.isdigit())

def statut_from_etat(etat):
    if etat == "A":
        return "Actif"
    if etat == "F":
        return "Fermé"
    return f"Inconnu ({etat})"

def fill_for_statut(statut):
    s = (statut or "").lower()
    if "actif" in s:
        return PatternFill("solid", fgColor="C6EFCE")
    if "fermé" in s or "ferme" in s:
        return PatternFill("solid", fgColor="FFC7CE")
    return PatternFill("solid", fgColor="FFEB9C")

def get_raison_sociale(unite):
    if not unite:
        return ""
    return (
        unite.get("denominationUniteLegale")
        or f"{unite.get('prenomUsuelUniteLegale','')} {unite.get('nomUniteLegale','')}".strip()
    )

onglet_verification, onglet_recherche = st.tabs(["✅ Vérification SIRET", "🔎 Recherche d’établissements"])

with onglet_verification:
    # ----------------------------------------------------------
    # UPLOAD CSV
    # ----------------------------------------------------------
    uploaded_file = st.file_uploader("📂 Importer un CSV avec une colonne 'siret'", type=["csv"])

    if uploaded_file:
        df_in = pd.read_csv(uploaded_file, dtype=str)

        if "siret" not in df_in.columns:
            st.error("❌ Le fichier doit contenir une colonne 'siret'")
            st.stop()

        sirets = df_in["siret"].dropna().tolist()
        st.success(f"✅ {len(sirets)} SIRET détectés")

        if st.button("🚀 Lancer la vérification"):
            results = []
            progress = st.progress(0)

            for i, s in enumerate(sirets, start=1):
                siret = normalize_siret(s)
                url = f"{API_URL}{siret}"

                while True:
                    r = requests.get(url, headers=HEADERS)

                    if r.status_code == 200:
                        etab = r.json().get("etablissement", {})
                        etat = (
                            etab.get("periodesEtablissement", [{}])[0]
                            .get("etatAdministratifEtablissement", "INCONNU")
                        )

                        statut = statut_from_etat(etat)

                        raison_sociale = get_raison_sociale(etab.get("uniteLegale"))
                        type_etab = (
                            "Siège social"
                            if etab.get("etablissementSiege")
                            else "Établissement secondaire"
                        )

                    elif r.status_code == 404:
                        statut = "Inexistant"
                        raison_sociale = ""
                        type_etab = ""




