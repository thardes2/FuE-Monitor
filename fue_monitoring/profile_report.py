# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Per-company reports, as Markdown and as a self-contained HTML page.

Both are generated from the same match data rather than converting one into the
other, so neither format is a second-class rendering of the other.

Eligibility and the supporting quote are shown prominently and honestly: a
judgement of "nein"/"unklar" carries its reason, and a quote the model couldn't
be shown to have taken from the call text is labelled as unverified instead of
being presented as evidence.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from .matching import Match
from .profiles import Profile

ELIGIBILITY_LABEL = {
    "ja": "förderfähig",
    "nein": "nicht förderfähig",
    "unklar": "unklar",
}


def _deadline(match: Match) -> str:
    if not match.call.deadline:
        return "unbekannt"
    label = match.call.deadline.strftime("%d.%m.%Y")
    if match.call.deadline_status == "urgent":
        return f"{label} (dringend)"
    return label


def _themes(match: Match) -> str:
    return ", ".join(f"{b} ({g})" for b, g in match.hits) or "—"


def _summary(matches: list[Match]) -> dict:
    return {
        "total": len(matches),
        "eligible": sum(1 for m in matches if m.foerderfaehig == "ja"),
        "unclear": sum(1 for m in matches if m.foerderfaehig == "unklar"),
        "unverified": sum(1 for m in matches if m.fundstelle and not m.fundstelle_belegt),
    }


FRAMEWORK_HEADING = "Rahmenbekanntmachungen (kein direkter Antrag)"
FRAMEWORK_NOTE = (
    "Auf diese Dokumente kann nicht direkt beantragt werden — sie kündigen separate "
    "Förderaufrufe an, die die eigentlichen Fristen tragen. Sie sind deshalb nicht "
    "bewertet, aber thematisch passend, und es lohnt sich, die Folgeaufrufe zu beobachten."
)


def render_markdown(
    profile: Profile,
    matches: list[Match],
    generated_at: datetime | None = None,
    frameworks: list | None = None,
) -> str:
    generated_at = generated_at or datetime.now()
    s = _summary(matches)

    lines = [
        f"# Förderausschreibungen für {profile.name}",
        "",
        f"Stand: {generated_at.strftime('%d.%m.%Y %H:%M')}  ",
        f"Profil: `{profile.id}` · {profile.groesse.upper()}"
        + (f" · {profile.rolle}" if profile.rolle else ""),
    ]
    if profile.sicp_ansprechpartner:
        lines.append(f"Ansprechpartner SICP: {profile.sicp_ansprechpartner}")

    lines += [
        "",
        f"**{s['total']} Treffer** — davon {s['eligible']} als förderfähig bewertet, "
        f"{s['unclear']} unklar.",
    ]
    if s["unverified"]:
        lines.append(
            f"> Hinweis: Bei {s['unverified']} Treffer(n) konnte das zitierte Belegfundstück "
            "nicht im Ausschreibungstext wiedergefunden werden. Diese sind unten als "
            "*unbelegt* markiert und sollten vor Weitergabe geprüft werden."
        )

    def framework_section() -> list[str]:
        if not frameworks:
            return []
        out = ["", f"## {FRAMEWORK_HEADING}", "", FRAMEWORK_NOTE, ""]
        for call in frameworks:
            out.append(f"- [{call.title.replace('|', chr(92) + '|')}]({call.link}) — {call.source}")
        return out

    if not matches:
        lines += ["", "Keine Treffer für dieses Profil."]
        return "\n".join(lines + framework_section()) + "\n"

    lines += [
        "",
        "## Übersicht",
        "",
        "| Score | Ausschreibung | Frist | Förderquote | Förderfähig |",
        "|---:|---|---|---|---|",
    ]
    for m in matches:
        title = m.call.title.replace("|", "\\|")
        lines.append(
            f"| {m.score} | [{title}]({m.call.link}) | {_deadline(m)} | "
            f"{m.call.funding_rate or '—'} | {ELIGIBILITY_LABEL[m.foerderfaehig]} |"
        )

    lines += ["", "## Details", ""]
    for m in matches:
        lines += [
            f"### {m.score} — {m.call.title}",
            "",
            f"- **Quelle:** {m.call.source}",
            f"- **Frist:** {_deadline(m)}",
            f"- **Förderquote:** {m.call.funding_rate or '—'}",
            f"- **Treffer-Themen:** {_themes(m)}",
            f"- **Förderfähigkeit:** {ELIGIBILITY_LABEL[m.foerderfaehig]}"
            + (f" — {m.foerderfaehig_grund}" if m.foerderfaehig_grund else ""),
            f"- **Link:** {m.call.link}",
        ]
        if m.review:
            lines.append(f"- **Status:** {m.review}")
        lines += ["", m.begruendung or "_Keine Begründung geliefert._", ""]
        if m.fundstelle:
            marker = " *(unbelegt — nicht im Ausschreibungstext gefunden)*" if not m.fundstelle_belegt else ""
            lines += [f"> {m.fundstelle}{marker}", ""]

    return "\n".join(lines + framework_section()) + "\n"


