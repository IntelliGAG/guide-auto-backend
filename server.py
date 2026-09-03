import os
import math
import json
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI
from elevenlabs.client import ElevenLabs

# --- CLÉS API (Lecture sécurisée depuis l'environnement Render ou local) ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "sk_026bfc79a7632eb4102ba554010198b1b41086aa8b26ddfa")

client_openai = OpenAI(api_key=OPENAI_API_KEY)
client_eleven = ElevenLabs(api_key=ELEVENLABS_API_KEY)

# ID de la voix "George" (100% autorisée via API gratuite)
VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"

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

class QuestionRequest(BaseModel):
    latitude: float
    longitude: float
    question: str

CATEGORIES_BASE = [
    "Origine du nom de la commune, étymologie ou tradition gastronomique historique",
    "Patrimoine bâti, monuments réels, architecture ou curiosités",
    "Histoire locale, personnages historiques ou événements marquants",
    "Géographie, cours d'eau nommés précisément, reliefs et environnement naturel",
    "Économie historique, terroir et traditions viticoles"
]

session_state = {
    "last_lat": None,
    "last_lon": None,
    "current_commune": None,
    "previous_commune": None,
    "stories_history": [],
    "queue_categories": list(CATEGORIES_BASE)
}

quiz_state = {
    "current_answer": "",
    "quiz_history": []
}

def generer_audio_elevenlabs(texte: str, output_path: str = "latest_story.mp3"):
    """Génère un fichier audio ultra-réaliste via ElevenLabs"""
    try:
        audio = client_eleven.text_to_speech.convert(
            text=texte,
            voice_id=VOICE_ID,
            model_id="eleven_multilingual_v2"
        )
        with open(output_path, "wb") as f:
            for chunk in audio:
                f.write(chunk)
    except AttributeError:
        audio_generator = client_eleven.generate(
            text=texte,
            voice=VOICE_ID,
            model="eleven_multilingual_v2"
        )
        with open(output_path, "wb") as f:
            for chunk in audio_generator:
                f.write(chunk)

def obtenir_infos_commune(lat, lon):
    headers = {'User-Agent': 'GuideAutoApp/1.0 (contact@example.com)'}
    commune = None
    
    url_osm = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=18"
    try:
        res = requests.get(url_osm, headers=headers, timeout=4).json()
        addr = res.get('address', {})
        commune = addr.get('village') or addr.get('town') or addr.get('municipality') or addr.get('city')
    except Exception as e:
        print(f"Erreur OSM: {e}")

    if not commune:
        commune = "La Haie-Fouassière" if (47.1 < lat < 47.2 and -1.5 < lon < -1.3) else "cette localité"

    wiki_summary = ""
    try:
        url_wiki = f"https://fr.wikipedia.org/api/rest_v1/page/summary/{commune}"
        res_w = requests.get(url_wiki, headers=headers, timeout=4)
        if res_w.status_code == 200:
            wiki_summary = res_w.json().get('extract', '')
    except Exception as e:
        print(f"Erreur Wikipedia: {e}")

    return commune, wiki_summary

@app.post("/reset")
async def reset_session():
    global session_state, quiz_state
    session_state = {
        "last_lat": None,
        "last_lon": None,
        "current_commune": None,
        "previous_commune": None,
        "stories_history": [],
        "queue_categories": list(CATEGORIES_BASE)
    }
    quiz_state = {
        "current_answer": "",
        "quiz_history": []
    }
    print("🧹 Mémoire réinitialisée !")
    return {"message": "Réinitialisation réussie"}

