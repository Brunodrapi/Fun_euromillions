"""
Rapport EuroMillions — exécuté chaque mercredi et samedi par GitHub Actions.
1. Charge l'historique (CSV + API pour les tirages récents)
2. Retire le dernier tirage pour ne pas biaiser les poids
3. Génère 100 combinaisons (modèle retard enrichi + contraintes de forme)
4. Vérifie les gains contre le dernier tirage
5. Envoie un email récapitulatif via Gmail SMTP
"""
import json, csv, random, time, os, math, smtplib, urllib.request
from pathlib import Path
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

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

# ── Poids retard enrichi (#3) ────────────────────────────────────────────────
# Fenêtre glissante 100 draws + std dev des écarts + fréquence récente

def compute_weights(draws):
    total = len(draws)
    window_size = min(100, total)
    recent = draws[-window_size:]

    def w_for(max_n, getter):
        w = {}
        for n in range(1, max_n + 1):
            positions = [i for i, d in enumerate(draws) if n in getter(d)]
            if not positions:
                w[n] = 1.0
                continue
            ago = total - 1 - positions[-1]
            if len(positions) == 1:
                w[n] = max(1.0, float(ago))
                continue
            gaps = [positions[i] - positions[i-1] for i in range(1, len(positions))]
            avg_gap = sum(gaps) / len(gaps)
            variance = sum((g - avg_gap)**2 for g in gaps) / len(gaps)
            std_bonus = 1.0 + math.sqrt(variance) / max(1.0, avg_gap) * 0.3
            recent_count = sum(1 for d in recent if n in getter(d))
            expected = window_size / max(1.0, avg_gap)
            freq_factor = max(0.5, 1.5 - recent_count / max(0.5, expected))
            retard = ago / max(1.0, avg_gap)
            w[n] = max(1.0, retard * std_bonus * freq_factor)
        return w

    bw = w_for(50, lambda d: d["balls"])
    sw = w_for(12, lambda d: d["stars"])

    # Pair co-occurrence bonus
    pair_bonus = {}
    for d in draws:
        balls = d["balls"]
        for i in range(len(balls)):
            for j in range(i+1, len(balls)):
                key = (balls[i], balls[j])
                pair_bonus[key] = pair_bonus.get(key, 0) + 1

    return bw, sw, pair_bonus

def wpick(pool, wmap):
    weights = [wmap.get(n, 1.0) for n in pool]
    r = random.random() * sum(weights)
    for n, w in zip(pool, weights):
        r -= w
        if r <= 0: return n
    return pool[-1]

# ── Contraintes de forme (#4) ────────────────────────────────────────────────

def decade(n): return 0 if n <= 9 else (n - 1) // 10

def is_valid_shape(balls):
    s = sum(balls)
    if s < 95 or s > 160:
        return False
    evens = sum(1 for n in balls if n % 2 == 0)
    if evens < 2 or evens > 3:
        return False
    decades = len({decade(n) for n in balls})
    if decades < 3:
        return False
    return True

# ── Génération ───────────────────────────────────────────────────────────────

def make_pair(bw, pair_bonus, excl_decades, excl_balls):
    for _ in range(2000):
        a = wpick(list(range(1, 51)), bw)
        if a in excl_balls:
            continue
        # Build valid b candidates with pair bias
        candidates = []
        for diff in [1, 2]:
            for b in [a + diff, a - diff]:
                if 1 <= b <= 50 and b not in excl_balls and decade(a) == decade(b):
                    candidates.append(b)
        if not candidates:
            continue
        d = decade(a)
        if d in excl_decades:
            continue
        # Weighted pick of b using pair co-occurrence
        b_weights = []
        for b in candidates:
            key = (min(a, b), max(a, b))
            b_weights.append(max(1.0, bw.get(b, 1.0)) * (1 + pair_bonus.get(key, 0) * 0.05))
        r = random.random() * sum(b_weights)
        chosen = candidates[-1]
        for b, bw_val in zip(candidates, b_weights):
            r -= bw_val
            if r <= 0:
                chosen = b
                break
        return sorted([a, chosen]), d
    return None, None

def make_combo(bw, sw, pair_bonus, hist_set, excl_balls=None):
    excl = set(excl_balls) if excl_balls else set()
    for _ in range(10000):
        ab, dec_ab = make_pair(bw, pair_bonus, set(), excl)
        if ab is None:
            continue
        cd, _ = make_pair(bw, pair_bonus, {dec_ab}, excl | set(ab))
        if cd is None:
            continue
        pool_e = [n for n in range(1, 51) if n not in excl and n not in set(ab + cd)]
        if not pool_e:
            continue
        e = wpick(pool_e, bw)
        balls = tuple(sorted(ab + cd + [e]))
        if len(set(balls)) != 5:
            continue
        # Shape constraints (#4)
        if not is_valid_shape(list(balls)):
            continue
        sp = list(range(1, 13))
        f = wpick(sp, sw)
        g = wpick([s for s in sp if s != f], sw)
        stars = tuple(sorted([f, g]))
        if balls + stars not in hist_set:
            return {"balls": list(balls), "stars": list(stars), "pair1": ab, "pair2": cd}
    return None

