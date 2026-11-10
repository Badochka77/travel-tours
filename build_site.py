"""
Сборщик одностраничника travelbadya.github.io.

    python build_site.py          собрать index.html
    python build_site.py --dry    показать, что получится, ничего не записывая
    python build_site.py --no-prices   без запроса цен (оффлайн)

Что делает:
  * цены «от N ₽» берёт из Travelpayouts на момент сборки (токен — из .env бота,
    в публичный репозиторий не попадает);
  * даты в ссылках Aviasales подставляет браузер посетителя: если найденная дата
    уже прошла, берётся «сегодня + 14 дней» — ссылки не протухают;
  * блок программы (туры, жильё, экскурсии, страховка) выводится, ТОЛЬКО если в
    site_config.json заполнены host и promo_id — ссылка без комиссии не нужна;
  * перед записью проверяет: каждая партнёрская ссылка несёт маркер. Нет — сборка падает.
"""
from __future__ import annotations

import html
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

BASE = Path(__file__).parent
CFG = json.loads((BASE / "site_config.json").read_text(encoding="utf-8"))
MARKER = str(CFG["marker"]).strip()
if not MARKER.isdigit():
    sys.exit(f"marker должен быть числовым ID партнёра, а не токеном: {MARKER[:6]}…")

# Направления из Москвы: IATA, город, подпись
DIRECTIONS = [
    ("IST", "Стамбул", "Турция"),
    ("AYT", "Анталья", "Турция, море"),
    ("DXB", "Дубай", "ОАЭ"),
    ("EVN", "Ереван", "Армения"),
    ("TBS", "Тбилиси", "Грузия"),
    ("GYD", "Баку", "Азербайджан"),
    ("TAS", "Ташкент", "Узбекистан"),
    ("AER", "Сочи", "Россия"),
    ("KGD", "Калининград", "Россия"),
    ("MRV", "Минводы", "Россия, Кавказ"),
    ("HRG", "Хургада", "Египет, море"),
    ("SSH", "Шарм-эль-Шейх", "Египет, море"),
    ("BKK", "Бангкок", "Таиланд"),
    ("HKT", "Пхукет", "Таиланд, море"),
    ("CXR", "Нячанг", "Вьетнам, море"),
    ("MLE", "Мале", "Мальдивы"),
    ("PEK", "Пекин", "Китай"),
    ("LED", "Санкт-Петербург", "Россия"),
]
ORIGINS = [("MOW", "Москва"), ("LED", "Санкт-Петербург"), ("KZN", "Казань"),
           ("SVX", "Екатеринбург"), ("OVB", "Новосибирск"), ("KRR", "Краснодар"),
           ("ROV", "Ростов-на-Дону"), ("UFA", "Уфа"), ("KUF", "Самара"),
           ("AER", "Сочи")]

# Программы с режимом «click» (редиректор Travelpayouts). Блок появится после настройки.
PROGRAMS = {
    "level":     ("tours",     "Level.Travel", "Пакетные туры: перелёт + отель от крупных туроператоров", "https://level.travel/"),
    "travelata": ("tours",     "Travelata",    "Туры и горящие путёвки, сравнение цен туроператоров", "https://travelata.ru/"),
    "ostrovok":  ("stay",      "Островок",     "Отели и апартаменты в России и за рубежом", "https://ostrovok.ru/"),
    "sutochno":  ("stay",      "Суточно.ру",   "Квартиры и дома посуточно по России", "https://sutochno.ru/"),
    "tripster":  ("fun",       "Tripster",     "Экскурсии с местными гидами", "https://experience.tripster.ru/"),
    "kiwitaxi":  ("fun",       "Kiwitaxi",     "Трансфер из аэропорта по фиксированной цене", "https://kiwitaxi.ru/"),
    "cherehapa": ("insurance", "Черехапа",     "Сравнение туристических страховок", "https://cherehapa.ru/"),
}
GROUPS = {
    "tours": ("🏝️", "Туры: перелёт + отель", "Готовые путёвки — часто дешевле, чем собирать самому"),
    "stay": ("🏨", "Где жить", "Отели и квартиры — бронь без звонков"),
    "fun": ("🗺️", "На месте", "Экскурсии и трансфер из аэропорта"),
    "insurance": ("🛡️", "Страховка", "Для визы и на случай болезни в поездке"),
}


