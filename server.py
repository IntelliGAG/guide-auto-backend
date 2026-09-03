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
    "Histoire et origine de la commune",
    "Monuments historiques certifiés et patrimoine bâti",
    "Géographie, cours d'eau et paysages locaux",
    "Spécialités, terroir et traditions"
]

session_state = {
    "last_lat": None,
    "last_lon": None,
    "current_commune": None,
    "stories_history": [],
    "queue_categories": list(CATEGORIES_BASE)
}

quiz_state = {
    "current_answer": "",
    "quiz_history": []
}

def calculer_point_en_avant(lat, lon, heading, distance_km=3.0):
    if heading < 0:
        return lat, lon
    
    r_terre = 6371.0
    cap_rad = math.radians(heading)
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)

    lat2_rad = math.asin(
        math.sin(lat_rad) * math.cos(distance_km / r_terre) +
        math.cos(lat_rad) * math.sin(distance_km / r_terre) * math.cos(cap_rad)
    )
    lon2_rad = lon_rad + math.atan2(
        math.sin(cap_rad) * math.sin(distance_km / r_terre) * math.cos(lat_rad),
        math.cos(distance_km / r_terre) - math.sin(lat_rad) * math.sin(lat2_rad)
    )

    return math.degrees(lat2_rad), math.degrees(lon2_rad)

def obtenir_infos_locales(lat, lon, heading=-1.0, elargir_trajectoire=False):
    headers = {'User-Agent': 'GuideAutoApp/1.0 (contact@example.com)'}
    commune = None
    
    url_osm = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=13"
    try:
        res = requests.get(url_osm, headers=headers, timeout=3).json()
        addr = res.get('address', {})
        # Recherche élargie du nom exact pour éviter "cette localité"
        commune = (addr.get('village') or addr.get('town') or addr.get('city') 
                   or addr.get('municipality') or addr.get('suburb') or addr.get('county'))
    except Exception as e:
        print(f"Erreur OSM: {e}")

    if not commune:
        commune = "cette zone"

    commune_a_venir = None
    if elargir_trajectoire and heading >= 0:
        lat_fwd, lon_fwd = calculer_point_en_avant(lat, lon, heading, distance_km=4.0)
        url_osm_fwd = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat_fwd}&lon={lon_fwd}&zoom=13"
        try:
            res_fwd = requests.get(url_osm_fwd, headers=headers, timeout=3).json()
            addr_fwd = res_fwd.get('address', {})
            commune_a_venir = (addr_fwd.get('village') or addr_fwd.get('town') or addr_fwd.get('city') 
                               or addr_fwd.get('municipality') or addr_fwd.get('suburb') or addr_fwd.get('county'))
        except Exception as e:
            print(f"Erreur OSM Trajectoire: {e}")

    wiki_summary = ""
    try:
        url_wiki = f"https://fr.wikipedia.org/api/rest_v1/page/summary/{commune}"
        res_w = requests.get(url_wiki, headers=headers, timeout=3)
        if res_w.status_code == 200:
            wiki_summary = res_w.json().get('extract', '')
    except Exception as e:
        print(f"Erreur Wikipedia: {e}")

    wiki_summary_a_venir = ""
    if commune_a_venir and commune_a_venir != commune:
        try:
            url_wiki_fwd = f"https://fr.wikipedia.org/api/rest_v1/page/summary/{commune_a_venir}"
            res_wf = requests.get(url_wiki_fwd, headers=headers, timeout=3)
            if res_wf.status_code == 200:
                wiki_summary_a_venir = res_wf.json().get('extract', '')
        except Exception as e:
            print(f"Erreur Wikipedia Trajectoire: {e}")

    monuments_merimee = []
    try:
        url_merimee = f"https://data.culture.gouv.fr/api/records/1.0/search/?dataset=liste-des-immeubles-proteges-au-titre-des-monuments-historiques&geofilter.distance={lat}%2C{lon}%2C5000&rows=3"
        res_m = requests.get(url_merimee, headers=headers, timeout=3)
        if res_m.status_code == 200:
            records = res_m.json().get('records', [])
            for r in records:
                fields = r.get('fields', {})
                nom = fields.get('tico') or fields.get('titr')
                if nom:
                    monuments_merimee.append(nom)
    except Exception as e:
        print(f"Erreur Base Mérimée: {e}")

    source_merimee_txt = f"Monuments officiellement classés aux alentours : {', '.join(monuments_merimee)}" if monuments_merimee else "Aucun monument spécial."

    lieux_proches = []
    try:
        overpass_query = f"""
        [out:json][timeout:3];
        (
          node["historic"](around:1200,{lat},{lon});
          way["historic"](around:1200,{lat},{lon});
          node["tourism"="attraction"](around:1200,{lat},{lon});
        );
        out body 3;
        """
        res_op = requests.post("https://overpass-api.de/api/interpreter", data={"data": overpass_query}, timeout=3)
        if res_op.status_code == 200:
            elements = res_op.json().get('elements', [])
            for el in elements:
                tags = el.get('tags', {})
                nom = tags.get('name')
                horaires = tags.get('opening_hours')
                if nom:
                    info = f"{nom}"
                    if horaires:
                        info += f" (Horaires : {horaires})"
                    lieux_proches.append(info)
    except Exception as e:
        print(f"Erreur Overpass: {e}")

    source_lieux_proches = f"Lieux proches : {', '.join(lieux_proches)}" if lieux_proches else "Aucun lieu immédiat."

    return commune, wiki_summary, source_merimee_txt, source_lieux_proches, commune_a_venir, wiki_summary_a_venir

