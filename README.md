# GTFS Manager – Home Assistant integráció

Élő (valós idejű) indulási idők megállónként, egy **GTFS Manager** szerver
`/api/v1` végpontjáról. Megállót, vagy megálló + járat párost lehet felvenni.
Minden felvett megálló három szenzort kap, és van egy szolgáltatás is, amely
kész, felolvasható magyar mondatot ad vissza. Ez hangasszisztenshez is
használható (Google Home, Gemini, Assist).

> Az integráció nem tartalmaz beégetett szervercímet. A GTFS Manager
> példány címét a beállításkor kell megadni.

## Mit tud

| Entitás | Állapot | Fontosabb attribútumok |
|---|---|---|
| `sensor.<megálló>_kovetkezo_indulas` | a következő indulás **várható** időpontja (timestamp) | `route`, `headsign`, `scheduled`, `expected`, `minutes`, `delay_min`, `realtime`, `speech` |
| `sensor.<megálló>_kovetkezo_indulas_perc` | hány perc múlva indul | `route`, `headsign`, `realtime` |
| `sensor.<megálló>_menetrend` | indulások száma az időablakban | `departures` (lista), `speech` |

- A **késés** a szerver élő adataiból jön (saját követett járművek, külső
  GTFS-RealTime források). Ha egy menetről nincs élő adat, a menetrendi
  időt kapod, `realtime: false` jelöléssel. A felolvasott mondat ilyenkor azt
  mondja: „menetrend szerint”.
- A **törölt** (elmaradó) menet a listában `canceled: true` jelölést kap, a
  „következő indulás” szenzor pedig átugorja.
- Az **állomás** típusú megálló (több peron) az összes peron indulását mutatja.

## Telepítés (HACS)

1. HACS → jobb felső ⋮ → **Custom repositories** → Repository:
   `https://github.com/patyiandras/ha-gtfs-manager`, Type: **Integration** → Add.
2. HACS-ben keresd meg a **GTFS Manager** integrációt → **Download**.
3. Indítsd újra a Home Assistantot.
4. **Beállítások → Eszközök és szolgáltatások → Integráció hozzáadása →
   GTFS Manager**.

Kézi telepítés: másold a `custom_components/gtfs_manager` mappát a HA
`config/custom_components/` mappájába, majd indítsd újra a HA-t.

## Beállítás

A varázsló lépései:

1. **Szerver címe**: a GTFS Manager példány alap-URL-je. Séma nélkül
   `https://` kerül elé.
2. **Menetrend**: csak akkor kérdezi, ha a szerveren több is van.
3. **Megálló keresése**: név vagy megállókód. Üresen hagyva az otthonod
   1 km-es körzetében lévő megállókat listázza.
4. **Megálló kiválasztása**: az azonos nevű peronokat a járatok és az irány
   (→ végállomás) különbözteti meg.
5. **Járatok**: egy vagy több járat, vagy egyik sem (akkor a megálló összes
   járata). Itt nevet is adhatsz a bejegyzésnek.

Ugyanazt a megállót más járat-szűréssel többször is felveheted. Utólag a
bejegyzés **Beállítások** gombjával módosítható:
- a járat-szűrés,
- a lista hossza (1–50),
- az időablak (10–720 perc, alapértelmezés 180),
- a frissítés gyakorisága (30–600 mp, alapértelmezés 60).

## Szolgáltatás: `gtfs_manager.get_departures`

Friss lekérés egy felvett megállóra. A válasz:

```yaml
stop: Határ út M
speech: >-
  Az 50-es villamos 3 perc múlva indul Pestszentlőrinc, Béke tér felé, 2 perc
  késéssel. Utána 9 perc múlva jön a következő.
departures:
  - route: "50"
    mode: tram
    headsign: Pestszentlőrinc, Béke tér
    scheduled: "14:51"
    expected: "14:53"
    minutes: 3
    delay_min: 2
    realtime: true
    canceled: false
```

A `config_entry_id` értéket a **Fejlesztői eszközök → Műveletek** felületén
a megálló-választó tölti ki. YAML-nézetre váltva látszik az azonosító.

## Példák

### Dashboard: Markdown-kártya