def generate_100(draws):
    hist_set = {tuple(d["balls"] + d["stars"]) for d in draws}
    bw, sw, pair_bonus = compute_weights(draws)
    combos = []
    used = set()
    for i in range(10):
        c = make_combo(bw, sw, pair_bonus, hist_set, excl_balls=used)
        if c:
            combos.append({**c, "exclusive": True})
            used.update(c["balls"])
    for _ in range(90):
        c = make_combo(bw, sw, pair_bonus, hist_set)
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
    stars = " ".join(f"*{n}" for n in r["stars"])
    col   = "#22c55e" if r["gain"] > 0 else "#9ca3af"
    tag   = "[P]" if r.get("exclusive") else f"#{r['num']}"
    gain  = f"{r['gain']:.2f} EUR" if r["gain"] else "-"
    return (f'<tr style="color:{col}">'
            f'<td style="padding:3px 8px">{tag}</td>'
            f'<td style="padding:3px 8px;font-family:monospace">{balls} | {stars}</td>'
            f'<td style="padding:3px 8px">{r["mb"]}B+{r["ms"]}E</td>'
            f'<td style="padding:3px 8px">{r["label"]}</td>'
            f'<td style="padding:3px 8px;font-weight:bold">{gain}</td></tr>')

def build_html(draw_date, db, ds, rows, g_top10, g_all):
    db_str = " - ".join(str(n) for n in sorted(db))
    ds_str = " - ".join(str(n) for n in sorted(ds))
    winners = [r for r in rows if r["gain"] > 0]
    all_rows = "\n".join(combo_row(r) for r in rows)
    return f"""<html><body style="font-family:sans-serif;background:#1a1a2e;color:#fff;padding:24px">
<h1 style="color:#ffd200">Rapport EuroMillions - {draw_date}</h1>
<p>Tirage : {db_str} | Etoiles : {ds_str}</p>
<p style="color:#888;font-size:12px">Modèle : retard enrichi (fenêtre 100 tirages, std dev, fréquence récente) + contraintes de forme (somme 95-160, 2-3 pairs/impairs, 3+ dizaines)</p>
<h2 style="color:#ffd200;margin-top:20px">Resume</h2>
<table>
<tr><td style="padding:3px 16px 3px 0">Gagnantes sur 100 combinaisons :</td><td><b>{len(winners)}</b></td></tr>
<tr><td>Gagnantes sur les 10 premieres [P] :</td><td><b>{len([r for r in winners if r.get('exclusive')])}</b></td></tr>
<tr><td>Gain cumule - 10 premieres :</td><td><b style="color:#22c55e">{g_top10:.2f} EUR</b></td></tr>
<tr><td>Gain cumule - 100 combinaisons :</td><td><b style="color:#22c55e">{g_all:.2f} EUR</b></td></tr>
</table>
<h2 style="color:#ffd200;margin-top:20px">Detail</h2>
<table style="font-size:13px;border-collapse:collapse">
<thead><tr style="color:#ffd200;border-bottom:1px solid #444">
<th style="padding:3px 8px">#</th><th>Combinaison</th><th>Match</th><th>Rang</th><th>Gain</th>
</tr></thead><tbody>{all_rows}</tbody></table>
<p style="color:#6b7280;font-size:11px;margin-top:20px">
[P] = selection premium (10 sans numero commun) - genere automatiquement selon retard historique enrichi
</p></body></html>"""

def send_email(subject, html):
    mail_from = os.environ["MAIL_FROM"]
    mail_to   = os.environ["MAIL_TO"]
    password  = os.environ["MAIL_PASSWORD"]
    smtp_host = os.environ.get("MAIL_SMTP", "smtp.gmail.com")
    smtp_port = int(os.environ.get("MAIL_PORT", "587"))

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = mail_from
    msg["To"]      = mail_to
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(smtp_host, smtp_port) as s:
        s.starttls()
        s.login(mail_from, password)
        s.sendmail(mail_from, mail_to, msg.as_string())
    print(f"Email envoye -> {mail_to}")

# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Chargement de l'historique...")
    draws = load_history()

    print("Recuperation du dernier tirage...")
    last_draw = fetch_last_draw()
    print(f"  Tirage du {last_draw['date']} : {last_draw['numbers']} | {last_draw['stars']}")

    last_balls = tuple(sorted(int(x) for x in last_draw["numbers"]))
    last_stars = tuple(sorted(int(x) for x in last_draw["stars"]))
    draws_without_last = [d for d in draws
                          if tuple(d["balls"]) != last_balls or tuple(d["stars"]) != last_stars]
    print(f"  Historique pour generation : {len(draws_without_last)} tirages (dernier exclu)")

    print("Generation des 100 combinaisons...")
    combos = generate_100(draws_without_last)

    print("Calcul des gains...")
    db, ds, rows, g_top10, g_all = build_report(last_draw, combos)
    winners = [r for r in rows if r["gain"] > 0]
    print(f"  {len(winners)} gagnante(s) - top10={g_top10:.2f}EUR - total={g_all:.2f}EUR")

    html    = build_html(last_draw["date"], db, ds, rows, g_top10, g_all)
    subject = f"EuroMillions {last_draw['date']} - {len(winners)} gagnante(s) - {g_all:.2f} EUR"
    send_email(subject, html)