def shmarker(sub: str) -> str:
    sub = re.sub(r"[^a-zA-Z0-9_]", "", sub)[:40]
    return f"{MARKER}.{sub}" if sub else MARKER


def click_link(key: str, target: str, sub: str) -> str | None:
    p = CFG.get("programs", {}).get(key, {})
    host, promo = str(p.get("host", "")).strip(), str(p.get("promo_id", "")).strip()
    if not (host and promo):
        return None
    q = urlencode({"shmarker": shmarker(sub), "promo_id": promo, "source_type": "customlink",
                   "type": "click", "custom_url": target})
    return f"https://{host}/click?{q}"


# ── цены ─────────────────────────────────────────────────────────────────────

def read_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.strip().partition("=")
                env[k] = v.strip()
    return env


def fetch_prices() -> dict:
    """{IATA: (цена, 'YYYY-MM-DD')} — минимальная цена из Москвы."""
    import requests
    token = read_env((BASE / CFG["env_path"]).resolve()).get("TRAVELPAYOUTS_TOKEN", "")
    if not token:
        print("! нет TRAVELPAYOUTS_TOKEN — собираю без цен")
        return {}
    out = {}
    for iata, city, _ in DIRECTIONS:
        url = ("https://api.travelpayouts.com/v1/prices/cheap"
               f"?origin=MOW&destination={iata}&currency=rub")
        for trust_env in (True, False):  # через системный прокси, затем напрямую
            s = requests.Session()
            s.trust_env = trust_env
            try:
                r = s.get(url, headers={"X-Access-Token": token}, timeout=20)
                tickets = (r.json().get("data") or {}).get(iata) or {}
                if tickets:
                    best = min(tickets.values(), key=lambda t: t.get("price", 10**9))
                    out[iata] = (int(best["price"]), (best.get("departure_at") or "")[:10])
                break
            except Exception as e:  # сеть моргнула — пробуем без прокси
                err = type(e).__name__
        else:
            print(f"! {city}: нет цены ({err})")
    return out


# ── HTML ─────────────────────────────────────────────────────────────────────

def esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def rub(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " ₽"


def flight_cards(prices: dict, today: date) -> str:
    cards = []
    order = sorted(DIRECTIONS, key=lambda d: prices.get(d[0], (10**9,))[0])
    for iata, city, note in order:
        price, dep = prices.get(iata, (None, ""))
        price_html = (f'<span class="price">от {rub(price)}</span>' if price
                      else '<span class="price muted">смотреть цены</span>')
        when = ""
        if price and dep:
            try:
                d = datetime.strptime(dep, "%Y-%m-%d").date()
                if d > today:
                    when = f'<span class="when">вылет {d.strftime("%d.%m")}</span>'
            except ValueError:
                pass
        cards.append(
            f'<a class="card fly" data-dest="{iata}" data-dep="{esc(dep)}" data-sub="land_card_{iata.lower()}" '
            f'href="https://www.aviasales.ru/?marker={shmarker("land_card_" + iata.lower())}" '
            f'target="_blank" rel="noopener sponsored">'
            f'<span class="city">{esc(city)}</span><span class="note">{esc(note)}</span>'
            f'{price_html}{when}<span class="go">Найти билеты →</span></a>')
    return "\n".join(cards)


def program_sections() -> tuple[str, list[str]]:
    blocks, active = [], []
    for gkey, (icon, title, sub) in GROUPS.items():
        items = []
        for pkey, (grp, name, desc, target) in PROGRAMS.items():
            if grp != gkey:
                continue
            href = click_link(pkey, target, f"land_{pkey}")
            if not href:
                continue
            active.append(name)
            items.append(f'<a class="card prog" href="{esc(href)}" data-sub="land_{pkey}" target="_blank" '
                         f'rel="noopener sponsored"><span class="city">{esc(name)}</span>'
                         f'<span class="note">{esc(desc)}</span><span class="go">Перейти →</span></a>')
        if items:
            blocks.append(f'<section class="block" id="{gkey}"><h2>{icon} {esc(title)}</h2>'
                          f'<p class="lead">{esc(sub)}</p><div class="grid">{"".join(items)}</div></section>')
    return "\n".join(blocks), active


def options(pairs, selected="") -> str:
    return "".join(f'<option value="{a}"{" selected" if a == selected else ""}>{esc(b)}</option>'
                   for a, b in pairs)


def metrika(mid: str) -> str:
    if not mid.isdigit():
        return ""
    return f"""<script>(function(m,e,t,r,i,k,a){{m[i]=m[i]||function(){{(m[i].a=m[i].a||[]).push(arguments)}};
m[i].l=1*new Date();for(var j=0;j<document.scripts.length;j++){{if(document.scripts[j].src===r)return;}}
k=e.createElement(t),a=e.getElementsByTagName(t)[0],k.async=1,k.src=r,a.parentNode.insertBefore(k,a)}})
(window,document,"script","https://mc.yandex.ru/metrika/tag.js","ym");
ym({mid},"init",{{clickmap:true,trackLinks:true,accurateTrackBounce:true}});</script>
<noscript><div><img src="https://mc.yandex.ru/watch/{mid}" style="position:absolute;left:-9999px" alt=""></div></noscript>"""


CSS = """
:root{--bg:#f6f8fb;--card:#fff;--ink:#14213d;--muted:#5c677d;--line:#e3e8ef;--accent:#ff6b35;--accent-ink:#fff;--brand:#0b6efd;--soft:#eaf2ff}
@media (prefers-color-scheme:dark){:root{--bg:#0f1420;--card:#171e2d;--ink:#e8edf5;--muted:#9aa6b8;--line:#263045;--soft:#16243d}}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,"Segoe UI",Roboto,Arial,sans-serif}
a{color:inherit}.wrap{max-width:1080px;margin:0 auto;padding:0 16px}
header.top{display:flex;justify-content:space-between;align-items:center;padding:14px 16px;max-width:1080px;margin:0 auto}
.logo{font-weight:800;font-size:19px;text-decoration:none}.logo b{color:var(--accent)}
.top nav a{margin-left:14px;color:var(--muted);text-decoration:none;font-size:15px}
.hero{background:linear-gradient(135deg,#0b6efd,#3d8bff 55%,#ff8a5c);color:#fff;padding:40px 0 48px}
.hero h1{font-size:clamp(28px,5vw,44px);line-height:1.15;margin:0 0 10px}
.hero p{margin:0 0 22px;opacity:.92;font-size:18px;max-width:640px}
form.search{background:var(--card);color:var(--ink);border-radius:16px;padding:14px;display:grid;gap:10px;
grid-template-columns:repeat(auto-fit,minmax(150px,1fr));box-shadow:0 10px 30px rgba(0,0,0,.18)}
form.search label{display:flex;flex-direction:column;font-size:13px;color:var(--muted);gap:4px}
form.search select,form.search input{font:inherit;padding:10px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink);width:100%}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;background:var(--accent);color:var(--accent-ink);
border:0;border-radius:12px;padding:12px 18px;font:600 16px/1.2 inherit;cursor:pointer;text-decoration:none}
form.search .btn{align-self:end;min-height:44px}
.block{padding:40px 0 8px}.block h2{font-size:clamp(22px,3.4vw,30px);margin:0 0 6px}
.lead{color:var(--muted);margin:0 0 18px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(220px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;text-decoration:none;
display:flex;flex-direction:column;gap:2px;transition:transform .15s,box-shadow .15s}
.card:hover{transform:translateY(-2px);box-shadow:0 8px 22px rgba(20,33,61,.12)}
.city{font-weight:700;font-size:18px}.note,.when{color:var(--muted);font-size:14px}
.price{font-weight:800;font-size:20px;color:var(--accent);margin-top:6px}.price.muted{color:var(--brand);font-size:16px}
.go{margin-top:8px;color:var(--brand);font-weight:600;font-size:15px}
.stamp{font-size:13px;color:var(--muted);margin-top:10px}
.tg{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.tg .card{background:var(--soft)}
.tips{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr))}
.tips div{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px}
.tips b{display:block;margin-bottom:4px}
details{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px;margin-bottom:8px}
summary{cursor:pointer;font-weight:600}
footer{margin-top:40px;padding:24px 0 90px;border-top:1px solid var(--line);color:var(--muted);font-size:14px}
.sticky{position:fixed;left:12px;right:12px;bottom:12px;display:none;z-index:9}
@media (max-width:640px){.top nav{display:none}.sticky{display:flex}.sticky .btn{width:100%}}
"""

JS = """
(function(){
  var M=%(marker)s, YM=%(ym)s;
  function pad(n){return (n<10?'0':'')+n}
  function ddmm(d){return pad(d.getDate())+pad(d.getMonth()+1)}
  function plus(days){var d=new Date();d.setHours(0,0,0,0);d.setDate(d.getDate()+days);return d}
  function mk(sub){return M+(sub?'.'+sub.replace(/[^a-zA-Z0-9_]/g,'').slice(0,40):'')}
  function avia(o,dst,d,pax,sub){return 'https://www.aviasales.ru/search/'+o+ddmm(d)+dst+(pax||1)+'?marker='+mk(sub)}
  function goal(sub){try{if(YM)ym(YM,'reachGoal','partner_click',{sub:sub})}catch(e){}}
  var min=plus(2);
  document.querySelectorAll('a.fly').forEach(function(a){
    var d=null,s=a.getAttribute('data-dep');
    if(s){var p=s.split('-');d=new Date(+p[0],+p[1]-1,+p[2]);}
    if(!d||d<min)d=plus(14);
    a.href=avia('MOW',a.getAttribute('data-dest'),d,1,a.getAttribute('data-sub'));
  });
  document.querySelectorAll('a[data-sub]').forEach(function(a){
    a.addEventListener('click',function(){goal(a.getAttribute('data-sub'))});
  });
  var f=document.getElementById('search'),dt=document.getElementById('date');
  var t=plus(14);dt.value=t.getFullYear()+'-'+pad(t.getMonth()+1)+'-'+pad(t.getDate());
  dt.min=(function(x){return x.getFullYear()+'-'+pad(x.getMonth()+1)+'-'+pad(x.getDate())})(plus(0));
  f.addEventListener('submit',function(e){
    e.preventDefault();
    var o=f.from.value,dst=f.to.value,p=dt.value.split('-');
    if(o===dst){alert('Город вылета и прилёта совпадают');return}
    var d=new Date(+p[0],+p[1]-1,+p[2]);if(isNaN(d))d=plus(14);
    goal('land_search');
    window.open(avia(o,dst,d,f.pax.value,'land_search'),'_blank','noopener');
  });
})();
"""


def build(prices: dict) -> tuple[str, list[str]]:
    today = date.today()
    progs_html, active = program_sections()
    bot, ch = CFG["bot"], CFG["channel"]
    stamp = (f'Цены — минимальные найденные из Москвы на {today.strftime("%d.%m.%Y")}. '
             'Они меняются каждый час: точная цена — на сайте Aviasales после перехода.'
             if prices else 'Нажмите на направление — Aviasales покажет актуальные цены.')
    dest_opts = options([(i, c) for i, c, _ in DIRECTIONS], "IST")
    menu_names = {"tours": "Туры", "stay": "Жильё", "fun": "На месте", "insurance": "Страховка"}
    menu = "".join(f'<a href="#{k}">{menu_names[k]}</a>'
                   for k in GROUPS if f'id="{k}"' in progs_html)
    page = f"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Дешёвые авиабилеты и туры из Москвы — цены {today.strftime("%m.%Y")} | Travel Badya</title>
<meta name="description" content="Самые дешёвые билеты из Москвы в Стамбул, Дубай, Ереван, Тбилиси, Сочи, Анталью и ещё 12 направлений. Цены обновлены {today.strftime("%d.%m.%Y")}. Поиск по всем авиакомпаниям через Aviasales.">
<link rel="canonical" href="{esc(CFG["site_url"])}">
<meta property="og:title" content="Дешёвые авиабилеты и туры — Travel Badya">
<meta property="og:description" content="Минимальные цены на билеты по 18 популярным направлениям, обновляются регулярно.">
<meta property="og:type" content="website"><meta property="og:url" content="{esc(CFG["site_url"])}">
<meta name="theme-color" content="#0b6efd">
<style>{CSS}</style>
{metrika(str(CFG.get("metrika_id", "")).strip())}
</head><body>
<header class="top"><a class="logo" href="#">Travel <b>Badya</b></a>
<nav><a href="#flights">Билеты</a>{menu}<a href="#telegram">Telegram</a><a href="#faq">Вопросы</a></nav></header>

<section class="hero"><div class="wrap">
<h1>Дешёвые билеты и туры —<br>без переплат</h1>
<p>Ищем по всем авиакомпаниям и агентствам сразу. Цена — как на сайте продавца, покупка и билеты — у него же.</p>
<form class="search" id="search" autocomplete="off">
<label>Откуда<select name="from">{options(ORIGINS, "MOW")}</select></label>
<label>Куда<select name="to">{dest_opts}</select></label>
<label>Дата вылета<input type="date" id="date" name="date"></label>
<label>Пассажиры<select name="pax">{options([(str(n), str(n)) for n in range(1, 7)], "1")}</select></label>
<button class="btn" type="submit">Найти билеты</button>
</form></div></section>

<main class="wrap">
<section class="block" id="flights"><h2>✈️ Куда дешевле всего улететь из Москвы</h2>
<p class="lead">Сначала — самые дешёвые. Нажмите на город, откроется поиск на ближайшую выгодную дату.</p>
<div class="grid">{flight_cards(prices, today)}</div>
<p class="stamp">{esc(stamp)}</p></section>

{progs_html}

<section class="block" id="telegram"><h2>📲 Горящие цены — в Telegram</h2>
<p class="lead">Дважды в день присылаем самые дешёвые билеты и туры. Бот подберёт направление под бюджет.</p>
<div class="tg">
<a class="card" href="https://t.me/{esc(ch)}" target="_blank" rel="noopener" data-sub="land_channel"><span class="city">Канал @{esc(ch)}</span><span class="note">Подборки в 10:00 и 19:00</span><span class="go">Подписаться →</span></a>
<a class="card" href="https://t.me/{esc(bot)}?start=land" target="_blank" rel="noopener" data-sub="land_bot"><span class="city">Бот @{esc(bot)}</span><span class="note">Цены по направлению, подбор, вопросы менеджеру</span><span class="go">Открыть бота →</span></a>
</div></section>

<section class="block"><h2>💡 Как не переплатить за билет</h2>
<div class="tips">
<div><b>Сдвиньте дату на 1–3 дня</b>Будни и вылеты рано утром часто дешевле на 20–40%.</div>
<div><b>Смотрите соседние аэропорты</b>Внуково, Шереметьево и Домодедово ищутся вместе — сравнивайте все.</div>
<div><b>Проверяйте багаж</b>Самый дешёвый тариф бывает без чемодана — сравнивайте итоговую цену.</div>
<div><b>Покупайте заранее</b>На море и в праздники — за 1–2 месяца, в города — за 3–6 недель.</div>
</div></section>

<section class="block" id="faq"><h2>Частые вопросы</h2>
<details><summary>Вы турагентство?</summary><p>Нет. Мы собираем выгодные предложения в одном месте. Покупка, оплата, билеты и поддержка — на сайте продавца (Aviasales и других сервисов), куда ведут ссылки.</p></details>
<details><summary>Цена у вас выше, чем на сайте продавца?</summary><p>Нет, цена та же. Если вы покупаете по нашей ссылке, продавец платит нам комиссию из своей доли — для вас ничего не меняется.</p></details>
<details><summary>Почему после перехода цена другая?</summary><p>Авиакомпании меняют цены постоянно. На карточках — минимальная цена на дату обновления страницы; актуальная — в поиске после перехода.</p></details>
<details><summary>Где мои билеты и кому писать, если что-то пошло не так?</summary><p>Билеты приходят на почту от того сервиса, где вы оплатили. Вопросы по брони — в его поддержку: она указана в письме с билетом.</p></details>
</section>
</main>

<footer><div class="wrap">
<p>Travel Badya — подборки дешёвых билетов и туров. Сайт содержит партнёрские ссылки: при покупке по ним мы получаем комиссию от продавца, цена для вас не меняется.</p>
<p>Страница обновлена {today.strftime("%d.%m.%Y")}.</p>
</div></footer>
<div class="sticky"><a class="btn" href="#search">✈️ Найти дешёвые билеты</a></div>
<script>{JS % {"marker": json.dumps(MARKER), "ym": json.dumps(int(CFG["metrika_id"]) if str(CFG.get("metrika_id", "")).isdigit() else 0)}}</script>
</body></html>
"""
    return page, active


def self_check(page: str) -> list[str]:
    """Каждая ссылка на партнёра обязана нести маркер. Возвращает список проблем."""
    problems = []
    for href in re.findall(r'href="([^"]+)"', page):
        h = html.unescape(href)
        if "aviasales." in h and f"marker={MARKER}" not in h:
            problems.append(f"Aviasales без маркера: {h[:90]}")
        if "travelpayouts.com/click" in h and f"shmarker={MARKER}" not in h:
            problems.append(f"редиректор без маркера: {h[:90]}")
        for bad in ("partner_id=", "&partner=", "travelata.ru/?", "level.travel/?"):
            if bad in h and "travelpayouts.com" not in h:
                problems.append(f"ссылка без атрибуции ({bad}): {h[:90]}")
    token = read_env((BASE / CFG["env_path"]).resolve()).get("TRAVELPAYOUTS_TOKEN", "")
    if (token and token in page) or "TRAVELPAYOUTS_TOKEN" in page:
        problems.append("в странице следы API-токена")
    if not re.search(r"var M=\"\d+\"", page):
        problems.append("в JS нет числового маркера")
    return problems


def main() -> None:
    args = set(sys.argv[1:])
    prices = {} if "--no-prices" in args else fetch_prices()
    page, active = build(prices)
    problems = self_check(page)
    print(f"цены: {len(prices)}/{len(DIRECTIONS)} направлений")
    print("подключённые программы на сайте:", ", ".join(active) or "только Aviasales")
    print("Метрика:", "да" if str(CFG.get("metrika_id", "")).isdigit() else "нет (metrika_id пустой)")
    if problems:
        print("ОШИБКИ ПРОВЕРКИ — index.html не записан:")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    print("проверка ссылок: ок")
    if "--dry" in args:
        print(f"--dry: index.html не записан ({len(page) // 1024} КБ)")
        return
    (BASE / "index.html").write_text(page, encoding="utf-8")
    print(f"записан index.html ({len(page) // 1024} КБ)")


if __name__ == "__main__":
    main()
