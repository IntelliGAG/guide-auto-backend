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
    "Faits historiques précis, dates et batailles du département",
    "Patrimoine bâti, monuments certifiés et architecture",
    "Spécialités culinaires locales, cépages et traditions",
    "Géographie précise, cours d'eau nommés et relief"
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
    """Extrait la commune et le département depuis OSM avec filtrage strict des régions."""
    commune = (addr.get('village') or addr.get('town') or addr.get('city') 
               or addr.get('municipality') or addr.get('suburb') 
               or addr.get('hamlet'))
    
    # On cherche le département dans 'county' en priorité
    departement = addr.get('county') or addr.get('state_district')
    
    # Si le champ renvoie la Région "Pays de la Loire", on tente de corriger avec le code postal
    postcode = addr.get('postcode', '')
    if postcode and len(postcode) >= 2:
        code_dept = postcode[:2]
        depts_map = {
            "44": "Loire-Atlantique",
            "49": "Maine-et-Loire",
            "53": "Mayenne",
            "72": "Sarthe",
            "85": "Vendée"
        }
        if code_dept in depts_map:
            departement = depts_map[code_dept]

    # Sécurité si toujours pas de département trouvé
    if not departement and addr.get('state') and "pays de la loire" not in addr.get('state').lower():
        departement = addr.get('state')

    if commune:
        c_low = commune.lower()
        if "secteur" in c_low or "environnant" in c_low or "localité" in c_low or "votre" in c_low:
            commune = None

    if departement:
        d_low = departement.lower()
        if "département" in d_low or "votre" in d_low or "secteur" in d_low or "pays de la loire" in d_low:
            # Si "Pays de la Loire" subsiste, on force Loire-Atlantique par défaut sur le secteur 44
            if "pays de la loire" in d_low:
                departement = "Loire-Atlantique"

    return commune, departement