_CSS = """
:root { color-scheme: light dark; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
       max-width: 60rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
h1 { margin-bottom: .2rem; }
.meta { color: #666; font-size: .9rem; margin-bottom: 1.5rem; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
th, td { border-bottom: 1px solid #ddd; padding: .5rem .6rem; text-align: left; vertical-align: top; }
th { background: #f4f4f4; }
.score { font-weight: 700; text-align: right; white-space: nowrap; }
.s-high { color: #1a7f37; } .s-mid { color: #9a6700; } .s-low { color: #777; }
.badge { display: inline-block; padding: .05rem .45rem; border-radius: .6rem; font-size: .8rem; }
.b-ja { background: #d9ead3; } .b-nein { background: #f4cccc; } .b-unklar { background: #efefef; }
.call { border-top: 1px solid #ddd; padding-top: 1rem; margin-top: 1.5rem; }
blockquote { border-left: 3px solid #ccc; margin: .6rem 0; padding: .2rem .8rem; color: #444; }
.warn { background: #fce8b2; padding: .6rem .8rem; border-radius: .3rem; }
.unverified { color: #9a6700; font-style: italic; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: .2rem .8rem; margin: .4rem 0; }
dt { color: #666; } dd { margin: 0; }
@media (prefers-color-scheme: dark) {
  body { background: #1b1b1b; color: #e6e6e6; }
  th { background: #2a2a2a; } th, td, .call, blockquote { border-color: #444; }
  .meta, dt, blockquote { color: #aaa; }
  .b-ja { background: #22442a; } .b-nein { background: #4a2626; } .b-unklar { background: #333; }
  .warn { background: #4a3c17; }
}
"""


def _score_class(score: int) -> str:
    if score >= 70:
        return "s-high"
    return "s-mid" if score >= 40 else "s-low"