@app.post("/reset")
async def reset_session():
    global session_state, quiz_state
    session_state = {
        "last_lat": None,
        "last_lon": None,
        "current_commune": None,
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
        lat, lon, heading = req.latitude, req.longitude, req.heading

        elargir = False
        if not session_state["queue_categories"]:
            elargir = True
            session_state["queue_categories"] = list(CATEGORIES_BASE)

        commune, wiki_summary, source_merimee, source_proche, commune_fwd, wiki_fwd = obtenir_infos_locales(
            lat, lon, heading, elargir_trajectoire=elargir
        )

        changement_commune_prompt = ""
        if session_state["current_commune"] and session_state["current_commune"] != commune:
            changement_commune_prompt = f"Nous changeons de commune : commence par indiquer 'Nous arrivons à {commune}'."
            session_state["queue_categories"] = list(CATEGORIES_BASE)

        session_state["current_commune"] = commune
        session_state["last_lat"] = lat
        session_state["last_lon"] = lon

        categorie_cible = session_state["queue_categories"].pop(0)
        historique_texte = "\n".join([f"- {h}" for h in session_state["stories_history"][-5:]]) if session_state["stories_history"] else "Aucun."

        instruction_trajectoire = ""
        if elargir and commune_fwd and commune_fwd != commune:
            instruction_trajectoire = f"""
ATTENTION : Nous avons fait le tour des informations immédiates sur {commune}.
ÉLARGISSEMENT STRATÉGIQUE : L'usager se dirige vers {commune_fwd}. 
Parle d'un point d'intérêt, d'une entreprise connue, d'un monument ou d'une spécificité située vers {commune_fwd} (sur sa route actuelle).
INTERDICTION d'évoquer des communes dans la direction opposée.
"""

        orientation_instruction = ""
        if heading >= 0:
            orientation_instruction = "Le véhicule avance avec un cap GPS valide. Tu peux utiliser des repères comme 'à votre droite', 'sur votre gauche' ou 'devant vous' si tu cites un lieu présent dans les sources."
        else:
            orientation_instruction = "Le cap est inconnu. N'utilise pas d'indications de direction."

        system_instruction = f"""
Tu es un guide vocal de voiture passionnant, précis et très bien informé sur la France (terroir, histoire, grandes entreprises, géographie, culture).

RÈGLES NARRATIVES STRICTES :
1. NOM DE LA ZONE : {changement_commune_prompt if changement_commune_prompt else f'Nous sommes dans la zone de {commune}.'} Ne dis JAMAIS "cette localité" ou "cette commune", nomme toujours la ville ou la région explicitement ({commune}).
2. PAS DE BANALITÉ NI DE PHRASE CREUSE : Ne dis JAMAIS "il n'y a pas de monument", "cette ville a une riche histoire" ou des généralités vagues. Va DROIT AUX FAITS CONCRETS.
3. CONNAISSANCE DU TERROIR ET DU PATRIMOINE : Utilise les sources fournies, MAIS complète librement avec ta culture générale sur le secteur de {commune} (appellations viticoles comme le Muscadet, industries emblématiques comme LU, histoire locale, étymologie du nom, rivières, spécialités culinaires).
4. ÉLARGISSEMENT : Si la commune est petite ou manque de faits majeurs, parle immédiatement de l'histoire du canton, du bassin industriel/viticole environnant ou de la grande ville/rivière la plus proche.
5. {instruction_trajectoire if instruction_trajectoire else 'Reste concentré sur le secteur.'}
6. ORIENTATION : {orientation_instruction}
7. Ne répète jamais ces anecdotes récentes :
{historique_texte}
"""

        user_prompt = f"""
Secteur : {commune}
Thème imposé : {categorie_cible}

SOURCE 1 (Wikipédia) : "{wiki_summary if wiki_summary else 'Extrait indisponible.'}"
SOURCE 2 (Monuments) : "{source_merimee}"
SOURCE 3 (Proximité) : "{source_proche}"
{"SOURCE 4 (Prochaine commune - " + str(commune_fwd) + ") : \"" + str(wiki_fwd) + "\"" if elargir and wiki_fwd else ""}

CONSIGNE : Donne une anecdote ultra-concrète (35-40 mots max) sur {commune} ou son secteur immédiat liée au thème "{categorie_cible}". 
Cite des détails précis : noms de cépages/vins, entreprises historiques, rivières, origine du nom de la ville ou faits marquants.
"""

        response_text = client_openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.2
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
        commune, wiki_summary, source_merimee, source_proche, _, _ = obtenir_infos_locales(req.latitude, req.longitude, req.heading)
        
        system_instruction = "Tu es un animateur de quiz radio. Pose une question précise sur une histoire, une spécialité ou un fait marquant lié au secteur."
        user_prompt = f"""
Commune : {commune}
SOURCE 1 : "{wiki_summary}"
SOURCE 2 : "{source_merimee}"
SOURCE 3 : "{source_proche}"

Renvoie un JSON :
{{
  "question": "Question précise sur {commune} ou sa région + Je vous laisse 10 secondes !",
  "reponse": "Explication factuelle avec noms propres exacts."
}}
"""

        response = client_openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.2
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
        commune, wiki_summary, source_merimee, source_proche, _, _ = obtenir_infos_locales(
            req.latitude, req.longitude, req.heading
        )
        
        dernier_sujet = session_state["stories_history"][-1] if session_state["stories_history"] else "Aucun sujet récent."

        system_instruction = f"""
Tu es un guide vocal réactif. L'utilisateur te pose une question directe à l'oral pendant qu'il conduit.

CONSIGNES STRICTES :
1. Si l'utilisateur demande 'dans quelle commune se trouve-t-on', réponds uniquement avec le nom : {commune}.
2. Si l'utilisateur demande des précisions, approfondis le sujet en utilisant la culture générale de la région de {commune}.
3. Sois ultra-concis (35 mots max) et captivant.
"""

        prompt = f"""
Localisation : {commune}
Cap GPS véhicule : {req.heading}
Dernier sujet évoqué par le guide : "{dernier_sujet}"

SOURCE 1 (Wikipédia) : "{wiki_summary}"
SOURCE 2 (Ministère de la Culture) : "{source_merimee}"
SOURCE 3 (Lieux proches) : "{source_proche}"

Question orale de l'utilisateur : "{req.question}"
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
