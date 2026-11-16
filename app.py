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

                    elif r.status_code == 429:
                        time.sleep(15)
                        continue

                    else:
                        statut = f"Erreur ({r.status_code})"
                        raison_sociale = ""
                        type_etab = ""

                    results.append(
                        {
                            "SIRET": siret,
                            "Raison sociale": raison_sociale,
                            "Type établissement": type_etab,
                            "Statut": statut,
                        }
                    )

                    progress.progress(i / len(sirets))
                    break

                time.sleep(0.3)

            df_res = pd.DataFrame(results)
            st.success("✅ Vérification terminée")

            # ----------------------------------------------------------
            # EXPORT EXCEL STYLÉ (ligne complète colorée)
            # ----------------------------------------------------------
            output = BytesIO()
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                df_res.to_excel(writer, index=False, sheet_name="Résultats")

            output.seek(0)
            wb = load_workbook(output)
            ws = wb["Résultats"]

            for row in range(2, ws.max_row + 1):
                statut_value = ws[f"D{row}"].value
                fill = fill_for_statut(statut_value)

                for col in ["A", "B", "C", "D"]:
                    ws[f"{col}{row}"].fill = fill

            final_output = BytesIO()
            wb.save(final_output)
            final_output.seek(0)

            st.download_button(
                "📥 Télécharger les résultats (Excel)",
                final_output,
                "resultats_siret.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

    else:
        st.info("🕮 Chargez un fichier CSV pour commencer.")

with onglet_recherche:

    # ----------------------------------------------------------
    # RECHERCHE SIRENE : BORDEAUX MÉTROPOLE
    # ----------------------------------------------------------
    from openpyxl.utils import get_column_letter
    from openpyxl.styles import Font, Alignment

    GEO_API = "https://geo.api.gouv.fr/epcis/243300316/communes"
    TRANCHES = {
        "11": "10 à 19", "12": "20 à 49", "21": "50 à 99",
        "22": "100 à 199", "31": "200 à 249", "32": "250 à 499",
        "41": "500 à 999", "42": "1 000 à 1 999", "51": "2 000 à 4 999",
        "52": "5 000 à 9 999", "53": "10 000 et plus",
    }

    @st.cache_data(ttl=86400)
    def communes_metropole():
        reponse = requests.get(GEO_API, params={"fields": "nom,code", "format": "json"}, timeout=30)
        reponse.raise_for_status()
        communes = sorted(reponse.json(), key=lambda c: c["nom"])
        if len(communes) != 28:
            raise ValueError(f"Le référentiel géographique renvoie {len(communes)} communes au lieu de 28.")
        return communes

    def periode_courante(etab):
        periodes = etab.get("periodesEtablissement") or []
        # L'API fournit normalement les périodes de la plus récente à la plus ancienne.
        return next((p for p in periodes if p.get("dateFin") is None), periodes[0] if periodes else {})

    def requete_insee(params):
        for tentative in range(5):
            try:
                r = requests.get(API_URL, headers=HEADERS, params=params, timeout=60)
            except requests.RequestException as exc:
                if tentative == 4:
                    raise RuntimeError(f"Connexion INSEE impossible : {exc}") from exc
                time.sleep(2 ** tentative)
                continue
            if r.status_code in (429, 500, 502, 503, 504) and tentative < 4:
                attente = r.headers.get("Retry-After", "")
                time.sleep(min(int(attente), 60) if attente.isdigit() else 2 ** (tentative + 1))
                continue
            if r.status_code != 200:
                raise RuntimeError(f"Erreur API INSEE {r.status_code} : {r.text[:350]}")
            return r.json()
        raise RuntimeError("L'API INSEE ne répond pas après plusieurs tentatives.")

    def extraire_etablissements(naf, communes, avancement):
        codes = [c["code"] for c in communes]
        # Requêtes par commune : pagination curseur pour ne pas perdre de résultats.
        trouves, inconnus = [], []
        for index, commune in enumerate(communes):
            q = (f"activitePrincipaleEtablissement:{naf} AND "
                 f"codeCommuneEtablissement:{commune['code']} AND "
                 "etatAdministratifEtablissement:A AND caractereEmployeurEtablissement:O")
            curseur = "*"
            curseurs_vus = set()
            while curseur:
                if curseur in curseurs_vus:
                    raise RuntimeError("Pagination INSEE bloquée : curseur répété.")
                curseurs_vus.add(curseur)
                data = requete_insee({"q": q, "nombre": 1000, "curseur": curseur})
                for etab in data.get("etablissements", []):
                    periode = periode_courante(etab)
                    if (periode.get("etatAdministratifEtablissement") != "A" or
                        periode.get("caractereEmployeurEtablissement") != "O" or
                        periode.get("activitePrincipaleEtablissement") != naf):
                        continue
                    tranche = etab.get("trancheEffectifsEtablissement")
                    # Ne pas confondre effectif inconnu et effectif inférieur à 10.
                    if tranche not in TRANCHES and tranche not in (None, "", "NN"):
                        continue
                    adresse = etab.get("adresseEtablissement") or {}
                    unite = etab.get("uniteLegale") or {}
                    ligne = {
                        "SIRET": etab.get("siret", ""),
                        "SIREN": etab.get("siren", ""),
                        "Raison sociale": get_raison_sociale(unite),
                        "Adresse": " ".join(str(x) for x in [adresse.get("numeroVoieEtablissement"), adresse.get("typeVoieEtablissement"), adresse.get("libelleVoieEtablissement")] if x),
                        "Code postal": adresse.get("codePostalEtablissement", ""),
                        "Commune": commune["nom"],
                        "NAF": naf,
                        "Tranche effectif": TRANCHES.get(tranche, "Inconnu"),
                        "Code tranche": tranche or "",
                        "Année effectif": etab.get("anneeEffectifsEtablissement", ""),
                        "Employeur": "Oui",
                        "Date création": etab.get("dateCreationEtablissement", ""),
                        "Siège social": "Oui" if etab.get("etablissementSiege") else "Non",
                    }
                    (trouves if tranche in TRANCHES else inconnus).append(ligne)
                suivant = (data.get("header") or {}).get("curseurSuivant")
                if not suivant or suivant == curseur:
                    break
                curseur = suivant
            avancement.progress((index + 1) / len(communes), text=f"Communes traitées : {index + 1}/{len(communes)}")
        return trouves, inconnus

    def exporter_recherche(valides, inconnus, communes, naf):
        colonnes = ["SIRET", "SIREN", "Raison sociale", "Adresse", "Code postal", "Commune", "NAF", "Tranche effectif", "Code tranche", "Année effectif", "Employeur", "Date création", "Siège social"]
        parametres = pd.DataFrame([
            {"Paramètre": "Code NAF", "Valeur": naf},
            {"Paramètre": "Statut", "Valeur": "Actif"},
            {"Paramètre": "Caractère employeur", "Valeur": "Oui"},
            {"Paramètre": "Effectifs", "Valeur": "Tranche 10–19 et supérieures"},
            {"Paramètre": "Zone", "Valeur": "Bordeaux Métropole (28 communes)"},
            {"Paramètre": "Attention", "Valeur": "10–19 inclut les établissements ayant exactement 10 salariés"},
            {"Paramètre": "Effectifs inconnus", "Valeur": "Onglet séparé ; effectif non disponible"},
        ])
        liste_communes = pd.DataFrame([{"Commune": c["nom"], "Code INSEE": c["code"]} for c in communes])
        buffer = BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            pd.DataFrame(valides, columns=colonnes).to_excel(writer, index=False, sheet_name="Établissements 10+")
            pd.DataFrame(inconnus, columns=colonnes).to_excel(writer, index=False, sheet_name="Effectifs inconnus")
            parametres.to_excel(writer, index=False, sheet_name="Paramètres")
            liste_communes.to_excel(writer, index=False, sheet_name="Communes")
            for ws in writer.book.worksheets:
                ws.freeze_panes = "A2"
                ws.auto_filter.ref = ws.dimensions
                for cell in ws[1]:
                    cell.fill = PatternFill("solid", fgColor="17365D")
                    cell.font = Font(bold=True, color="FFFFFF")
                for col in ws.columns:
                    letter = get_column_letter(col[0].column)
                    largeur = min(55, max(13, max(len(str(c.value or "")) for c in col) + 2))
                    ws.column_dimensions[letter].width = largeur
                if ws.title in ("Établissements 10+", "Effectifs inconnus"):
                    for row in ws.iter_rows(min_row=2):
                        row[0].number_format = "@"
                        row[1].number_format = "@"
        return buffer.getvalue()

    st.subheader("🔎 Recherche d'établissements – Bordeaux Métropole")
    st.caption("Source : API Sirene INSEE ; liste des communes issue de l'API Géo officielle.")
    naf = st.text_input("Code NAF / APE", value="69.20Z", key="naf_recherche").strip().upper()
    st.write("**Zone :** Bordeaux Métropole (28 communes)")
    st.write("**Statut :** actif uniquement | **Employeur :** oui uniquement")
    st.write("**Effectifs :** tranche 10–19 salariés et tranches supérieures")
    st.info("La tranche 10–19 inclut les établissements de 10 salariés exactement. Les effectifs non renseignés seront exportés à part.")
    if st.button("🚀 Rechercher les établissements", key="lancer_recherche"):
        import re
        if not re.fullmatch(r"\d{2}\.\d{2}[A-Z]", naf):
            st.error("Saisir un code NAF au format 69.20Z.")
        else:
            try:
                with st.spinner("Chargement des communes et interrogation de l'INSEE..."):
                    communes = communes_metropole()
                    barre = st.progress(0)
                    valides, inconnus = extraire_etablissements(naf, communes, barre)
                    fichier = exporter_recherche(valides, inconnus, communes, naf)
                    st.session_state["recherche_insee"] = (valides, inconnus, fichier)
            except Exception as exc:
                st.error(f"Recherche interrompue : {exc}")
    if "recherche_insee" in st.session_state:
        valides, inconnus, fichier = st.session_state["recherche_insee"]
        st.success(f"{len(valides)} établissements avec tranche 10+ ; {len(inconnus)} avec effectif inconnu.")
        st.dataframe(pd.DataFrame(valides), use_container_width=True)
        st.download_button("📥 Télécharger l'extraction Excel", data=fichier,
                           file_name="extraction_insee_bordeaux_metropole.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
