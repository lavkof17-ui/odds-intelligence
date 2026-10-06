# Odds Intelligence — finale

1. Ajouter `ODDS_API_KEY` dans Streamlit Secrets.
2. Lancer `streamlit run streamlit_app.py` ou déployer le dossier.
3. Choisir librement cote min/max, fenêtre et probabilité minimale.
4. Le scanner récupère les fixtures puis les cotes par tournois afin d'éviter un appel OddsPapi par match.
5. Les options 1xBet sont conservées dynamiquement. Le modèle ne donne une probabilité que lorsqu'il sait réellement calculer le marché.

La source historique du modèle est football-data.co.uk, chargée et mise en cache localement par Streamlit. Le modèle V1 est un Poisson indépendant basé sur résultats récents, séparations domicile/extérieur et shrinkage.
