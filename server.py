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
    """Extrait la commune et le département depuis les données OSM."""
    commune = (addr.get('village') or addr.get('town') or addr.get('city') 
               or addr.get('municipality') or addr.get('suburb') 
               or addr.get('hamlet'))
    
    departement = addr.get('county') or addr.get('state_district') or addr.get('state')
    
    return commune, departement

def obtenir_infos_locales(lat, lon):
    headers = {'User-Agent': 'GuideAutoApp/1.0 (contact@example.com)'}
    commune = None
    departement = None
    
    # 1. Premier essai OSM Zoom 13
    try:
        url_osm = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=13"
        res = requests.get(url_osm, headers=headers, timeout=3).json()
        addr = res.get('address', {})
        commune, departement = extraction_infos_geo(addr)
    except Exception as e:
        print(f"Erreur OSM Zoom 13: {e}")

    # 2. Second essai OSM Zoom 10 (niveau département/canton)
    if not commune or not departement:
        try:
            url_osm_w = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=10"
            res_w = requests.get(url_osm_w, headers=headers, timeout=3).json()
            addr_w = res_w.get('address', {})
            c_tmp, d_tmp = extraction_infos_geo(addr_w)
            if not commune: commune = c_tmp
            if not departement: departement = d_tmp
        except Exception as e:
            print(f"Erreur OSM Zoom 10: {e}")

    # 3. Secours via API BigDataCloud (gratuite sans clé) si OSM ne renvoie pas le département
    if not departement:
        try:
            url_bdc = f"https://api.bigdatacloud.net/data/reverse-geocode-client?latitude={lat}&longitude={lon}&localityLanguage=fr"
            res_bdc = requests.get(url_bdc, timeout=3).json()
            departement = res_bdc.get('principalSubdivision')
            if not commune:
                commune = res_bdc.get('locality') or res_bdc.get('city')
        except Exception as e:
            print(f"Erreur BDC: {e}")

    # Nettoyage strict des variables pour éviter de transmettre des mots génériques à l'IA
    if commune and ("secteur" in commune.lower() or "environnant" in commune.lower()):
        commune = None

    if departement and ("département" in departement.lower() or "votre" in departement.lower()):
        departement = None

    # Extrait Wikipédia pour donner du contexte réel
    wiki_context = ""
    target_search = commune or departement
    if target_search:
        try:
            url_wiki = f"https://fr.wikipedia.org/api/rest_v1/page/summary/{target_search}"
            res_w = requests.get(url_wiki, headers=headers, timeout=3)
            if res_w.status_code == 200:
                wiki_context = res_w.json().get('extract', '')
        except Exception as e:
            print(f"Erreur Wiki: {e}")

    return commune, departement, wiki_context

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
        commune, departement, wiki_context = obtenir_infos_locales(lat, lon)

        # Construction de la consigne d'accroche sans phrases génériques
        consigne_position = ""
        if session_state["current_commune"] is None:
            if commune and departement:
                consigne_position = f"TOUTE PREMIÈRE INTERVENTION : Commence exactement par 'Bienvenue à {commune}, dans le département de {departement}'."
            elif commune:
                consigne_position = f"TOUTE PREMIÈRE INTERVENTION : Commence exactement par 'Bienvenue à {commune}'."
            elif departement:
                consigne_position = f"TOUTE PREMIÈRE INTERVENTION : Commence exactement par 'Bienvenue dans le département de {departement}'."
            else:
                consigne_position = "TOUTE PREMIÈRE INTERVENTION : Commence directement par présenter la région où l'usager circule sans formule bancale."
        elif commune and session_state["current_commune"] != commune:
            ancienne = session_state["current_commune"]
            consigne_position = f"Changement de commune : Commence exactement par 'Nous venons de quitter {ancienne} et entrons dans la commune de {commune}'."
            session_state["queue_categories"] = list(CATEGORIES_BASE)

        session_state["current_commune"] = commune if commune else session_state["current_commune"]
        session_state["current_departement"] = departement if departement else session_state["current_departement"]

        if not session_state["queue_categories"]:
            session_state["queue_categories"] = list(CATEGORIES_BASE)
            
        categorie_cible = session_state["queue_categories"].pop(0)
        historique_texte = "\n".join([f"- {h}" for h in session_state["stories_history"]]) if session_state["stories_history"] else "Aucune."

        system_instruction = f"""
Tu es un guide touristique vocal de voiture, passionné, érudit et très concrét.

RÈGLES D'OR ABSOLUES :
1. ACCROCHE : {consigne_position}
2. INTERDICTION DES PHRASES CREUSES OU FLOUES : Ne dis JAMAIS "il y a un fleuve", "un monument se trouve ici", "votre secteur" ou "votre département". Tu dois OBLIGATOIREMENT donner des NOMS PROPRES (ex: La Loire, le château de X, le vignoble du Muscadet, etc.).
3. PÉRIMÈTRE : Fais appel à ta culture générale sur le département ({departement if departement else 'de la région actuelle'}) ou la commune ({commune if commune else 'locale'}).
4. ANTI-RÉPÉTITION : Ne répète jamais ce qui a déjà été dit :
{historique_texte}
5. FORMAT : Récit direct, fluide et vivant de 40 à 50 mots.
"""

        user_prompt = f"""
Coordonnées GPS : Lat {lat}, Lon {lon}
Commune identifiée : {commune if commune else 'Non spécifiée'}
Département identifié : {departement if departement else 'Non spécifié'}
Thème imposé : {categorie_cible}
Extrait Wikipédia : "{wiki_context}"

Rédige une anecdote captivante truffée de noms propres et de détails réels.
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
            "commune": commune if commune else (departement if departement else "Votre route")
        }

    except Exception as e:
        print(f"Erreur Serveur: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/get_quiz_question")
async def get_quiz_question(req: LocationRequest, request: Request):
    global quiz_state
    try:
        commune, departement, wiki_context = obtenir_infos_locales(req.latitude, req.longitude)
        cible = departement or commune or "la région"
        
        system_instruction = f"Tu es un animateur de quiz radio. Pose une question précise avec des noms propres sur l'histoire, la gastronomie ou la géographie de {cible}."
        user_prompt = f"""
Secteur : {cible}
Extrait : {wiki_context}

Renvoie un JSON :
{{
  "question": "Question de quiz précise sur {cible} + Je vous laisse 10 secondes !",
  "reponse": "Réponse complète avec noms propres."
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
Tu es un guide vocal en voiture.

RÈGLES DE RÉPONSE :
1. Si l'utilisateur demande où il est : Donne les noms propres réels de la commune ({commune}) et du département ({departement}). Ne dis JAMAIS "votre secteur" ou "votre département".
2. Si l'utilisateur demande de répéter : Réexplique brièvement le dernier sujet ("{dernier_sujet}").
3. Pour toute autre question : Réponds concisément (35 mots max) avec des détails réels sur le département de {departement if departement else 'la région'}.
"""

        prompt = f"""
Commune : {commune}
Département : {departement}
Dernier sujet : "{dernier_sujet}"
Question utilisateur : "{req.question}"
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
