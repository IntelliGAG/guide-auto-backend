import os
import math
import json
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client_openai = OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class LocationRequest(BaseModel):
    latitude: float
    longitude: float
    heading: float = -1.0

class QuestionRequest(BaseModel):
    latitude: float
    longitude: float
    heading: float = -1.0
    question: str

CATEGORIES_BASE = [
    "Histoire globale et faits marquants du département",
    "Patrimoine bâti et monuments du secteur",
    "Spécialités culinaires, vins et terroir départemental",
    "Espaces naturels, reliefs, fleuves et géographie"
]

session_state = {
    "last_lat": None,
    "last_lon": None,
    "current_commune": None,
    "current_departement": None,
    "stories_history": [],
    "queue_categories": list(CATEGORIES_BASE)
}

quiz_state = {
    "current_answer": "",
    "quiz_history": []
}

def extraction_infos_geo(addr):
    """Extrait la commune et le département depuis OSM."""
    commune = (addr.get('village') or addr.get('town') or addr.get('city') 
               or addr.get('municipality') or addr.get('suburb') or addr.get('hamlet'))
    
    departement = addr.get('county') or addr.get('state_district') or addr.get('state')
    
    return commune, departement

def obtenir_infos_locales(lat, lon):
    headers = {'User-Agent': 'GuideAutoApp/1.0 (contact@example.com)'}
    commune = None
    departement = None
    
    # Appel OSM
    url_osm = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=13"
    try:
        res = requests.get(url_osm, headers=headers, timeout=3).json()
        addr = res.get('address', {})
        commune, departement = extraction_infos_geo(addr)
    except Exception as e:
        print(f"Erreur OSM: {e}")

    # Secours si commune non trouvée
    if not commune:
        try:
            url_osm_w = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=10"
            res_w = requests.get(url_osm_w, headers=headers, timeout=3).json()
            addr_w = res_w.get('address', {})
            commune, departement = extraction_infos_geo(addr_w)
        except Exception as e:
            print(f"Erreur OSM Zoom 10: {e}")

    if not commune:
        commune = "votre secteur"
    if not departement:
        departement = "votre département"

    # Wikipédia sur le département pour donner du fond culturel à l'IA
    wiki_dept = ""
    try:
        url_wiki = f"https://fr.wikipedia.org/api/rest_v1/page/summary/{departement}"
        res_w = requests.get(url_wiki, headers=headers, timeout=3)
        if res_w.status_code == 200:
            wiki_dept = res_w.json().get('extract', '')
    except Exception as e:
        print(f"Erreur Wiki Dept: {e}")

    return commune, departement, wiki_dept

@app.post("/reset")
async def reset_session():
    global session_state, quiz_state
    session_state = {
        "last_lat": None,
        "last_lon": None,
        "current_commune": None,
        "current_departement": None,
        "stories_history": [],
        "queue_categories": list(CATEGORIES_BASE)
    }
    quiz_state = {
        "current_answer": "",
        "quiz_history": []
    }
    return {"message": "Réinitialisation réussie"}

