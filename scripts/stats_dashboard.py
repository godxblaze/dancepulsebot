"""HTML-дашборд статистики школ — самодостаточный файл, открывается в браузере.

Дополняет текстовые /stats и scripts/dev_stats.py визуальным разрезом:
тренд «обещали → пришли» по занятиям, баланс пар 🕺/💃, матрица посещаемости
учеников. Ничего не пишет в базу; Mini App не нужен — файл локальный.

Запуск:
    python -m scripts.stats_dashboard                 # реальная БД, все школы
    python -m scripts.stats_dashboard --demo          # сэмпл-данные (посмотреть вид)
    python -m scripts.stats_dashboard --out d.html --no-open

Палитра — валидированная dataviz-палитра (CVD-safe по-чартовым парам):
обещали=синий, пришли=aqua, 🕺=синий, 💃=magenta; статусы отдельно.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import webbrowser
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path

LESSONS_LIMIT = 12   # последних занятий в графиках/матрице
TILE_WINDOW = timedelta(weeks=4)  # окно stat-плиток, как /devstats
MATRIX_MAX_ROWS = 30


# ── данные ───────────────────────────────────────────────────────────────
@dataclass
class LessonStat:
    label: str                 # "25.06"
    total: int                 # участников опроса
    promised: int              # ответили «приду»
    attended: int | None       # реально пришли (None — не отмечали)
    male_yes: int
    female_yes: int


@dataclass
class MatrixRow:
    name: str
    cells: list[str]           # коды: v=пришёл, x=no-show, p=обещал(не отмечено),
                               # n=«не приду», s=молчал, w=пришёл без обещания


@dataclass
class GroupDash:
    name: str
    lessons: list[LessonStat]
    matrix: list[MatrixRow]
    matrix_truncated: int = 0  # скрыто строк сверх MATRIX_MAX_ROWS


@dataclass
class SchoolDash:
    name: str
    tiles: dict[str, str]      # label -> value (уже отформатировано)
    groups: list[GroupDash] = field(default_factory=list)


# ── загрузка из БД ───────────────────────────────────────────────────────
async def load_from_db() -> list[SchoolDash]:
    from sqlalchemy import func, select

    from app.db.models import Answer, Gender, Group, Person, Response, School
    from app.db.session import init_db, session_scope
    from app.services import devstats as devstats_svc
    from app.services import surveys as surveys_svc
    from app.services.schedule import format_lesson_at

    await init_db()
    since = datetime.now(UTC) - TILE_WINDOW
    out: list[SchoolDash] = []

    async with session_scope() as s:
        schools = (await s.execute(select(School).order_by(School.id))).scalars().all()
        for school in schools:
            row = await devstats_svc.school_row(s, school, since)
            active_persons = await devstats_svc.active_persons(s, school.id, since)

            def _pct(part, whole) -> str:
                return f"{round(100 * (part or 0) / whole)}%" if whole else "—"

            dash = SchoolDash(
                name=school.name,
                tiles={
                    "Отклик (обещали)": _pct(row["yes"], row["responded"]),
                    "Реальная посещаемость": _pct(row["attended"], row["marked"]),
                    "No-show": _pct(row["noshow"], row["marked"]),
                    "Активных учеников": str(active_persons),
                    "Зависших доборов": str(row["stuck_calls"]),
                },
            )

            groups = (await s.execute(
                select(Group)
                .where(Group.school_id == school.id, Group.active.is_(True))
                .order_by(Group.name)
            )).scalars().all()
            for g in groups:
                closed = await surveys_svc.recent_closed_surveys(
                    s, g.id, limit=LESSONS_LIMIT
                )
                if not closed:
                    continue
                closed = list(reversed(closed))  # старые → новые
                lessons: list[LessonStat] = []
                # person_id -> {survey_id: (answer, attended)}
                per_person: dict[int, dict[int, tuple]] = {}
                names: dict[int, str] = {}
                for survey in closed:
                    summary = await surveys_svc.summarize(s, survey.id)
                    by_gender = dict((await s.execute(
                        select(Person.gender, func.count())
                        .select_from(Response)
                        .join(Person, Response.person_id == Person.id)
                        .where(
                            Response.survey_id == survey.id,
                            Response.answer == Answer.yes,
                        )
                        .group_by(Person.gender)
                    )).all())
                    lessons.append(LessonStat(
                        label=format_lesson_at(survey.lesson_at, "%d.%m"),
                        total=summary.total,
                        promised=summary.yes,
                        attended=(
                            summary.actual_attended
                            if summary.actual_marked else None
                        ),
                        male_yes=by_gender.get(Gender.male, 0),
                        female_yes=by_gender.get(Gender.female, 0),
                    ))
                    rows = (await s.execute(
                        select(Person.id, Person.full_name,
                               Response.answer, Response.actually_attended)
                        .select_from(Response)
                        .join(Person, Response.person_id == Person.id)
                        .where(Response.survey_id == survey.id)
                    )).all()
                    for pid, name, answer, att in rows:
                        names[pid] = name
                        per_person.setdefault(pid, {})[survey.id] = (answer, att)

                matrix = []
                for pid in sorted(names, key=lambda p: names[p].lower()):
                    cells = [
                        _cell_code(*per_person[pid][sv.id])
                        if sv.id in per_person[pid] else "s"
                        for sv in closed
                    ]
                    matrix.append(MatrixRow(name=names[pid], cells=cells))
                truncated = max(0, len(matrix) - MATRIX_MAX_ROWS)
                dash.groups.append(GroupDash(
                    name=g.name,
                    lessons=lessons,
                    matrix=matrix[:MATRIX_MAX_ROWS],
                    matrix_truncated=truncated,
                ))
            out.append(dash)
    return out


def _cell_code(answer, attended) -> str:
    from app.db.models import Answer
    promised = answer == Answer.yes
    if attended is True:
        return "v" if promised else "w"
    if attended is False and promised:
        return "x"
    if promised:
        return "p"
    if answer is not None:
        return "n"
    return "s"


# ── сэмпл-данные (посмотреть дизайн без БД) ─────────────────────────────
def demo_data() -> list[SchoolDash]:
    rng = random.Random(42)
    school = SchoolDash(
        name="Студия «Ритм» (демо)",
        tiles={
            "Отклик (обещали)": "68%",
            "Реальная посещаемость": "84%",
            "No-show": "9%",
            "Активных учеников": "47",
            "Зависших доборов": "1",
        },
    )
    day0 = datetime(2026, 4, 20, tzinfo=UTC)
    for gname, size, step in (
        ("Бачата · вт·чт", 12, 3), ("Соушл · сб", 16, 7), ("Хастл · пн", 9, 7),
    ):
        lessons: list[LessonStat] = []
        for i in range(10):
            promised = rng.randint(size // 2, size - 1)
            attended = max(0, promised - rng.choice((0, 0, 1, 1, 2)))
            male = rng.randint(promised // 4, promised // 2)
            lessons.append(LessonStat(
                label=(day0 + timedelta(days=step * i)).strftime("%d.%m"),
                total=size,
                promised=promised,
                attended=attended if i < 9 else None,  # свежее — не отмечено
                male_yes=male,
                female_yes=promised - male,
            ))
        matrix = []
        first = ["Аня", "Боря", "Вика", "Глеб", "Дана", "Ева", "Женя", "Зара",
                 "Ира", "Кира", "Лера", "Марк", "Ната", "Олег", "Пётр", "Рита"]
        for name in first[:size]:
            cells = [
                rng.choices("vxpnsw", weights=(55, 6, 8, 14, 14, 3))[0]
                for _ in range(10)
            ]
            matrix.append(MatrixRow(name=name, cells=cells))
        school.groups.append(GroupDash(name=gname, lessons=lessons, matrix=matrix))
    return [school]


# ── SVG-примитивы ────────────────────────────────────────────────────────
# Размеры line-чарта: viewBox 560×200, плот-зона с полями под оси/подписи.
W, H = 560, 200
PAD_L, PAD_R, PAD_T, PAD_B = 34, 46, 12, 24


def _xy(i: int, n: int, pct: float) -> tuple[float, float]:
    x = PAD_L + (W - PAD_L - PAD_R) * (i / max(1, n - 1))
    y = PAD_T + (H - PAD_T - PAD_B) * (1 - pct / 100)
    return round(x, 1), round(y, 1)


def _polyline(points: list[tuple[float, float]], var: str) -> str:
    if len(points) == 1:
        x, y = points[0]
        return (f'<circle cx="{x}" cy="{y}" r="4" style="fill:var({var})" '
                f'stroke="var(--surface)" stroke-width="2"/>')
    pts = " ".join(f"{x},{y}" for x, y in points)
    return (f'<polyline points="{pts}" fill="none" style="stroke:var({var})" '
            f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')


def _trend_svg(lessons: list[LessonStat]) -> str:
    """2 серии: обещали % и пришли % (от размера группы) по занятиям."""
    n = len(lessons)
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" '
             f'aria-label="Тренд отклика по занятиям">']
    # сетка 0/50/100 + подписи оси Y
    for pct in (0, 50, 100):
        _, y = _xy(0, n, pct)
        parts.append(f'<line x1="{PAD_L}" y1="{y}" x2="{W - PAD_R}" y2="{y}" '
                     f'stroke="var(--grid)" stroke-width="1"/>')
        parts.append(f'<text x="{PAD_L - 6}" y="{y + 3}" text-anchor="end" '
                     f'class="tick">{pct}</text>')
    promised_pts, attended_pts = [], []
    for i, les in enumerate(lessons):
        p_pct = 100 * les.promised / les.total if les.total else 0
        promised_pts.append(_xy(i, n, p_pct))
        if les.attended is not None and les.total:
            attended_pts.append((i, _xy(i, n, 100 * les.attended / les.total)))
        x, _ = _xy(i, n, 0)
        if n <= 12:
            parts.append(f'<text x="{x}" y="{H - 6}" text-anchor="middle" '
                         f'class="tick">{escape(les.label)}</text>')
    parts.append(_polyline(promised_pts, "--s-promised"))
    if attended_pts:
        parts.append(_polyline([xy for _, xy in attended_pts], "--s-attended"))
    # маркеры + hover-мишени (невидимые широкие колонки)
    for x, y in promised_pts:
        parts.append(f'<circle cx="{x}" cy="{y}" r="4" style="fill:var(--s-promised)" '
                     f'stroke="var(--surface)" stroke-width="2"/>')
    for _i, xy in attended_pts:
        parts.append(f'<circle cx="{xy[0]}" cy="{xy[1]}" r="4" '
                     f'style="fill:var(--s-attended)" '
                     f'stroke="var(--surface)" stroke-width="2"/>')
    slot_w = (W - PAD_L - PAD_R) / max(1, n - 1)
    for i, les in enumerate(lessons):
        x, _ = _xy(i, n, 0)
        came = "—" if les.attended is None else str(les.attended)
        tip = (f"{les.label} · обещали {les.promised}/{les.total} · пришли {came}")
        parts.append(
            f'<rect x="{x - slot_w / 2}" y="{PAD_T}" width="{slot_w}" '
            f'height="{H - PAD_T - PAD_B}" fill="transparent" '
            f'data-tip="{escape(tip, quote=True)}"/>'
        )
    # прямые подписи на концах серий
    lx, ly = promised_pts[-1]
    last = lessons[-1]
    p_pct = round(100 * last.promised / last.total) if last.total else 0
    parts.append(f'<text x="{lx + 8}" y="{ly + 3}" class="endlbl">{p_pct}%</text>')
    if attended_pts:
        i, (ax, ay) = attended_pts[-1]
        a_les = lessons[i]
        a_pct = round(100 * (a_les.attended or 0) / a_les.total) if a_les.total else 0
        if abs(ay - ly) < 12:
            ay += 14 if ay >= ly else -10  # не наезжать на подпись первой серии
        parts.append(f'<text x="{ax + 8}" y="{ay + 3}" class="endlbl">{a_pct}%</text>')
    parts.append("</svg>")
    return "".join(parts)


BW, BH, BMID = 560, 132, 66  # диаграмма баланса: зеркальные колонки


def _balance_svg(lessons: list[LessonStat]) -> str:
    n = len(lessons)
    peak = max((max(les.male_yes, les.female_yes) for les in lessons), default=1) or 1
    scale = (BMID - 18) / peak
    slot_w = (BW - PAD_L - PAD_R) / max(1, n)
    bar_w = min(24, slot_w - 8)
    parts = [f'<svg viewBox="0 0 {BW} {BH}" role="img" '
             f'aria-label="Баланс пар по занятиям">']
    parts.append(f'<line x1="{PAD_L}" y1="{BMID}" x2="{BW - PAD_R}" y2="{BMID}" '
                 f'stroke="var(--axis)" stroke-width="1"/>')
    worst_i = max(
        range(n), key=lambda i: abs(lessons[i].male_yes - lessons[i].female_yes),
        default=0,
    )
    for i, les in enumerate(lessons):
        cx = PAD_L + slot_w * (i + 0.5)
        x = cx - bar_w / 2
        mh, fh = les.male_yes * scale, les.female_yes * scale
        tip = f"{les.label} · 🕺 {les.male_yes} · 💃 {les.female_yes}"
        t = escape(tip, quote=True)
        if mh:
            parts.append(
                f'<path d="M{x},{BMID} v{-max(0, mh - 4)} q0,-4 4,-4 h{bar_w - 8} '
                f'q4,0 4,4 v{max(0, mh - 4)} z" style="fill:var(--s-male)" '
                f'data-tip="{t}"/>'
            )
        if fh:
            parts.append(
                f'<path d="M{x},{BMID + 2} v{max(0, fh - 4)} q0,4 4,4 h{bar_w - 8} '
                f'q4,0 4,-4 v{-max(0, fh - 4)} z" style="fill:var(--s-female)" '
                f'data-tip="{t}"/>'
            )
        if i == worst_i and les.male_yes != les.female_yes:
            diff = les.male_yes - les.female_yes
            who = "🕺" if diff > 0 else "💃"
            parts.append(f'<text x="{cx}" y="12" text-anchor="middle" '
                         f'class="endlbl">+{abs(diff)} {who}</text>')
    parts.append("</svg>")
    return "".join(parts)


# ── HTML ────────────────────────────────────────────────────────────────
_CELL = {
    "v": ("c-v", "пришёл"),
    "w": ("c-w", "пришёл без обещания"),
    "x": ("c-x", "no-show (обещал, не пришёл)"),
    "p": ("c-p", "обещал, явка не отмечена"),
    "n": ("c-n", "ответил «не приду»"),
    "s": ("c-s", "молчал"),
}


def _matrix_html(g: GroupDash) -> str:
    head = "".join(
        f"<th>{escape(les.label)}</th>" for les in g.lessons
    )
    rows = []
    for row in g.matrix:
        cells = []
        for code, les in zip(row.cells, g.lessons, strict=False):
            cls, label = _CELL[code]
            tip = f"{escape(row.name)} · {escape(les.label)} — {label}"
            cells.append(f'<td><span class="cell {cls}" '
                         f'data-tip="{tip}"></span></td>')
        rows.append(f"<tr><th>{escape(row.name)}</th>{''.join(cells)}</tr>")
    note = (f'<p class="note">…и ещё {g.matrix_truncated} — см. 📥 Экспорт CSV</p>'
            if g.matrix_truncated else "")
    return (
        f'<div class="matrix-scroll"><table class="matrix">'
        f"<thead><tr><th></th>{head}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>{note}"
    )


def _group_card(g: GroupDash) -> str:
    legend_trend = (
        '<span class="key"><i style="background:var(--s-promised)"></i>обещали</span>'
        '<span class="key"><i style="background:var(--s-attended)"></i>пришли</span>'
    )
    legend_bal = (
        '<span class="key"><i style="background:var(--s-male)"></i>🕺 парни</span>'
        '<span class="key"><i style="background:var(--s-female)"></i>💃 девушки</span>'
    )
    legend_matrix = "".join(
        f'<span class="key"><span class="cell {cls}"></span>{label}</span>'
        for cls, label in _CELL.values()
    )
    def _head(title: str, legend: str) -> str:
        return (f'<div class="chart-head"><h3>{title}</h3>'
                f'<div class="legend">{legend}</div></div>')

    return (
        f'<section class="card"><h2>{escape(g.name)}</h2>'
        + _head("Отклик, % группы", legend_trend)
        + _trend_svg(g.lessons)
        + _head("Баланс пар среди «приду»", legend_bal)
        + _balance_svg(g.lessons)
        + _head("Ученики × занятия", legend_matrix)
        + _matrix_html(g)
        + "</section>"
    )


def render_html(schools: list[SchoolDash], generated_at: datetime) -> str:
    body = []
    for school in schools:
        tiles = "".join(
            f'<div class="tile"><div class="lbl">{escape(k)}</div>'
            f'<div class="val">{escape(v)}</div></div>'
            for k, v in school.tiles.items()
        )
        groups = "".join(_group_card(g) for g in school.groups) or (
            '<p class="note">Нет завершённых опросов — графики появятся '
            "после первых занятий.</p>"
        )
        body.append(
            f'<h1>💓 {escape(school.name)}</h1>'
            f'<div class="tiles">{tiles}</div>'
            f'<div class="grid">{groups}</div>'
        )
    when = generated_at.strftime("%d.%m.%Y %H:%M")
    joined = "".join(body) or "<h1>💓 Dance Pulse</h1><p class='note'>Школ пока нет.</p>"
    return _PAGE.replace("{{BODY}}", joined).replace("{{WHEN}}", when)


_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dance Pulse — статистика</title>
<style>
:root{
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7;
  --border:rgba(11,11,11,0.10);
  --s-promised:#2a78d6; --s-attended:#1baf7a;
  --s-male:#2a78d6; --s-female:#e87ba4;
  --bad:#d03b3b; --good:#0ca30c;
}
@media (prefers-color-scheme: dark){:root{
  --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --axis:#383835;
  --border:rgba(255,255,255,0.10);
  --s-promised:#3987e5; --s-attended:#199e70;
  --s-male:#3987e5; --s-female:#d55181;
  --bad:#d03b3b; --good:#0ca30c;
}}
*{box-sizing:border-box}
body{margin:0;padding:28px 24px 60px;background:var(--page);color:var(--ink);
  font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
h1{font-size:22px;margin:8px 0 14px}
h2{font-size:16px;margin:0 0 4px}
h3{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;
  color:var(--muted);margin:0}
.meta{color:var(--muted);font-size:12px;margin:0 0 4px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:10px;margin:0 0 22px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:10px;
  padding:10px 14px}
.tile .lbl{font-size:12px;color:var(--ink2)}
.tile .val{font-size:26px;font-weight:600;margin-top:2px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:18px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;
  padding:16px 18px;min-width:0}
.chart-head{display:flex;justify-content:space-between;align-items:baseline;
  gap:12px;margin:14px 0 4px;flex-wrap:wrap}
.legend{display:flex;gap:12px;flex-wrap:wrap}
.key{display:inline-flex;align-items:center;gap:5px;font-size:11px;color:var(--ink2)}
.key i{width:10px;height:10px;border-radius:3px;display:inline-block}
svg{width:100%;height:auto;display:block}
.tick{font-size:10px;fill:var(--muted)}
.endlbl{font-size:11px;font-weight:600;fill:var(--ink2)}
.matrix-scroll{overflow-x:auto;margin-top:4px}
.matrix{border-collapse:collapse;font-variant-numeric:tabular-nums}
.matrix th{font-size:10px;color:var(--muted);font-weight:500;text-align:left;
  padding:1px 6px 1px 0;white-space:nowrap}
.matrix thead th{text-align:center;padding:0 2px 3px}
.matrix td{padding:1px 2px;text-align:center}
.cell{display:inline-block;width:13px;height:13px;border-radius:4px}
.c-v{background:var(--s-attended)}
.c-w{background:transparent;border:2px solid var(--s-attended)}
.c-x{background:var(--bad)}
.c-p{background:transparent;border:2px solid var(--s-promised)}
.c-n{background:transparent;border:2px solid var(--axis)}
.c-s{background:var(--grid)}
.note{color:var(--muted);font-size:12px}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--page);
  font-size:12px;padding:4px 9px;border-radius:6px;opacity:0;z-index:9;
  transition:opacity .08s;max-width:340px}
footer{margin-top:34px;color:var(--muted);font-size:12px}
</style></head><body>
<p class="meta">Dance Pulse · дашборд статистики · сгенерирован {{WHEN}} ·
плитки — окно 4 недели, графики — последние занятия</p>
{{BODY}}
<div id="tip" role="status"></div>
<footer>python -m scripts.stats_dashboard · read-only, база не изменяется</footer>
<script>
const tip = document.getElementById('tip');
document.addEventListener('mouseover', e => {
  const t = e.target.closest('[data-tip]');
  if (!t) { tip.style.opacity = 0; return; }
  tip.textContent = t.dataset.tip;
  tip.style.opacity = 1;
});
document.addEventListener('mousemove', e => {
  if (tip.style.opacity === '0') return;
  const pad = 14;
  let x = e.clientX + pad, y = e.clientY + pad;
  const r = tip.getBoundingClientRect();
  if (x + r.width > innerWidth - 8) x = e.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = e.clientY - r.height - pad;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
});
</script>
</body></html>
"""


# ── CLI ─────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="HTML-дашборд статистики Dance Pulse")
    ap.add_argument("--demo", action="store_true", help="сэмпл-данные вместо БД")
    ap.add_argument("--out", default="stats_dashboard.html", help="путь HTML-файла")
    ap.add_argument("--no-open", action="store_true", help="не открывать браузер")
    args = ap.parse_args()

    schools = demo_data() if args.demo else asyncio.run(load_from_db())
    html = render_html(schools, datetime.now())
    out = Path(args.out)
    out.write_text(html, encoding="utf-8")
    print(f"dashboard: {out.resolve()}")
    if not args.no_open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