# --- MODE 1 : AUDIO GUIDE ---
@app.post("/get_story")
async def generate_story(req: LocationRequest, request: Request):
    global session_state
    try:
        lat, lon = req.latitude, req.longitude
        commune, wiki_summary = obtenir_infos_commune(lat, lon)

        changement_commune_prompt = ""
        if session_state["current_commune"] and session_state["current_commune"] != commune:
            changement_commune_prompt = f"Tu viens de changer de ville. Démarre par une phrase d'annonce du type : 'Nous venons d'entrer dans la commune de {commune}'."
            session_state["queue_categories"] = list(CATEGORIES_BASE)

        session_state["previous_commune"] = session_state["current_commune"]
        session_state["current_commune"] = commune
        session_state["last_lat"] = lat
        session_state["last_lon"] = lon

        if session_state["queue_categories"]:
            categorie_cible = session_state["queue_categories"].pop(0)
        else:
            session_state["queue_categories"] = list(CATEGORIES_BASE)
            categorie_cible = session_state["queue_categories"].pop(0)

        historique_texte = "\n".join([f"- {h}" for h in session_state["stories_history"]]) if session_state["stories_history"] else "Aucun."

        system_instruction = f"""
Tu es un guide vocal de voiture ultra-précis et factuel.
RÈGLES ABSOLUES :
1. {changement_commune_prompt if changement_commune_prompt else f'Cite la commune de {commune} au moins une fois de façon naturelle.'}
2. PRÉCISION STRICTE : Nomme TOUJOURS précisément les châteaux, églises ou cours d'eau.
3. N'INVENTE RIEN. Si un détail manque dans la source, ne le brode pas.
4. Évite les répétitions avec cet historique :
{historique_texte}
"""

        user_prompt = f"""
Commune : {commune}
Thème : {categorie_cible}
Source : "{wiki_summary}"
Rédige une anecdote orale courte (40 mots max).
"""

        response_text = client_openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3
        )
        texte_guide = response_text.choices[0].message.content

        session_state["stories_history"].append(texte_guide)
        print(f"🎭 Récit généré ({commune}) : {texte_guide}")

        generer_audio_elevenlabs(texte_guide)

        return {
            "text": texte_guide,
            "audio_url": f"{request.base_url}get_audio",
            "commune": commune
        }

    except Exception as e:
        print(f"❌ ERREUR SERVEUR : {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- MODE 2 : QUIZ (QUESTION) ---
@app.post("/get_quiz_question")
async def get_quiz_question(req: LocationRequest, request: Request):
    global quiz_state
    try:
        commune, wiki_summary = obtenir_infos_commune(req.latitude, req.longitude)
        historique_quiz = "\n".join([f"- {q}" for q in quiz_state["quiz_history"]]) if quiz_state["quiz_history"] else "Aucune."

        system_instruction = """
Tu es un animateur de quiz radio en voiture.
RÈGLES ABSOLUES :
1. Interdiction de poser une question administrative basique sur le département/région sauf si c'est la seule info disponible.
2. Varie les sujets : spécialités gastronomiques, géographie, histoire, monuments ou terroir.
3. Il est STRICTEMENT INTERDIT de poser une question similaire aux questions précédentes listées dans l'historique.
4. Génère UNIQUEMENT la question et termine par une phrase d'attente (ex: "Je vous laisse 10 secondes pour y réfléchir !").
"""

        user_prompt = f"""
Commune : {commune}
SOURCE OFFICIELLE : "{wiki_summary}"

HISTORIQUE DES QUESTIONS DÉJÀ POSÉES (À NE PAS RÉPÉTER) :
{historique_quiz}

Propose une NOUVELLE question originale et intéressante.
Renvoie un objet JSON :
{{
  "question": "La question + phrase d'attente",
  "reponse": "La réponse détaillée + 'Prêt pour la question suivante ?'"
}}
"""

        response = client_openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.5
        )
        
        data = json.loads(response.choices[0].message.content)
        quiz_state["current_answer"] = data.get("reponse", "")
        question_text = data.get("question", "")

        quiz_state["quiz_history"].append(question_text)

        generer_audio_elevenlabs(question_text)

        return {
            "text": question_text,
            "audio_url": f"{request.base_url}get_audio"
        }

    except Exception as e:
        print(f"❌ Erreur Question Quiz : {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- MODE 2 : QUIZ (RÉPONSE) ---
@app.post("/get_quiz_answer")
async def get_quiz_answer(request: Request):
    global quiz_state
    try:
        reponse_text = quiz_state.get("current_answer", "Je n'ai pas retrouvé la réponse.")

        generer_audio_elevenlabs(reponse_text)

        return {
            "text": reponse_text,
            "audio_url": f"{request.base_url}get_audio"
        }

    except Exception as e:
        print(f"❌ Erreur Réponse Quiz : {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- INTERACTION VOCALE ---
@app.post("/ask_question")
async def ask_question(req: QuestionRequest, request: Request):
    try:
        commune, wiki_summary = obtenir_infos_commune(req.latitude, req.longitude)
        
        system_instruction = """
Tu es un guide vocal. L'utilisateur te pose une question directe pendant qu'il conduit.
RÈGLES ANTI-HALLUCINATION STRICTES :
1. Utilise systématiquement des formules de précaution : "D'après mes informations...", "À ma connaissance...".
2. Si tu n'as pas l'information exacte dans la source, dis clairement : "À ma connaissance, il n'y a pas d'information précise sur ce sujet ici."
"""

        prompt = f"""
Localisation : {commune}
Source : "{wiki_summary}"
Question de l'utilisateur : "{req.question}"

Réponds directement de manière concise (30 à 40 mots max).
"""

        response_text = client_openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1
        )
        texte_reponse = response_text.choices[0].message.content

        generer_audio_elevenlabs(texte_reponse)

        return {
            "text": texte_reponse,
            "audio_url": f"{request.base_url}get_audio"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/get_audio")
async def get_audio():
    return FileResponse("latest_story.mp3", media_type="audio/mpeg")

# --- DEMARRAGE DU SERVEUR ---
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)