@app.post("/get_story")
async def generate_story(req: LocationRequest, request: Request):
    global session_state
    try:
        lat, lon = req.latitude, req.longitude
        commune, departement, wiki_dept = obtenir_infos_locales(lat, lon)

        # Gestion des consignes d'introduction et de transition
        consigne_position = ""
        if session_state["current_commune"] is None:
            # Première intervention
            consigne_position = f"C'est ta TOUTE PREMIÈRE intervention. Indique obligatoirement à l'usager dans quelle commune ({commune}) et quel département ({departement}) il se trouve pour le situer."
        elif session_state["current_commune"] != commune:
            # Changement de commune
            ancienne = session_state["current_commune"]
            consigne_position = f"L'utilisateur vient de changer de ville ! Utilise IMPÉRATIVEMENT une phrase du style : 'Nous venons de quitter {ancienne} et entrons dans la commune de {commune}'."
            session_state["queue_categories"] = list(CATEGORIES_BASE)

        session_state["current_commune"] = commune
        session_state["current_departement"] = departement

        # Gestion de la boucle des thèmes
        if not session_state["queue_categories"]:
            session_state["queue_categories"] = list(CATEGORIES_BASE)
            
        categorie_cible = session_state["queue_categories"].pop(0)
        historique_texte = "\n".join([f"- {h}" for h in session_state["stories_history"]]) if session_state["stories_history"] else "Aucune."

        system_instruction = f"""
Tu es un guide touristique vocal de voiture, passionnant, cultivé et captivant.

CONSIGNES DE NARRATION :
1. LOCALISATION : {consigne_position if consigne_position else f"Nous sommes toujours dans la commune de {commune} ({departement})."}
2. PÉRIMÈTRE THÉMATIQUE : Tu parles du DÉPARTEMENT ({departement}) et de la commune ({commune}). Tu peux aborder les monuments, faits historiques, spécialités culinaires/viticoles, zones naturelles ou toute anecdote culturelle captivante sur ce département.
3. RÈGLE STRICTE ANTI-RÉPÉTITION : Ne répète JAMAIS deux fois la même information ou anecdote déjà dite. Voici ce que tu as DÉJÀ raconté à l'utilisateur :
{historique_texte}
4. TON : Reste fluide, naturel et agréable à écouter en voiture (environ 40 à 50 mots).
"""

        user_prompt = f"""
Commune : {commune}
Département : {departement}
Thème orienté : {categorie_cible}
Ressource Wikipédia départementale : "{wiki_dept}"

Rédige une anecdote intéressante et vivante à lire à voix haute.
"""

        response_text = client_openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.4
        )
        texte_guide = response_text.choices[0].message.content
        session_state["stories_history"].append(texte_guide)

        return {
            "text": texte_guide,
            "commune": commune
        }

    except Exception as e:
        print(f"Erreur Serveur: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/get_quiz_question")
async def get_quiz_question(req: LocationRequest, request: Request):
    global quiz_state
    try:
        commune, departement, wiki_dept = obtenir_infos_locales(req.latitude, req.longitude)
        
        system_instruction = f"Tu es un animateur de quiz touristique. Pose une question amusante ou intéressante sur l'histoire, la gastronomie, la géographie ou les monuments du département ({departement}) ou de la commune ({commune})."
        user_prompt = f"""
Commune : {commune}
Département : {departement}
Contexte : {wiki_dept}

Renvoie un JSON :
{{
  "question": "Question de quiz captivante + Je vous laisse 10 secondes !",
  "reponse": "Explication claire et complète de la réponse."
}}
"""

        response = client_openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.3
        )
        
        data = json.loads(response.choices[0].message.content)
        quiz_state["current_answer"] = data.get("reponse", "")
        question_text = data.get("question", "")

        return {
            "text": question_text
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/get_quiz_answer")
async def get_quiz_answer(request: Request):
    global quiz_state
    reponse_text = quiz_state.get("current_answer", "Information non disponible.")
    return {
        "text": reponse_text
    }

@app.post("/ask_question")
async def ask_question(req: QuestionRequest, request: Request):
    global session_state
    try:
        commune, departement, _ = obtenir_infos_locales(req.latitude, req.longitude)
        dernier_sujet = session_state["stories_history"][-1] if session_state["stories_history"] else "Aucun sujet récent."

        system_instruction = f"""
Tu es un guide vocal réactif en voiture.

CONSIGNES STRICTES :
1. Si l'utilisateur demande où il est (ex: "Où suis-je ?", "Quelle commune ?"), tu dois OBLIGATOIREMENT lui indiquer sa commune ({commune}) ET son département ({departement}).
2. Si l'utilisateur demande de répéter ou de réexpliquer une information précédente, tu as exceptionnellement le droit de la répéter.
3. Si l'utilisateur pose une autre question, réponds de manière concise (35-40 mots max) en t'appuyant sur ta culture générale du département ({departement}).
"""

        prompt = f"""
Localisation : Commune de {commune}, Département {departement}.
Dernière anecdote donnée par le guide : "{dernier_sujet}"

Question posée par l'utilisateur : "{req.question}"
"""

        response_text = client_openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2
        )
        texte_reponse = response_text.choices[0].message.content

        return {
            "text": texte_reponse
        }

    except Exception as e:
        print(f"Erreur question vocale : {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
