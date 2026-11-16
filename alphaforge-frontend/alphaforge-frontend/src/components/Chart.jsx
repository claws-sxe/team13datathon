// Graphique placé avec la réponse du coach qu'il explique. Tout est lisible sans survol : repères numérotés,
// dates, devise, source et message principal sont écrits sous le graphique.
import { useId, useState } from "react";
import { altText, fmtNum, geometry, keyPoints, periodLine, takeaways } from "../chartModel";

const STYLES = [{ cls: "s0", dash: "" }, { cls: "s1", dash: "6 4" }];

export default function Chart({ chart }) {
  const [currency, setCurrency] = useState(chart.default);
  const uid = useId();
  const variant = chart.variants[currency] ?? chart.variants[chart.default];
  const g = geometry(chart, variant);
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
            <text x={g.left - 4} y={g.y(v) + 3} textAnchor="end">{ticks(v)}</text>
          </g>
        ))}
        {g.base != null && <line className="base" x1={g.left} x2={g.w - g.right} y1={g.base} y2={g.base} />}
        {g.xTicks.map((t, k) => (
          <text key={`x${t.i}`} className="axis" x={g.x(t.i)} y={g.h - 10}
                textAnchor={g.xTicks.length === 2 ? (k ? "end" : "start") : "middle"}>{t.label}</text>
        ))}
        {g.paths.map((p) => (
          <path key={p.id} d={p.d} className={`line ${STYLES[p.k % 2].cls}`} strokeDasharray={STYLES[p.k % 2].dash} />
        ))}
        {g.bracket && (
          <g className="bracket">
            <line x1={g.bracket.fromX} x2={g.bracket.x} y1={g.bracket.y1} y2={g.bracket.y1} strokeDasharray="3 3" />
            <line x1={g.bracket.x} x2={g.bracket.x} y1={g.bracket.y1} y2={g.bracket.y2} />
            <text x={g.bracket.x + 5} y={(g.bracket.y1 + g.bracket.y2) / 2 + 4}
                  textAnchor={g.bracket.x > g.w * 0.75 ? "end" : "start"}
                  dx={g.bracket.x > g.w * 0.75 ? -10 : 0}>{g.bracket.label}</text>
          </g>
        )}
        {g.points.map((p) => (
          <g key={p.n} className={`mark ${p.type}`}>
            <circle cx={p.cx} cy={p.cy} r="8" />
            <text x={p.cx} y={p.cy + 3.5} textAnchor="middle">{p.n}</text>
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
      {g.points.length > 0 && (
        <ol className="chart-points">
          {keyPoints(chart, variant).map((p) => <li key={p.n} className={p.type}><span className="pin">{p.n}</span>{p.text}</li>)}
        </ol>
      )}
      <ul className="chart-takeaways">
        {takeaways(chart, variant).map((t) => <li key={t}>{t}</li>)}
      </ul>
      <p className="chart-meta">
        {periodLine(chart, variant) && <span>{periodLine(chart, variant)}. </span>}
        {chart.explain}
      </p>
      <p className="chart-meta">Source : {chart.source}</p>
    </figure>
  );
}
