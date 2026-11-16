// Graphique placé avec la réponse du coach qu'il explique. Tout est lisible sans survol : le résumé (devise, période,
// baisses) vient d'abord, puis les repères numérotés, puis l'explication et la source.
import { useId, useState } from "react";
import { BADGE_R, altText, fmtNum, geometry, keyPoints, summary } from "../chartModel";

const STYLES = [{ cls: "s0", dash: "" }, { cls: "s1", dash: "6 4" }];

export default function Chart({ chart }) {
  const [currency, setCurrency] = useState(chart.default);
  const uid = useId();
  const variant = chart.variants[currency] ?? chart.variants[chart.default];
  const g = geometry(chart, variant);
  const sum = summary(chart, variant);
  const ticks = (v) => (chart.kind === "compare" ? fmtNum(v, 0) : chart.kind === "example" ? `${fmtNum(v, 0)} $` : fmtNum(v, v >= 1000 ? 0 : 2));

  return (
    <figure className={`chart chart-${chart.kind}`} aria-labelledby={`${uid}-t`}>
      <figcaption id={`${uid}-t`} className="chart-title">
        {chart.fictional && <span className="badge">Exemple fictif</span>}
        <strong>{chart.title}</strong>
      </figcaption>

      {chart.currencies.length > 1 && (
        <div className="seg" role="group" aria-label="Devise du graphique">
          {chart.currencies.map((c) => (
            <button key={c} type="button" className={c === currency ? "on" : ""} aria-pressed={c === currency}
                    onClick={() => setCurrency(c)}>{c}</button>
          ))}
        </div>
      )}

      <svg viewBox={`0 0 ${g.w} ${g.h}`} role="img" aria-label={altText(chart, variant)} preserveAspectRatio="xMidYMid meet">
        {g.yTicks.map((v) => (
          <g key={`y${v}`} className="axis">
            <line x1={g.left} x2={g.w - g.right} y1={g.y(v)} y2={g.y(v)} />
            <text x={g.left - 6} y={g.y(v) + 4} textAnchor="end">{ticks(v)}</text>
          </g>
        ))}
        {g.base != null && <line className="base" x1={g.left} x2={g.w - g.right} y1={g.base} y2={g.base} />}
        {g.xTicks.map((t) => (
          <text key={`x${t.i}`} className="axis" x={g.x(t.i)} y={g.xLabelY} textAnchor={t.anchor}>{t.label}</text>
        ))}
        {g.paths.map((p) => (
          <path key={p.id} d={p.d} className={`line ${STYLES[p.k % 2].cls}`} strokeDasharray={STYLES[p.k % 2].dash} />
        ))}
        {g.bracket && (
          <g className="bracket">
            <line x1={g.bracket.fromX} x2={g.bracket.x} y1={g.bracket.y1} y2={g.bracket.y1} strokeDasharray="3 3" />
            <line x1={g.bracket.x} x2={g.bracket.x} y1={g.bracket.y1} y2={g.bracket.y2} />
            {/* fond et contour blancs : la valeur reste lisible même si la courbe passe dessous */}
            <rect className="label-bg" x={g.bracket.box.x} y={g.bracket.box.y} width={g.bracket.box.w} height={g.bracket.box.h} rx="3" />
            <text className="halo" x={g.bracket.tx} y={g.bracket.ty} textAnchor={g.bracket.anchor}>{g.bracket.label}</text>
          </g>
        )}
        {g.points.map((p) => (
          <g key={p.n} className={`mark ${p.type} ser${p.k % 2}`}>
            {p.leader && <line className="leader" x1={p.leader.x1} y1={p.leader.y1} x2={p.leader.x2} y2={p.leader.y2} />}
            <circle className="dot" cx={p.cx} cy={p.cy} r="2.6" />
            <circle className="pin-c" cx={p.bx} cy={p.by} r={BADGE_R} />
            <text x={p.bx} y={p.by + 3.6} textAnchor="middle">{p.n}</text>
          </g>
        ))}
      </svg>

      {variant.series.length > 1 && (
        <ul className="chart-legend">
          {variant.series.map((s, k) => (
            <li key={s.id}><i className={`swatch ${STYLES[k % 2].cls}`} aria-hidden="true" />{s.label}</li>
          ))}
        </ul>
      )}

      <div className="chart-summary">
        <p className="chart-head">{sum.head}</p>
        <ul>
          {sum.lines.map((l) => (
            <li key={l.label}>
              <span>{l.label}</span> <strong className="val">{l.value}</strong>
              {l.detail && <span className="detail"> ({l.detail})</span>}
            </li>
          ))}
        </ul>
        {sum.conclusion && <p className="chart-conclusion">{sum.conclusion}</p>}
      </div>

      {g.points.length > 0 && (
        <ol className="chart-points" aria-label="Repères du graphique">
          {keyPoints(chart, variant).map((p) => (
            <li key={p.n} className={`${p.type} ser${g.points[p.n - 1].k % 2}`}><span className="pin">{p.n}</span>{p.text}</li>
          ))}
        </ol>
      )}
      <p className="chart-meta">{chart.explain}</p>
      <p className="chart-meta">Source : {chart.source}</p>
    </figure>
  );
}
