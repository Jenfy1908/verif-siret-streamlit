import streamlit as st
import pandas as pd
import requests
import time
import html as pyhtml

st.error("TEST COULEUR : SI TU VOIS CE MESSAGE, STREAMLIT UTILISE BIEN CETTE VERSION")

# ----------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------
API_KEY = st.secrets["API_KEY"]
HEADERS = {"X-INSEE-Api-Key-Integration": API_KEY}
API_URL = "https://api.insee.fr/api-sirene/3.11/siret/"

st.set_page_config(page_title="Vérification SIRET", page_icon="🏢")

st.title("🏢 Vérificateur SIRET - API INSEE")
st.write("Importez un fichier CSV contenant une colonne **siret** (14 chiffres).")

# ----------------------------------------------------------
# Helpers
# ----------------------------------------------------------
def normalize_siret(s):
    return "".join(ch for ch in str(s) if ch.isdigit())

def render_table(df):
    css = """
    <style>
      table { width:100%; border-collapse: collapse; font-family: Arial, sans-serif; }
      th, td { border:1px solid #ddd; padding:8px; }
      th { background:#f4f4f4; text-align:left; }
      .ok { background:#c6efce; color:#006100; font-weight:bold; }
      .bad { background:#ffc7ce; color:#9c0006; font-weight:bold; }
      .warn { background:#ffeb9c; color:#9c5700; font-weight:bold; }
      .mono { font-family: Consolas, monospace; }
    </style>
    """

    rows = []
    for _, r in df.iterrows():
        statut = r["Statut"].lower()
        if "actif" in statut:
            cls = "ok"
        elif "fermé" in statut:
            cls = "bad"
        else:
            cls = "warn"

        rows.append(
            f"<tr>"
            f"<td class='mono'>{pyhtml.escape(r['SIRET'])}</td>"
            f"<td class='{cls}'>{pyhtml.escape(r['Statut'])}</td>"
            f"</tr>"
        )

    html = css + (
        "<table>"
        "<thead><tr><th>SIRET</th><th>Statut</th></tr></thead>"
        "<tbody>"
        + "".join(rows) +
        "</tbody></table>"
    )

    st.markdown(html, unsafe_allow_html=True)

# ----------------------------------------------------------
# UPLOAD CSV
# ----------------------------------------------------------
uploaded_file = st.file_uploader("📂 Importer fichier CSV", type=["csv"])

if uploaded_file:
    df_in = pd.read_csv(uploaded_file, dtype=str)

    if "siret" not in df_in.columns:
        st.error("❌ Le fichier doit contenir une colonne 'siret'.")
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
                    etat = (
                        r.json()
                        .get("etablissement", {})
                        .get("periodesEtablissement", [{}])[0]
                        .get("etatAdministratifEtablissement", "INCONNU")
                    )
                    statut = "Actif" if etat == "A" else "Fermé" if etat == "F" else f"Inconnu ({etat})"

                elif r.status_code == 404:
                    statut = "Inexistant"

                elif r.status_code == 429:
                    time.sleep(15)
                    continue

                else:
                    statut = f"Erreur ({r.status_code})"

                results.append({"SIRET": siret, "Statut": statut})
                progress.progress(i / len(sirets))
                break

            time.sleep(0.3)

        df_res = pd.DataFrame(results)
        st.success("✅ Vérification terminée")

        st.subheader("📊 Résultats")
        render_table(df_res)

        st.download_button(
            "📥 Télécharger les résultats (CSV)",
            df_res.to_csv(index=False, sep=";").encode("utf-8"),
            "resultats_siret.csv",
            "text/csv",
        )

else:
    st.info("🕮 Chargez un fichier CSV pour commencer.")
