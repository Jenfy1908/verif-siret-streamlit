import streamlit as st
import pandas as pd
import requests
import time

# ✅ TEST INFAILLIBLE : si tu vois ce message sur Streamlit, c'est bien le bon code qui tourne
st.write("### ✅ VERSION NOUVELLE - 06/01/2026")

# ----------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------
API_KEY = st.secrets["API_KEY"]  # Clé API INSEE (Streamlit Secrets)
HEADERS = {"X-INSEE-Api-Key-Integration": API_KEY}
API_URL = "https://api.insee.fr/api-sirene/3.11/siret/"

st.set_page_config(page_title="Vérification SIRET", page_icon="🏢")

st.title("🏢 Vérificateur SIRET - API INSEE")
st.write("Importez un fichier CSV contenant une colonne **siret**, puis lancez la vérification.")

# ----------------------------------------------------------
# UPLOAD CSV
# ----------------------------------------------------------
uploaded_file = st.file_uploader("📂 Importer fichier CSV", type=["csv"])

if uploaded_file:
    df = pd.read_csv(uploaded_file, dtype=str)

    if "siret" not in df.columns:
        st.error("❌ Le fichier doit contenir une colonne 'siret'.")
    else:
        siret_list = df["siret"].dropna().astype(str).tolist()
        st.success(f"✅ {len(siret_list)} SIRET détectés")

        if st.button("🚀 Lancer la vérification"):
            results = []
            progress = st.progress(0)
            status = st.empty()

            for i, siret in enumerate(siret_list, start=1):
                url = f"{API_URL}{siret.strip()}"

                while True:
                    r = requests.get(url, headers=HEADERS)
                    code = r.status_code

                    if code == 200:
                        data = r.json()
                        etat = (
                            data.get("etablissement", {})
                            .get("periodesEtablissement", [{}])[0]
                            .get("etatAdministratifEtablissement", "INCONNU")
                        )

                        if etat == "A":
                            statut = "Actif"
                        elif etat == "F":
                            statut = "Fermé"
                        else:
                            statut = f"Inconnu ({etat})"

                    elif code == 404:
                        statut = "Inexistant"

                    elif code == 429:
                        status.warning("⚠️ Limite API atteinte — pause 15s…")
                        time.sleep(15)
                        continue

                    else:
                        statut = f"Erreur ({code})"

                    results.append({"SIRET": siret, "Statut": statut})
                    progress.progress(i / len(siret_list))
                    status.text(f"{i}/{len(siret_list)} : {siret} → {statut}")
                    break

                time.sleep(0.3)

            # ----------------------------------------------------------
            # RÉSULTATS
            # ----------------------------------------------------------
            df_res = pd.DataFrame(results)
            st.success("✅ Vérification terminée")

            # Formatage SIRET lisible
            def format_siret(s):
                s = str(s).strip()
                if len(s) == 14 and s.isdigit():
                    return f"{s[:3]} {s[3:6]} {s[6:9]} {s[9:14]}"
                return s

            df_res["SIRET"] = df_res["SIRET"].apply(format_siret)

            # Style couleur selon statut
            def style_statut(val):
                v = str(val).lower()
                if "actif" in v:
                    return "background-color: #c6efce; color: #006100; font-weight: bold;"
                if "fermé" in v:
                    return "background-color: #ffc7ce; color: #9c0006; font-weight: bold;"
                if "inconnu" in v or "inexistant" in v or "erreur" in v:
                    return "background-color: #ffeb9c; color: #9c5700; font-weight: bold;"
                return ""

            styled_df = df_res.style.applymap(style_statut, subset=["Statut"])

            st.subheader("📊 Résultats")
            st.dataframe(styled_df, use_container_width=True)

            # ----------------------------------------------------------
            # TÉLÉCHARGEMENT
            # ----------------------------------------------------------
            st.download_button(
                "📥 Télécharger les résultats (CSV)",
                df_res.to_csv(index=False, sep=";").encode("utf-8"),
                "resultats_siret.csv",
                "text/csv",
            )

else:
    st.info("🕮 Chargez un fichier CSV pour commencer.")