def interroger_toutes_les_sources(lat, lon):
    headers = {'User-Agent': 'GuideAutoApp/1.0 (contact@example.com)'}
    commune = None
    departement = None
    
    # SOURCE 1 : Reverse Geocoding OSM (Zoom 13 & 10)
    try:
        url_osm = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=13"
        res = requests.get(url_osm, headers=headers, timeout=3).json()
        addr = res.get('address', {})
        commune, departement = extraction_infos_geo(addr)
    except Exception as e:
        print(f"Erreur OSM: {e}")

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

    # Secours Géographique BigDataCloud
    if not departement:
        try:
            url_bdc = f"https://api.bigdatacloud.net/data/reverse-geocode-client?latitude={lat}&longitude={lon}&localityLanguage=fr"
            res_bdc = requests.get(url_bdc, timeout=3).json()
            d_bdc = res_bdc.get('principalSubdivision')
            if d_bdc and "pays de la loire" not in d_bdc.lower():
                departement = d_bdc
            else:
                departement = "Loire-Atlantique"
            if not commune:
                c_bdc = res_bdc.get('locality') or res_bdc.get('city')
                if c_bdc and "secteur" not in c_bdc.lower():
                    commune = c_bdc
        except Exception as e:
            print(f"Erreur BDC: {e}")

    # SOURCE 2 : Base Mérimée (Ministère de la Culture - Monuments Historiques)
    monuments_merimee = []
    try:
        url_merimee = f"https://data.culture.gouv.fr/api/records/1.0/search/?dataset=liste-des-immeubles-proteges-au-titre-des-monuments-historiques&geofilter.distance={lat}%2C{lon}%2C8000&rows=3"
        res_m = requests.get(url_merimee, headers=headers, timeout=3)
        if res_m.status_code == 200:
            records = res_m.json().get('records', [])
            for r in records:
                fields = r.get('fields', {})
                nom = fields.get('tico') or fields.get('titr')
                if nom:
                    monuments_merimee.append(nom)
    except Exception as e:
        print(f"Erreur Mérimée: {e}")

    source_merimee_txt = f"Monuments classés proches : {', '.join(monuments_merimee)}" if monuments_merimee else "Aucun monument spécifique sur ce point."

    # SOURCE 3 : Overpass API (Lieux d'intérêt & patrimoine OSM)
    lieux_overpass = []
    try:
        overpass_query = f"""
        [out:json][timeout:3];
        (
          node["historic"](around:2000,{lat},{lon});
          way["historic"](around:2000,{lat},{lon});
          node["tourism"="attraction"](around:2000,{lat},{lon});
          node["waterway"](around:2000,{lat},{lon});
        );
        out body 3;
        """
        res_op = requests.post("https://overpass-api.de/api/interpreter", data={"data": overpass_query}, timeout=3)
        if res_op.status_code == 200:
            elements = res_op.json().get('elements', [])
            for el in elements:
                tags = el.get('tags', {})
                nom = tags.get('name')
                if nom:
                    lieux_overpass.append(nom)
    except Exception as e:
        print(f"Erreur Overpass: {e}")

    source_overpass_txt = f"Points d'intérêt Overpass : {', '.join(lieux_overpass)}" if lieux_overpass else "Aucun lieu Overpass immédiat."

    # SOURCE 4 : Wikipédia API (Focus sur le Département)
    wiki_context = ""
    target_search = departement or commune
    if target_search:
        try:
            url_wiki = f"https://fr.wikipedia.org/api/rest_v1/page/summary/{target_search}"
            res_w = requests.get(url_wiki, headers=headers, timeout=3)
            if res_w.status_code == 200:
                wiki_context = res_w.json().get('extract', '')
        except Exception as e:
            print(f"Erreur Wiki: {e}")

    source_wiki_txt = f"Extrait Wikipédia ({target_search}) : {wiki_context}" if wiki_context else "Pas d'extrait Wikipédia."

    return commune, departement, source_merimee_txt, source_overpass_txt, source_wiki_txt

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
        commune, departement, src_merimee, src_overpass, src_wiki = interroger_toutes_les_sources(lat, lon)

        # Formulations de localisation
        consigne_position = ""
        if session_state["current_commune"] is None:
            if commune and departement:
                consigne_position = f"TOUTE PREMIÈRE INTERVENTION : Commence exactement par 'Bienvenue à {commune}, dans le département de {departement}.'"
            elif commune:
                consigne_position = f"TOUTE PREMIÈRE INTERVENTION : Commence exactement par 'Bienvenue à {commune}.'"
            elif departement:
                consigne_position = f"TOUTE PREMIÈRE INTERVENTION : Commence exactement par 'Bienvenue dans le département de {departement}.'"
            else:
                consigne_position = "TOUTE PREMIÈRE INTERVENTION : Entre directement dans le récit avec un fait historique départemental précis."
        elif commune and session_state["current_commune"] != commune:
            ancienne = session_state["current_commune"]
            consigne_position = f"Changement de commune : Commence exactement par 'Nous venons de quitter {ancienne} et entrons dans la commune de {commune}.'"
            session_state["queue_categories"] = list(CATEGORIES_BASE)

        if commune: session_state["current_commune"] = commune
        if departement: session_state["current_departement"] = departement

        if not session_state["queue_categories"]:
            session_state["queue_categories"] = list(CATEGORIES_BASE)
            
        categorie_cible = session_state["queue_categories"].pop(0)
        historique_texte = "\n".join([f"- {h}" for h in session_state["stories_history"]]) if session_state["stories_history"] else "Aucune."

        system_instruction = f"""
Tu es un guide vocal touristique et historique captivant, direct et très bien documenté.

RÈGLES DE NARRATION ET PRÉCISION GÉOGRAPHIQUE :
1. ACCROCHE : {consigne_position}
2. PRÉCISION DÉPARTEMENTALE : Le DÉPARTEMENT est '{departement}' (ATTENTION : 'Pays de la Loire' est une Région, NE DIS JAMAIS 'le département des Pays de la Loire'). Tu peux et DOIS aborder des faits historiques, monuments, batailles, spécialités ou paysages de TOUT LE DÉPARTEMENT de {departement}.
3. CROISEMENT DE SOURCES : Utilise au moins 2 sources parmi celles fournies (Mérimée, Overpass, Wikipédia ou ta culture générale sur le département {departement}).
4. STYLE DIRECT ET FACTUEL :
   - Donne des DATES, DES NOMS PROPRES, DES PERSONNAGES HISTORIQUES et DES LIEUX DÉPARTEMENTAUX PRÉCIS.
   - Pas de questions rhétoriques ("Saviez-vous que...").
   - Ne dis jamais "votre secteur" ou "votre département".
5. ANTI-RÉPÉTITION : Ne répète jamais ce qui a été dit :
{historique_texte}
6. FORMAT : 40 à 50 mots max, oral et captivant.
"""

        user_prompt = f"""
Commune actuelle : {commune if commune else 'Non spécifiée'}
Département exact : {departement if departement else 'Non spécifié'}
Thème imposé : {categorie_cible}

SOURCE 1 (Mérimée) : "{src_merimee}"
SOURCE 2 (Overpass) : "{src_overpass}"
SOURCE 3 (Wikipédia Départemental) : "{src_wiki}"

CONSIGNE : Rédige une anecdote sur le thème "{categorie_cible}" en piochant dans le patrimoine du DÉPARTEMENT {departement}, tout en respectant l'amorce qui cite la commune de {commune}. Noms propres et dates exigés.
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

        return {
            "text": texte_guide,
            "commune": commune if commune else (departement if departement else "Guide Auto")
        }

    except Exception as e:
        print(f"Erreur Serveur: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/get_quiz_question")
async def get_quiz_question(req: LocationRequest, request: Request):
    global quiz_state
    try:
        commune, departement, src_m, src_o, src_w = interroger_toutes_les_sources(req.latitude, req.longitude)
        cible = departement or commune or "la région"
        
        system_instruction = f"Tu es un animateur de quiz. Pose une question précise sur l'histoire, la gastronomie, la géographie ou le patrimoine du département {cible} (Rappel : {cible} est un département, pas une région)."
        user_prompt = f"""
Département : {cible}
Commune : {commune}
Données :
- Mérimée : {src_m}
- Overpass : {src_o}
- Wikipédia : {src_w}

Renvoie un JSON :
{{
  "question": "Question de quiz précise sur le département {cible} + Je vous laisse 10 secondes !",
  "reponse": "Explication factuelle précise avec dates et noms propres."
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
        commune, departement, src_m, src_o, src_w = interroger_toutes_les_sources(req.latitude, req.longitude)
        dernier_sujet = session_state["stories_history"][-1] if session_state["stories_history"] else "Aucun sujet récent."

        system_instruction = f"""
Tu es un guide vocal réactif.

RÈGLES STRICTES :
1. Si l'utilisateur demande où il se trouve : Réponds précisément qu'il est dans la commune de {commune if commune else 'votre position'}, dans le département de {departement if departement else 'Loire-Atlantique'}.
2. Si l'utilisateur demande de répéter : Réexplique brièvement l'anecdote précédente ("{dernier_sujet}").
3. Pour toute autre question : Réponds directement avec des faits, noms propres et dates (35 mots max) sur le département {departement}.
"""

        prompt = f"""
Commune : {commune}
Département : {departement}
Sources : {src_m} | {src_o} | {src_w}
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
