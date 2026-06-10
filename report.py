"""
Rapport EuroMillions — exécuté chaque mercredi et samedi par GitHub Actions.
1. Charge l'historique (CSV + API pour les tirages récents)
2. Retire le dernier tirage pour ne pas biaiser les poids
3. Génère 100 combinaisons basées sur les tendances actuelles
4. Vérifie les gains contre le dernier tirage
5. Envoie un email récapitulatif via Brevo
"""
import json, csv, random, time, os, urllib.request
from pathlib import Path
from datetime import date

API   = "https://euromillions.api.pedromealha.dev/v1/draws"
CSV_F = Path(__file__).parent / "euromillions_history.csv"

PRIZE_LABELS = {
    (5,2):"Jackpot",(5,1):"Rang 2",(5,0):"Rang 3",
    (4,2):"Rang 4", (4,1):"Rang 5",(4,0):"Rang 6",
    (3,2):"Rang 7", (2,2):"Rang 8",(3,1):"Rang 9",
    (3,0):"Rang 10",(1,2):"Rang 11",(2,1):"Rang 12",
    (2,0):"Rang 13",
}

# ── Historique ───────────────────────────────────────────────────────────────

def fetch_year(year, retries=5):
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(f"{API}?year={year}", timeout=15) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(2 * (attempt + 1))
    return []

def load_history():
    draws, seen = [], set()
    with open(CSV_F, encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter=";"):
            try:
                balls = tuple(sorted(int(row[f"boule_{i}"]) for i in range(1,6)))
                stars = tuple(sorted(int(row[f"etoile_{i}"]) for i in range(1,3)))
                key = balls + stars
                if key not in seen:
                    seen.add(key)
                    draws.append({"balls": list(balls), "stars": list(stars)})
            except (ValueError, KeyError):
                continue
    current_year = date.today().year
    for year in [current_year - 1, current_year]:
        for d in fetch_year(year):
            balls = tuple(sorted(int(x) for x in d["numbers"]))
            stars = tuple(sorted(int(x) for x in d["stars"]))
            key = balls + stars
            if key not in seen:
                seen.add(key)
                draws.append({"balls": list(balls), "stars": list(stars)})
        time.sleep(0.6)
    print(f"  {len(draws)} tirages chargés")
    return draws

def fetch_last_draw():
    for year in [date.today().year, date.today().year - 1]:
        draws = fetch_year(year)
        if draws:
            return max(draws, key=lambda d: d["date"])
    raise RuntimeError("Impossible de récupérer le dernier tirage")

# ── Poids retard ─────────────────────────────────────────────────────────────

def compute_weights(draws):
    total = len(draws)
    def w_for(max_n, getter):
        w = {}
        for n in range(1, max_n + 1):
            pos = [i for i,d in enumerate(draws) if n in getter(d)]
            if len(pos) < 2: w[n] = 1.0; continue
            avg_gap    = (pos[-1] - pos[0]) / (len(pos) - 1)
            since_last = total - 1 - pos[-1]
            w[n] = max(1.0, since_last / max(1.0, avg_gap))
        return w
    return w_for(50, lambda d: d["balls"]), w_for(12, lambda d: d["stars"])

def wpick(pool, wmap):
    weights = [wmap.get(n, 1.0) for n in pool]
    r = random.random() * sum(weights)
    for n, w in zip(pool, weights):
        r -= w
        if r <= 0: return n
    return pool[-1]

# ── Génération ───────────────────────────────────────────────────────────────

def decade(n): return 0 if n <= 9 else (n - 1) // 10

def make_pair(bw, excl_decades, excl_balls):
    for _ in range(2000):
        a = wpick(list(range(1, 51)), bw)
        if a in excl_balls: continue
        diff = random.choice([1, 2])
        b = a + diff if random.random() < 0.5 else a - diff
        if b < 1 or b > 50 or b in excl_balls: continue
        if decade(a) != decade(b): continue
        d = decade(a)
        if d in excl_decades: continue
        return sorted([a, b]), d
    return None, None

def make_combo(bw, sw, hist_set, excl_balls=None):
    excl = excl_balls or set()
    for _ in range(10000):
        ab, dec_ab = make_pair(bw, set(), excl)
        if ab is None: continue
        cd, _      = make_pair(bw, {dec_ab}, excl | set(ab))
        if cd is None: continue
        pool_e = [n for n in range(1,51) if n not in excl and n not in set(ab+cd)]
        if not pool_e: continue
        e = wpick(pool_e, bw)
        balls = tuple(sorted(ab + cd + [e]))
        if len(set(balls)) != 5: continue
        sp = list(range(1, 13))
        f  = wpick(sp, sw)
        g  = wpick([s for s in sp if s != f], sw)
        stars = tuple(sorted([f, g]))
        if balls + stars not in hist_set:
            return {"balls": list(balls), "stars": list(stars), "pair1": ab, "pair2": cd}
    return None

def generate_100(draws):
    hist_set = {tuple(d["balls"] + d["stars"]) for d in draws}
    bw, sw   = compute_weights(draws)
    combos   = []
    used = set()
    for i in range(10):
        c = make_combo(bw, sw, hist_set, excl_balls=used)
        if c:
            combos.append({**c, "exclusive": True})
            used.update(c["balls"])
    for _ in range(90):
        c = make_combo(bw, sw, hist_set)
        if c:
            combos.append({**c, "exclusive": False})
    print(f"  {len(combos)} combinaisons générées")
    return combos

# ── Gains ────────────────────────────────────────────────────────────────────

def prize_amount(draw, mb, ms):
    for p in draw.get("prizes", []):
        if p["matched_numbers"] == mb and p["matched_stars"] == ms:
            return p.get("prize", 0)
    return 0

