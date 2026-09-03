def interroger_toutes_les_sources(lat, lon):
    # User-Agent unique pour éviter le blocage par Nominatim/OSM
    headers = {'User-Agent': 'GuideAutoApp_PROD_v2/1.0 (contact_app@domain.com)'}
    commune = None
    departement = None
    
    # SOURCE 1 : Reverse Geocoding OSM (Zoom 13 & 10)
    try:
        url_osm = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=13&addressdetails=1"
        res = requests.get(url_osm, headers=headers, timeout=4)
        if res.status_code == 200:
            addr = res.json().get('address', {})
            commune, departement = extraction_infos_geo(addr)
    except Exception as e:
        print(f"Erreur OSM: {e}")

    if not commune or not departement:
        try:
            url_osm_w = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=10&addressdetails=1"
            res_w = requests.get(url_osm_w, headers=headers, timeout=4)
            if res_w.status_code == 200:
                addr_w = res_w.json().get('address', {})
                c_tmp, d_tmp = extraction_infos_geo(addr_w)
                if not commune: commune = c_tmp
                if not departement: departement = d_tmp
        except Exception as e:
            print(f"Erreur OSM Zoom 10: {e}")

    departement = nettoyer_departement(departement)

    # SOURCE 2 : Base Mérimée (Monuments Historiques)
    monuments_merimee = []
    try:
        url_merimee = f"https://data.culture.gouv.fr/api/records/1.0/search/?dataset=liste-des-immeubles-proteges-au-titre-des-monuments-historiques&geofilter.distance={lat}%2C{lon}%2C8000&rows=3"
        res_m = requests.get(url_merimee, headers=headers, timeout=4)
        if res_m.status_code == 200:
            records = res_m.json().get('records', [])
            for r in records:
                fields = r.get('fields', {})
                nom = fields.get('tico') or fields.get('titr')
                if nom:
                    monuments_merimee.append(nom)
    except Exception as e:
        print(f"Erreur Mérimée: {e}")

    source_merimee_txt = f"Monuments classés proches : {', '.join(monuments_merimee)}" if monuments_merimee else "Aucun monument spécifique immédiat."

    # SOURCE 3 : Overpass API avec secours d'instance
    lieux_overpass = []
    overpass_endpoints = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter"
    ]
    overpass_query = f"""
    [out:json][timeout:4];
    (
      node["historic"](around:2000,{lat},{lon});
      way["historic"](around:2000,{lat},{lon});
      node["tourism"="attraction"](around:2000,{lat},{lon});
      node["waterway"](around:2000,{lat},{lon});
    );
    out body 3;
    """
    for endpoint in overpass_endpoints:
        try:
            res_op = requests.post(endpoint, data={"data": overpass_query}, headers=headers, timeout=4)
            if res_op.status_code == 200:
                elements = res_op.json().get('elements', [])
                for el in elements:
                    tags = el.get('tags', {})
                    nom = tags.get('name')
                    if nom:
                        lieux_overpass.append(nom)
                break
        except Exception as e:
            print(f"Erreur Overpass ({endpoint}): {e}")

    source_overpass_txt = f"Points d'intérêt Overpass : {', '.join(lieux_overpass)}" if lieux_overpass else "Aucun lieu Overpass immédiat."

    # SOURCE 4 : Wikipédia API
    wiki_context = ""
    target_search = departement or commune
    if target_search:
        try:
            url_wiki = f"https://fr.wikipedia.org/api/rest_v1/page/summary/{target_search}"
            res_w = requests.get(url_wiki, headers=headers, timeout=4)
            if res_w.status_code == 200:
                wiki_context = res_w.json().get('extract', '')
        except Exception as e:
            print(f"Erreur Wiki: {e}")

    source_wiki_txt = f"Extrait Wikipédia ({target_search}) : {wiki_context}" if wiki_context else "Pas d'extrait Wikipédia."

    return commune, departement, source_merimee_txt, source_overpass_txt, source_wiki_txt