```yaml
type: markdown
title: Határ út M
content: >
  {% for d in state_attr('sensor.hatar_ut_m_50_menetrend', 'departures') or [] %}
  **{{ d.route }}** → {{ d.headsign }} · {{ d.expected }}
  {%- if d.canceled %} ~~elmarad~~
  {%- elif d.realtime and d.delay_min %} ({{ '%+d' % d.delay_min }} perc)
  {%- elif not d.realtime %} *(menetrend)*{% endif %}
  · {{ d.minutes }} perc
  {% endfor %}
```

### „Mikor jön az 50-es?” – válasz a Google/Nest hangszórón

Egy szkript lekéri az indulásokat, és felolvastatja őket a hangszórón:

```yaml
script:
  mikor_jon_az_50_es:
    alias: Mikor jön az 50-es?
    description: Megmondja, mikor indul a következő 50-es villamos a Határ út M megállóból.
    sequence:
      - action: gtfs_manager.get_departures
        data:
          config_entry_id: 01JXXXXXXXXXXXXXXXXXXXXXXX
          limit: 3
        response_variable: dep
      - action: tts.speak
        target:
          entity_id: tts.google_translate_hu_hu
        data:
          media_player_entity_id: media_player.nappali_hangszoro
          message: "{{ dep.speech }}"
```

Ezután a szkriptet **kiteszed a Google Assistant felé**:
- Nabu Casa esetén: **Beállítások → Hangasszisztensek → Kitett entitások**;
- kézi beállításnál: a `google_assistant:` integráció.

A Google felé a szkript jelenetként (scene) jelenik meg. A **Google Home**
appban egy **rutint** kell létrehozni:
- indító: „Mikor jön az 50-es?” hangparancs;
- művelet: a jelenet aktiválása.

A választ a HA mondja el TTS-sel, a megadott hangszórón.

### Gemini (vagy más LLM) a Home Assistant Assistban

A **Google Generative AI** beszélgető-ügynökkel maga a Gemini válaszol.

1. A fenti szkript helyett egy olyat tegyél ki az Assist felé, amely
   **visszaadja** a mondatot. Beállítás: **Beállítások → Hangasszisztensek →
   Kitett entitások**.
2. Az ügynök a szkript leírása (`description`) alapján dönti el, mikor hívja.

```yaml
script:
  kovetkezo_50_es:
    alias: Következő 50-es villamos
    description: >-
      Élő adat: mikor indul a következő 50-es villamos a Határ út M megállóból,
      és van-e késés. Ezt használd, ha a felhasználó az 50-esről kérdez.
    sequence:
      - action: gtfs_manager.get_departures
        data:
          config_entry_id: 01JXXXXXXXXXXXXXXXXXXXXXXX
          limit: 4
        response_variable: dep
      - variables:
          result:
            speech: "{{ dep.speech }}"
            departures: "{{ dep.departures }}"
      - stop: kész
        response_variable: result
```

## A szerver API-ja (`/api/v1`)

Minden végpont GET, hitelesítés nélkül.

| Végpont | Leírás |
|---|---|
| `/api/v1/info` | API-verzió, aktív menetrendek |
| `/api/v1/feeds/{feed}/stops?q=…` vagy `?lat=…&lon=…&radius_m=…` | megálló-keresés (név, kód vagy közelség) |
| `/api/v1/feeds/{feed}/stops/{stop}` | megálló + járatai (iránnyal) |
| `/api/v1/feeds/{feed}/stops/{stop}/departures?route_id=a,b&limit=10&horizon_min=180` | következő indulások élő késéssel |

## Biztonság

- Az API csak a publikus menetrendi adatot adja, hitelesítés nélkül. A
  szerver IP-címenként percenként 120 kérést enged, efölött `429` választ ad.
- Az integráció **nem követ átirányítást**. Egyetlen kivétel a beállításkor
  történő, ugyanazon a hoszton belüli átirányítás (pl. `http` → `https`). Egy
  másik hosztra mutató átirányítást hibaként jelez.
- A szerver válaszát a HA külső adatként kezeli:
  - méretkorlát (1 MB);
  - típus- és hosszellenőrzés;
  - a vezérlő- és irányváltó karakterek kiszűrése.
- Felhasználónév/jelszó nem adható meg az URL-ben. Lehetőleg `https`
  címet használj.

## Fejlesztés

```bash
python tests/test_speech.py                          # HA nélkül, a felolvasó-szöveg
pip install pytest-homeassistant-custom-component
pytest                                               # varázsló, szenzorok, szolgáltatás
```

## Licenc

MIT