def build_report(draw, combos):
    db = [int(x) for x in draw["numbers"]]
    ds = [int(x) for x in draw["stars"]]
    rows, g_top10, g_all = [], 0, 0
    for i, c in enumerate(combos):
        mb = len(set(c["balls"]) & set(db))
        ms = len(set(c["stars"]) & set(ds))
        gain  = prize_amount(draw, mb, ms)
        label = PRIZE_LABELS.get((mb, ms), "—")
        rows.append({**c, "num": i+1, "mb": mb, "ms": ms, "gain": gain, "label": label})
        g_all += gain
        if i < 10: g_top10 += gain
    return db, ds, rows, g_top10, g_all

# ── Email HTML ────────────────────────────────────────────────────────────────

def combo_row(r):
    balls = " ".join(str(n) for n in r["balls"])
    stars = " ".join(f"★{n}" for n in r["stars"])
    col   = "#22c55e" if r["gain"] > 0 else "#9ca3af"
    tag   = "⭐" if r.get("exclusive") else f"#{r['num']}"
    gain  = f"{r['gain']:.2f} €" if r["gain"] else "—"
    return (f'<tr style="color:{col}">'
            f'<td style="padding:3px 8px">{tag}</td>'
            f'<td style="padding:3px 8px;font-family:monospace">{balls} | {stars}</td>'
            f'<td style="padding:3px 8px">{r["mb"]}B+{r["ms"]}E</td>'
            f'<td style="padding:3px 8px">{r["label"]}</td>'
            f'<td style="padding:3px 8px;font-weight:bold">{gain}</td></tr>')

def build_html(draw_date, db, ds, rows, g_top10, g_all):
    db_str = " · ".join(f"<b>{n}</b>" for n in sorted(db))
    ds_str = " · ".join(f"<b>★{n}</b>" for n in sorted(ds))
    winners = [r for r in rows if r["gain"] > 0]
    all_rows = "\n".join(combo_row(r) for r in rows)
    return f"""<html><body style="font-family:sans-serif;background:#1a1a2e;color:#fff;padding:24px">
<h1 style="color:#ffd200">&#11088; Rapport EuroMillions &#8212; {draw_date}</h1>
<p>Tirage&nbsp;: {db_str} &nbsp;|&nbsp; &#201;toiles&nbsp;: {ds_str}</p>
<h2 style="color:#ffd200;margin-top:20px">R&#233;sum&#233;</h2>
<table>
<tr><td style="padding:3px 16px 3px 0">Gagnantes sur 100 combinaisons&nbsp;:</td><td><b>{len(winners)}</b></td></tr>
<tr><td>Gagnantes sur les 10 premi&#232;res &#11088;&nbsp;:</td><td><b>{len([r for r in winners if r.get('exclusive')])}</b></td></tr>
<tr><td>Gain cumul&#233; &#8212; 10 premi&#232;res&nbsp;:</td><td><b style="color:#22c55e">{g_top10:.2f} &#8364;</b></td></tr>
<tr><td>Gain cumul&#233; &#8212; 100 combinaisons&nbsp;:</td><td><b style="color:#22c55e">{g_all:.2f} &#8364;</b></td></tr>
</table>
<h2 style="color:#ffd200;margin-top:20px">D&#233;tail</h2>
<table style="font-size:13px;border-collapse:collapse">
<thead><tr style="color:#ffd200;border-bottom:1px solid #444">
<th style="padding:3px 8px">#</th><th>Combinaison</th><th>Match</th><th>Rang</th><th>Gain</th>
</tr></thead><tbody>{all_rows}</tbody></table>
<p style="color:#6b7280;font-size:11px;margin-top:20px">
&#11088; = s&#233;lection premium (10 sans num&#233;ro commun) &#183; g&#233;n&#233;r&#233; automatiquement selon retard historique
</p></body></html>"""

def send_email(subject, html):
    api_key   = os.environ["BREVO_API_KEY"]
    mail_to   = os.environ["MAIL_TO"]
    mail_from = os.environ["MAIL_FROM"]
    payload = json.dumps({
        "sender":      {"email": mail_from},
        "to":          [{"email": mail_to}],
        "subject":     subject,
        "htmlContent": html,
    }).encode()
    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=payload,
        headers={"api-key": api_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        print(f"Email envoyé → {mail_to} (status {r.status})")

# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Chargement de l'historique...")
    draws = load_history()

    print("Récupération du dernier tirage...")
    last_draw = fetch_last_draw()
    print(f"  Tirage du {last_draw['date']} : {last_draw['numbers']} | {last_draw['stars']}")

    last_balls = tuple(sorted(int(x) for x in last_draw["numbers"]))
    last_stars = tuple(sorted(int(x) for x in last_draw["stars"]))
    draws_without_last = [d for d in draws
                          if tuple(d["balls"]) != last_balls or tuple(d["stars"]) != last_stars]
    print(f"  Historique pour génération : {len(draws_without_last)} tirages (dernier exclu)")

    print("Génération des 100 combinaisons...")
    combos = generate_100(draws_without_last)

    print("Calcul des gains...")
    db, ds, rows, g_top10, g_all = build_report(last_draw, combos)
    winners = [r for r in rows if r["gain"] > 0]
    print(f"  {len(winners)} gagnante(s) · top10={g_top10:.2f}€ · total={g_all:.2f}€")

    html    = build_html(last_draw["date"], db, ds, rows, g_top10, g_all)
    subject = f"EuroMillions {last_draw['date']} — {len(winners)} gagnante(s) · {g_all:.2f} €"
    send_email(subject, html)