def render_html(
    profile: Profile,
    matches: list[Match],
    generated_at: datetime | None = None,
    frameworks: list | None = None,
) -> str:
    generated_at = generated_at or datetime.now()
    s = _summary(matches)
    e = html.escape

    parts = [
        "<!doctype html>",
        '<html lang="de"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>Förderausschreibungen — {e(profile.name)}</title>",
        f"<style>{_CSS}</style></head><body>",
        f"<h1>Förderausschreibungen für {e(profile.name)}</h1>",
        f'<div class="meta">Stand: {generated_at.strftime("%d.%m.%Y %H:%M")} · '
        f"Profil <code>{e(profile.id)}</code> · {e(profile.groesse.upper())}"
        + (f" · {e(profile.rolle)}" if profile.rolle else "")
        + (f" · Ansprechpartner: {e(profile.sicp_ansprechpartner)}" if profile.sicp_ansprechpartner else "")
        + "</div>",
        f"<p><strong>{s['total']} Treffer</strong> — davon {s['eligible']} als förderfähig bewertet, "
        f"{s['unclear']} unklar.</p>",
    ]

    if s["unverified"]:
        parts.append(
            f'<p class="warn">Bei {s["unverified"]} Treffer(n) konnte das zitierte Belegfundstück nicht '
            "im Ausschreibungstext wiedergefunden werden. Diese sind unten als <em>unbelegt</em> "
            "markiert und sollten vor Weitergabe geprüft werden.</p>"
        )

    def framework_block() -> list[str]:
        if not frameworks:
            return []
        out = [f"<h2>{e(FRAMEWORK_HEADING)}</h2>", f'<p class="meta">{e(FRAMEWORK_NOTE)}</p>', "<ul>"]
        for call in frameworks:
            out.append(
                f'<li><a href="{e(call.link)}" target="_blank">{e(call.title)}</a> — {e(call.source)}</li>'
            )
        out.append("</ul>")
        return out

    if not matches:
        parts.append("<p>Keine Treffer für dieses Profil.</p>")
        return "\n".join(parts + framework_block() + ["</body></html>"])

    parts += [
        "<h2>Übersicht</h2><table><thead><tr><th>Score</th><th>Ausschreibung</th>"
        "<th>Frist</th><th>Förderquote</th><th>Förderfähig</th></tr></thead><tbody>"
    ]
    for m in matches:
        parts.append(
            f'<tr><td class="score {_score_class(m.score)}">{m.score}</td>'
            f'<td><a href="{e(m.call.link)}" target="_blank">{e(m.call.title)}</a></td>'
            f"<td>{e(_deadline(m))}</td><td>{e(m.call.funding_rate or '—')}</td>"
            f'<td><span class="badge b-{m.foerderfaehig}">{ELIGIBILITY_LABEL[m.foerderfaehig]}</span></td></tr>'
        )
    parts.append("</tbody></table><h2>Details</h2>")

    for m in matches:
        eligibility = ELIGIBILITY_LABEL[m.foerderfaehig]
        if m.foerderfaehig_grund:
            eligibility += f" — {e(m.foerderfaehig_grund)}"
        parts += [
            '<div class="call">',
            f'<h3><span class="score {_score_class(m.score)}">{m.score}</span> — {e(m.call.title)}</h3>',
            "<dl>",
            f"<dt>Quelle</dt><dd>{e(m.call.source)}</dd>",
            f"<dt>Frist</dt><dd>{e(_deadline(m))}</dd>",
            f"<dt>Förderquote</dt><dd>{e(m.call.funding_rate or '—')}</dd>",
            f"<dt>Treffer-Themen</dt><dd>{e(_themes(m))}</dd>",
            f'<dt>Förderfähigkeit</dt><dd><span class="badge b-{m.foerderfaehig}">{eligibility}</span></dd>',
        ]
        if m.review:
            parts.append(f"<dt>Status</dt><dd>{e(m.review)}</dd>")
        parts += [
            f'<dt>Link</dt><dd><a href="{e(m.call.link)}" target="_blank">{e(m.call.link)}</a></dd>',
            "</dl>",
            f"<p>{e(m.begruendung) or '<em>Keine Begründung geliefert.</em>'}</p>",
        ]
        if m.fundstelle:
            marker = (
                ' <span class="unverified">(unbelegt — nicht im Ausschreibungstext gefunden)</span>'
                if not m.fundstelle_belegt
                else ""
            )
            parts.append(f"<blockquote>{e(m.fundstelle)}{marker}</blockquote>")
        parts.append("</div>")

    parts += framework_block()
    parts.append("</body></html>")
    return "\n".join(parts)


def write_reports(
    profile: Profile,
    matches: list[Match],
    output_dir: str | Path,
    generated_at: datetime | None = None,
    frameworks: list | None = None,
) -> tuple[Path, Path]:
    directory = Path(output_dir) / profile.id
    directory.mkdir(parents=True, exist_ok=True)

    md_path = directory / "report.md"
    html_path = directory / "report.html"
    md_path.write_text(render_markdown(profile, matches, generated_at, frameworks), encoding="utf-8")
    html_path.write_text(render_html(profile, matches, generated_at, frameworks), encoding="utf-8")
    return md_path, html_path
